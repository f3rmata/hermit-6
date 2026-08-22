#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
requested_mode=${MODE-}
requested_run_id=${RUN_ID-}
requested_result_dir=${RESULT_DIR-}
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'USAGE'
usage:
  MODE=cgroup-hermit REDIS_WORKSET_MB=16384 LOCAL_RATIO_PCT=70 \
    ./run_redis_page_sweep.sh

Redis large-value page-size sweep under Hermit. For every requested page size
the script starts a Redis server and a signal-driven Python harness in a cgroup,
loads deterministic large string values, lowers memory.max to force RDMA stores,
restores memory.max, and GET-scans the keys to measure and verify RDMA loads.

Important environment:
  PAGE_SIZES_KB="4 16 32 64 128 256 512 1024 2048"
  REDIS_WORKSET_MB=16384            (total value bytes to load)
  REDIS_VALUE_SIZE=2097152          (bytes per value; default 2 MiB)
  REDIS_PORT=6391
  REDIS_SERVER_BIN=/path/to/redis-server
  LOCAL_RATIO_PCT=70                (limit = resident MiB * ratio / 100)
  BENCH_REPEATS=3
  BENCH_CPU=0                       (Python harness CPU)
  REDIS_SERVER_CPU=0                (redis-server CPU)
  QUIET_INTERVAL_SEC=1
  QUIET_SAMPLES=3
  QUIET_TIMEOUT_SEC=120
  READY_TIMEOUT_SEC=600
  BENCH_TIMEOUT_SEC=3600
  RESTORE_THP=1

Requirements: cgroup v2, active Hermit RDMA backend, debugfs, zswap disabled,
and exactly one block-device swap area. The script owns
/sys/fs/cgroup/hermit-redis.
USAGE
}

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
  usage
  exit 0
fi

MODE=$(normalize_mode "${requested_mode:-cgroup-hermit}")
[ "$MODE" = cgroup-hermit ] || rdma_die "this benchmark requires MODE=cgroup-hermit"

PAGE_SIZES_KB=${PAGE_SIZES_KB:-"4 16 32 64 128 256 512 1024 2048"}
REDIS_WORKSET_MB=${REDIS_WORKSET_MB:-16384}
REDIS_VALUE_SIZE=${REDIS_VALUE_SIZE:-2097152}
REDIS_PORT=${REDIS_PORT:-6391}
REDIS_SERVER_BIN=${REDIS_SERVER_BIN:-}
LOCAL_RATIO_PCT=${LOCAL_RATIO_PCT:-70}
BENCH_REPEATS=${BENCH_REPEATS:-3}
BENCH_CPU=${BENCH_CPU:-0}
REDIS_SERVER_CPU=${REDIS_SERVER_CPU:-0}
QUIET_INTERVAL_SEC=${QUIET_INTERVAL_SEC:-1}
QUIET_SAMPLES=${QUIET_SAMPLES:-3}
QUIET_TIMEOUT_SEC=${QUIET_TIMEOUT_SEC:-120}
READY_TIMEOUT_SEC=${READY_TIMEOUT_SEC:-600}
BENCH_TIMEOUT_SEC=${BENCH_TIMEOUT_SEC:-3600}
RESTORE_THP=${RESTORE_THP:-1}
RUN_ID=${requested_run_id:-"$(date +%Y%m%d-%H%M%S)-redis-swapio"}
RESULT_DIR=${requested_result_dir:-"$RESULT_ROOT/$RUN_ID"}
WORKER="$SCRIPT_DIR/redis_bench.py"
CGROUP=/sys/fs/cgroup/hermit-redis
THP_ROOT=/sys/kernel/mm/transparent_hugepage
REMOTE_MASK_FILE=/sys/kernel/debug/hermit/remote_order_mask
EFFECTIVE_MASK_FILE=/sys/kernel/debug/hermit/effective_order_mask
CSV="$RESULT_DIR/redis-swapio-summary.csv"
current_worker_pid=
current_redis_pid=
remote_mask_before=
declare -a thp_files=()
declare -A thp_policy=()

page_order() {
  case "$1" in
    4) printf 0 ;; 16) printf 2 ;; 32) printf 3 ;; 64) printf 4 ;;
    128) printf 5 ;; 256) printf 6 ;; 512) printf 7 ;;
    1024) printf 8 ;; 2048) printf 9 ;; *) return 1 ;;
  esac
}

selected_policy() {
  awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^\[.*\]$/) { gsub(/\[|\]/, "", $i); print $i; exit } }' "$1"
}

find_redis_server_bin() {
  local candidates=()
  if [ -n "$REDIS_SERVER_BIN" ]; then
    candidates+=("$REDIS_SERVER_BIN")
  fi
  candidates+=("$PWD/redis/src/redis-server" "$HOME/redis/src/redis-server")
  if command -v redis-server >/dev/null 2>&1; then
    candidates+=("$(command -v redis-server)")
  fi
  local bin
  for bin in "${candidates[@]}"; do
    if [ -n "$bin" ] && [ -x "$bin" ]; then
      printf '%s' "$bin"
      return 0
    fi
  done
  rdma_die "cannot find redis-server; set REDIS_SERVER_BIN=/path/to/redis-server"
}

snapshot_order_stats() {
  sudo_cat /sys/kernel/debug/hermit/order_stats
}

order_field() {
  local file=$1 order=$2 column=$3
  awk -v o="$order" -v c="$column" 'NR > 1 && $1 == o { print $c; found=1 } END { if (!found) print 0 }' "$file"
}

order_total_stores() {
  snapshot_order_stats | awk 'NR > 1 { total += $3 } END { printf "%d", total }'
}

wait_for_stores_quiet() {
  local previous current quiet=0 start
  start=$(date +%s)
  previous=$(order_total_stores)
  while [ "$quiet" -lt "$QUIET_SAMPLES" ]; do
    sleep "$QUIET_INTERVAL_SEC"
    current=$(order_total_stores)
    if [ "$current" -eq "$previous" ]; then
      quiet=$((quiet + 1))
    else
      previous=$current
      quiet=0
    fi
    if [ "$quiet" -lt "$QUIET_SAMPLES" ]; then
      [ "$(( $(date +%s) - start ))" -lt "$QUIET_TIMEOUT_SEC" ] || \
        rdma_die "Hermit store counters did not become quiet; stop other swap workloads"
    fi
  done
}

memory_event() {
  local key=$1
  awk -v key="$key" '$1 == key { print $2; found=1 } END { if (!found) print 0 }' \
    "$CGROUP/memory.events"
}

save_thp_state() {
  local file policy
  for file in "$THP_ROOT/enabled" "$THP_ROOT"/hugepages-*kB/enabled; do
    [ -e "$file" ] || continue
    policy=$(selected_policy "$file")
    [ -n "$policy" ] || rdma_die "cannot read THP policy: $file"
    thp_files+=("$file")
    thp_policy["$file"]=$policy
  done
  remote_mask_before=$(sudo_cat "$REMOTE_MASK_FILE")
}

configure_page_size() {
  local kb=$1 order=$2 file selected
  for file in "${thp_files[@]}"; do
    sudo_write never "$file"
  done
  if [ "$kb" != 4 ]; then
    selected="$THP_ROOT/hugepages-${kb}kB/enabled"
    if [ "$kb" = 2048 ] && [ ! -e "$selected" ]; then
      selected="$THP_ROOT/enabled"
    fi
    [ -e "$selected" ] || return 1
    sudo_write always "$selected"
    [ "$kb" != 2048 ] || sudo_write always "$THP_ROOT/enabled"
  fi
  sudo_write "$(printf '0x%x' $((1 | (1 << order))))" "$REMOTE_MASK_FILE"
}

start_redis_server() {
  local bin log
  bin=$(find_redis_server_bin)
  log="$1"
  taskset -c "$REDIS_SERVER_CPU" "$bin" \
    --port "$REDIS_PORT" \
    --bind 127.0.0.1 \
    --protected-mode no \
    --save "" \
    --appendonly no \
    --daemonize no \
    --stop-writes-on-bgsave-error no \
    --rdbcompression no \
    --maxmemory 0 \
    > "$log" 2>&1 &
  current_redis_pid=$!
  sudo_write "$current_redis_pid" "$CGROUP/cgroup.procs"
}

wait_for_redis_ready() {
  local log=$1 start
  start=$(date +%s)
  until grep -q 'Ready to accept connections' "$log" 2>/dev/null; do
    kill -0 "$current_redis_pid" 2>/dev/null || rdma_die "redis-server exited; see $log"
    [ "$(( $(date +%s) - start ))" -lt "$READY_TIMEOUT_SEC" ] || \
      rdma_die "timeout waiting for redis-server; see $log"
    sleep 0.2
  done
}

cleanup_processes() {
  if [ -n "$current_worker_pid" ] && kill -0 "$current_worker_pid" 2>/dev/null; then
    kill -TERM "$current_worker_pid" 2>/dev/null || true
    wait "$current_worker_pid" 2>/dev/null || true
  fi
  if [ -n "$current_redis_pid" ] && kill -0 "$current_redis_pid" 2>/dev/null; then
    kill -TERM "$current_redis_pid" 2>/dev/null || true
    wait "$current_redis_pid" 2>/dev/null || true
  fi
  current_worker_pid=
  current_redis_pid=
  sudo_write max "$CGROUP/memory.max" 2>/dev/null || true
}

cleanup() {
  local ret=$? file
  cleanup_processes
  if [ "$RESTORE_THP" = 1 ]; then
    for file in "${thp_files[@]}"; do
      sudo_write "${thp_policy[$file]}" "$file" || true
    done
    [ -z "$remote_mask_before" ] || sudo_write "$remote_mask_before" "$REMOTE_MASK_FILE" || true
  fi
  sudo rmdir "$CGROUP" 2>/dev/null || true
  exit "$ret"
}

wait_for_log() {
  local pattern=$1 log=$2 timeout=$3 poll=${4:-1} start
  start=$(date +%s)
  until grep -q "$pattern" "$log" 2>/dev/null; do
    if [ -n "$current_worker_pid" ] && ! kill -0 "$current_worker_pid" 2>/dev/null; then
      rdma_die "redis harness exited; see $log"
    fi
    [ "$(( $(date +%s) - start ))" -lt "$timeout" ] || \
      rdma_die "timeout waiting for $pattern; see $log"
    sleep "$poll"
  done
}

[ -x "$WORKER" ] || rdma_die "missing executable worker: $WORKER"
command -v python3 >/dev/null 2>&1 || rdma_die "python3 is required"
[ -r /sys/fs/cgroup/cgroup.controllers ] || rdma_die "cgroup v2 is required"
mount_debugfs_if_needed
sudo_test -r /sys/kernel/debug/hermit/order_stats || rdma_die "Hermit order_stats is unavailable"
sudo_test -w "$REMOTE_MASK_FILE" || rdma_die "Hermit remote_order_mask is not writable"
[ "$(detect_rswap_backend)" = rdma ] || rdma_die "Hermit RDMA backend is not active"
swap_count=$(awk 'NR > 1 { count++ } END { print count + 0 }' /proc/swaps)
[ "$swap_count" -eq 1 ] || \
  rdma_die "exactly one active swap device is required; found $swap_count"
awk 'NR == 2 && $2 == "partition" { ok=1 } END { exit !ok }' /proc/swaps || \
  rdma_die "the active swap area must be a block device, not a swapfile"
if [ -r /sys/module/zswap/parameters/enabled ] && grep -qi '^Y' /sys/module/zswap/parameters/enabled; then
  rdma_die "zswap is enabled; disable it before measuring RDMA swap-out"
fi

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
save_thp_state
configure_hermit_mode

if sudo_test -d "$CGROUP"; then
  existing=$(sudo_cat "$CGROUP/cgroup.procs")
  [ -z "$existing" ] || rdma_die "$CGROUP is busy with PIDs: $existing"
else
  sudo mkdir "$CGROUP"
fi
sudo_write max "$CGROUP/memory.max"
sudo_write max "$CGROUP/memory.swap.max"

mkdir -p "$RESULT_DIR"
{
  printf 'date=%s\n' "$(date --iso-8601=seconds)"
  printf 'kernel=%s\n' "$(uname -r)"
  printf 'mode=%s\nlocal_ratio_pct=%s\n' "$MODE" "$LOCAL_RATIO_PCT"
  printf 'page_sizes_kb=%s\nrepeats=%s\n' "$PAGE_SIZES_KB" "$BENCH_REPEATS"
  printf 'bench_cpu=%s\nredis_server_cpu=%s\n' "$BENCH_CPU" "$REDIS_SERVER_CPU"
  printf 'redis_workset_mb=%s\nredis_value_size=%s\nredis_port=%s\n' \
    "$REDIS_WORKSET_MB" "$REDIS_VALUE_SIZE" "$REDIS_PORT"
  printf 'redis_server_bin=%s\n' "$(find_redis_server_bin)"
  printf 'backend=%s\n' "$(detect_rswap_backend)"
  printf 'swap:\n'
  sed 's/^/  /' /proc/swaps
} > "$RESULT_DIR/environment.txt"
printf 'page_kb,order,repeat,mode,resident_mb,limit_mb,value_size,keys,populate_sec,bench_sec,checksum_errors,pswpout_delta,target_stores_delta,target_loads_delta,target_fallback_delta,target_errors_delta,total_store_bytes,large_store_bytes,large_store_pct,swapout_gib,protocol_gib,protocol_gib_per_sec,pswpin_delta,bench_pswpout_delta,total_load_bytes,large_load_bytes,large_load_pct,swapin_gib,load_protocol_gib,load_protocol_gib_per_sec\n' > "$CSV"

export REDIS_WORKSET_MB REDIS_VALUE_SIZE REDIS_PORT

for kb in $PAGE_SIZES_KB; do
  order=$(page_order "$kb") || rdma_die "unsupported page size: ${kb} KiB"
  configure_page_size "$kb" "$order" || rdma_die "THP size ${kb} KiB is unavailable"
  effective=$(sudo_cat "$EFFECTIVE_MASK_FILE")
  (( (effective & (1 << order)) != 0 )) || rdma_die "effective mask $effective lacks order $order"

  for repeat in $(seq 1 "$BENCH_REPEATS"); do
    run_dir="$RESULT_DIR/${kb}k/r${repeat}"
    redis_log="$run_dir/redis.log"
    worker_log="$run_dir/redis-bench.log"
    before="$run_dir/order-before.txt"
    after="$run_dir/order-after.txt"
    mkdir -p "$run_dir"
    sudo_write max "$CGROUP/memory.max"

    start_redis_server "$redis_log"
    wait_for_redis_ready "$redis_log"

    taskset -c "$BENCH_CPU" python3 "$WORKER" > "$worker_log" 2>&1 &
    current_worker_pid=$!
    sudo_write "$current_worker_pid" "$CGROUP/cgroup.procs"
    wait_for_log '^WAITING ' "$worker_log" "$READY_TIMEOUT_SEC"
    kill -USR1 "$current_worker_pid"
    wait_for_log '^READY ' "$worker_log" "$READY_TIMEOUT_SEC"

    populate_sec=$(awk -F'populate_sec=' '/^READY / { print $2; exit }' "$worker_log")
    keys=$(awk -F'keys=' '/^READY / { print $2; exit }' "$worker_log" | awk '{ print $1 }')
    value_size=$(awk -F'value_size=' '/^READY / { print $2; exit }' "$worker_log" | awk '{ print $1 }')
    [ -n "$populate_sec" ] || rdma_die "missing READY metrics; see $worker_log"

    resident_bytes=$(sudo_cat "$CGROUP/memory.current")
    resident_mb=$((resident_bytes / 1024 / 1024))
    limit_mb=$(max_int 256 $((resident_mb * LOCAL_RATIO_PCT / 100)))
    [ "$limit_mb" -lt "$resident_mb" ] || \
      rdma_die "computed limit ${limit_mb} MiB is not below resident ${resident_mb} MiB; lower LOCAL_RATIO_PCT or enlarge the workset"
    required_swap_mb=$((resident_mb - limit_mb))
    free_swap_mb=$(awk 'NR > 1 { free += $3 - $4 } END { printf "%d", free / 1024 }' /proc/swaps)
    [ "$free_swap_mb" -ge "$required_swap_mb" ] || \
      rdma_die "only ${free_swap_mb} MiB swap is free; at least ${required_swap_mb} MiB is required"

    sudo_cat "/proc/$current_redis_pid/smaps_rollup" > "$run_dir/redis-smaps-before.txt"
    sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-before.txt"
    wait_for_stores_quiet
    snapshot_order_stats > "$before"
    pswpout_before=$(read_vmstat_key pswpout)
    stores_before=$(order_total_stores)
    oom_before=$(memory_event oom_kill)
    start_ns=$(date +%s%N)
    sudo_write "$((limit_mb * 1024 * 1024))" "$CGROUP/memory.max"
    last_change_ns=$start_ns
    previous=$(order_total_stores)
    quiet=0
    quiet_start=$(date +%s)
    while [ "$quiet" -lt "$QUIET_SAMPLES" ]; do
      sleep "$QUIET_INTERVAL_SEC"
      current=$(order_total_stores)
      if [ "$current" -ne "$previous" ]; then
        previous=$current
        quiet=0
        last_change_ns=$(date +%s%N)
      else
        quiet=$((quiet + 1))
      fi
      if [ "$quiet" -lt "$QUIET_SAMPLES" ]; then
        [ "$(( $(date +%s) - quiet_start ))" -lt "$QUIET_TIMEOUT_SEC" ] || \
          rdma_die "swap-out did not become quiet within ${QUIET_TIMEOUT_SEC}s; see $run_dir"
      fi
    done
    snapshot_order_stats > "$after"
    pswpout_after=$(read_vmstat_key pswpout)
    sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-after.txt"
    sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after.txt"
    oom_after=$(memory_event oom_kill)
    [ "$oom_after" -eq "$oom_before" ] || rdma_die "cgroup OOM killed redis; see $run_dir"
    kill -0 "$current_redis_pid" 2>/dev/null || rdma_die "redis-server exited during reclaim; see $redis_log"
    kill -0 "$current_worker_pid" 2>/dev/null || rdma_die "redis harness exited during reclaim; see $worker_log"

    target_stores=$(( $(order_field "$after" "$order" 3) - $(order_field "$before" "$order" 3) ))
    target_loads=$(( $(order_field "$after" "$order" 4) - $(order_field "$before" "$order" 4) ))
    target_fallback=$(( $(order_field "$after" "$order" 5) - $(order_field "$before" "$order" 5) ))
    target_errors=$(( $(order_field "$after" "$order" 6) - $(order_field "$before" "$order" 6) ))
    pswpout_delta=$((pswpout_after - pswpout_before))
    stores_after=$(order_total_stores)
    stores_delta=$((stores_after - stores_before))

    metrics=$(awk -v before="$before" -v after="$after" -v ms="$(( $(date +%s%N) - start_ns ))" '
      FILENAME == before && NR > 1 { b[$1]=$3; fb[$1]=$5; size[$1]=$2; next }
      FILENAME == after && FNR > 1 {
        d=$3-b[$1]; f=$5-fb[$1];
        if (f < 0) f=0; if (f > d) f=d;
        total += d*$2; if ($1 > 0) large += (d-f)*$2
      }
      END {
        pct=total ? 100*large/total : 0;
        gib=total/1073741824;
        ms=(ms > 0 ? ms : 1);
        printf "%.0f,%.0f,%.3f,%.6f", total, large, pct, gib/(ms/1000)
      }' "$before" "$after")
    total_store_bytes=${metrics%%,*}
    rest=${metrics#*,}; large_store_bytes=${rest%%,*}
    rest=${rest#*,}; large_store_pct=${rest%%,*}
    protocol_gib_per_sec=${rest#*,}
    swapout_gib=$(awk -v p="$pswpout_delta" 'BEGIN { printf "%.6f", p*4096/1073741824 }')
    protocol_gib=$(awk -v b="$total_store_bytes" 'BEGIN { printf "%.6f", b/1073741824 }')

    # Restore headroom before scanning; otherwise GETs force new swap-outs.
    sudo_write max "$CGROUP/memory.max"
    load_before="$run_dir/order-before-swapin.txt"
    load_after="$run_dir/order-after-swapin.txt"
    snapshot_order_stats > "$load_before"
    pswpin_before=$(read_vmstat_key pswpin)
    bench_pswpout_before=$(read_vmstat_key pswpout)
    bench_start_ns=$(date +%s%N)
    kill -USR2 "$current_worker_pid"
    wait_for_log '^BENCH ' "$worker_log" "$BENCH_TIMEOUT_SEC" 1
    bench_end_ns=$(date +%s%N)
    snapshot_order_stats > "$load_after"
    pswpin_after=$(read_vmstat_key pswpin)
    bench_pswpout_after=$(read_vmstat_key pswpout)
    sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-after-swapin.txt"
    sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after-swapin.txt"

    bench_sec=$(awk -F'sec=' '/^BENCH / { print $2; exit }' "$worker_log" | awk '{ print $1 }')
    checksum_errors=$(awk -F'checksum_errors=' '/^BENCH / { print $2; exit }' "$worker_log" | awk '{ print $1 }')
    [ -n "$bench_sec" ] || rdma_die "missing BENCH metrics; see $worker_log"
    [ "${checksum_errors:-1}" -eq 0 ] || rdma_die "redis GET checksum mismatch; see $worker_log"

    swapin_wall_ms=$(( (bench_end_ns - bench_start_ns) / 1000000 ))
    pswpin_delta=$((pswpin_after - pswpin_before))
    bench_pswpout_delta=$((bench_pswpout_after - bench_pswpout_before))
    target_loads=$(( $(order_field "$load_after" "$order" 4) - $(order_field "$load_before" "$order" 4) ))
    target_fallback=$(( $(order_field "$load_after" "$order" 5) - $(order_field "$load_before" "$order" 5) ))
    target_errors=$(( $(order_field "$load_after" "$order" 6) - $(order_field "$load_before" "$order" 6) ))

    if ! load_metrics=$(awk -v before="$load_before" -v after="$load_after" -v sec="$bench_sec" '
      FILENAME == before && NR > 1 { b[$1]=$4; fb[$1]=$5; size[$1]=$2; next }
      FILENAME == after && FNR > 1 {
        d=$4-b[$1]; f=$5-fb[$1];
        if (f < 0) f=0; if (f > d) f=d;
        total += d*$2; if ($1 > 0) large += (d-f)*$2
      }
      END {
        pct=total ? 100*large/total : 0;
        printf "%.0f,%.0f,%.3f,%.6f", total, large, pct,
               (sec > 0 ? total / 1073741824 / sec : 0)
      }' "$load_before" "$load_after"); then
      rdma_die "failed to summarize Hermit swap-in counters; see $run_dir"
    fi
    [ -n "$load_metrics" ] || \
      rdma_die "empty Hermit swap-in counter summary; see $run_dir"
    total_load_bytes=${load_metrics%%,*}
    rest=${load_metrics#*,}; large_load_bytes=${rest%%,*}
    rest=${rest#*,}; large_load_pct=${rest%%,*}
    load_protocol_gib_per_sec=${rest#*,}
    swapin_gib=$(awk -v p="$pswpin_delta" 'BEGIN { printf "%.6f", p*4096/1073741824 }')
    load_protocol_gib=$(awk -v b="$total_load_bytes" 'BEGIN { printf "%.6f", b/1073741824 }')
    oom_after_swapin=$(memory_event oom_kill)
    [ "$oom_after_swapin" -eq "$oom_before" ] || rdma_die "cgroup OOM during GET scan; see $run_dir"
    [ "$pswpin_delta" -gt 0 ] || rdma_die "GET scan caused no swap-ins; see $run_dir"
    [[ "$total_load_bytes" =~ ^[0-9]+$ ]] && [ "$total_load_bytes" -gt 0 ] || \
      rdma_die "Hermit recorded no backend loads; see $run_dir"
    [ "$bench_pswpout_delta" -eq 0 ] || \
      rdma_die "GET scan caused $bench_pswpout_delta new swap-outs; results are not isolated"

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$kb" "$order" "$repeat" "$MODE" "$resident_mb" "$limit_mb" \
      "$value_size" "$keys" "$populate_sec" "$bench_sec" "$checksum_errors" \
      "$pswpout_delta" "$target_stores" "$target_loads" "$target_fallback" \
      "$target_errors" "$total_store_bytes" "$large_store_bytes" \
      "$large_store_pct" "$swapout_gib" "$protocol_gib" \
      "$protocol_gib_per_sec" "$pswpin_delta" "$bench_pswpout_delta" \
      "$total_load_bytes" "$large_load_bytes" "$large_load_pct" \
      "$swapin_gib" "$load_protocol_gib" "$load_protocol_gib_per_sec" >> "$CSV"
    rdma_log "page=${kb}k repeat=$repeat stores=$stores_delta store_large=${large_store_pct}% swapout=${protocol_gib_per_sec}GiB/s loads=$target_loads load_large=${large_load_pct}% bench_sec=$bench_sec checksum_errors=$checksum_errors"
    cleanup_processes
  done
done

trap - EXIT INT TERM
cleanup
