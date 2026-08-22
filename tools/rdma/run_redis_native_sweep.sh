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
  MODE=local ./run_redis_native_sweep.sh
  MODE=cgroup-linux LOCAL_RATIO_PCT=70 ./run_redis_native_sweep.sh

Native local / local-swap Redis large-value page-size baseline. The script
uses the same Redis server and Python harness as the Hermit sweep, but never
touches Hermit debugfs. For cgroup-linux it lowers memory.max to force native
swap-out and then GET-scans the keys to measure native swap-in; for local it
simply runs the scan with unlimited memory.

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

Requirements: for MODE=cgroup-linux, cgroup v2 and exactly one active
block-device swap area; the script owns /sys/fs/cgroup/hermit-redis-native.
USAGE
}

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
  usage
  exit 0
fi

MODE=$(normalize_mode "${requested_mode:-cgroup-linux}")
case "$MODE" in
  local|cgroup-linux) ;;
  *) rdma_die "unsupported mode $MODE; use local or cgroup-linux" ;;
esac

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
RUN_ID=${requested_run_id:-"$(date +%Y%m%d-%H%M%S)-redis-native"}
RESULT_DIR=${requested_result_dir:-"$RESULT_ROOT/$RUN_ID"}
WORKER="$SCRIPT_DIR/redis_bench.py"
CGROUP_NAME=hermit-redis-native
CGROUP=/sys/fs/cgroup/$CGROUP_NAME
THP_ROOT=/sys/kernel/mm/transparent_hugepage
CSV="$RESULT_DIR/redis-native-summary.csv"
current_worker_pid=
current_redis_pid=
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

save_thp_state() {
  local file policy
  for file in "$THP_ROOT/enabled" "$THP_ROOT"/hugepages-*kB/enabled; do
    [ -e "$file" ] || continue
    policy=$(selected_policy "$file")
    [ -n "$policy" ] || rdma_die "cannot read THP policy: $file"
    thp_files+=("$file")
    thp_policy["$file"]=$policy
  done
}

configure_page_size() {
  local kb=$1 selected
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
}

wait_for_pswpout_quiet() {
  local previous current quiet=0 start
  start=$(date +%s)
  previous=$(read_vmstat_key pswpout)
  while [ "$quiet" -lt "$QUIET_SAMPLES" ]; do
    sleep "$QUIET_INTERVAL_SEC"
    current=$(read_vmstat_key pswpout)
    if [ "$current" -eq "$previous" ]; then
      quiet=$((quiet + 1))
    else
      previous=$current
      quiet=0
    fi
    if [ "$quiet" -lt "$QUIET_SAMPLES" ]; then
      [ "$(( $(date +%s) - start ))" -lt "$QUIET_TIMEOUT_SEC" ] || \
        rdma_die "pswpout did not become quiet; stop other swap workloads"
    fi
  done
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
  if [ "$MODE" = cgroup-linux ]; then
    move_pid_to_cgroup "$current_redis_pid"
  fi
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
  if [ "$MODE" = cgroup-linux ]; then
    sudo_write max "$CGROUP/memory.max" 2>/dev/null || true
  fi
}

cleanup() {
  local ret=$? file
  cleanup_processes
  if [ "$RESTORE_THP" = 1 ]; then
    for file in "${thp_files[@]}"; do
      sudo_write "${thp_policy[$file]}" "$file" || true
    done
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
if [ "$MODE" = cgroup-linux ]; then
  [ -r /sys/fs/cgroup/cgroup.controllers ] || rdma_die "cgroup v2 is required"
  swap_count=$(awk 'NR > 1 { count++ } END { print count + 0 }' /proc/swaps)
  [ "$swap_count" -eq 1 ] || \
    rdma_die "exactly one active swap device is required; found $swap_count"
  awk 'NR == 2 && $2 == "partition" { ok=1 } END { exit !ok }' /proc/swaps || \
    rdma_die "the active swap area must be a block device, not a swapfile"
fi

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
save_thp_state

if [ "$MODE" = cgroup-linux ]; then
  setup_cgroup
  if sudo_test -d "$CGROUP"; then
    existing=$(sudo_cat "$CGROUP/cgroup.procs")
    [ -z "$existing" ] || rdma_die "$CGROUP is busy with PIDs: $existing"
  else
    sudo mkdir "$CGROUP"
  fi
  sudo_write max "$CGROUP/memory.max"
  sudo_write max "$CGROUP/memory.swap.max"
fi

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
  printf 'swap:\n'
  sed 's/^/  /' /proc/swaps
} > "$RESULT_DIR/environment.txt"
printf 'page_kb,order,repeat,mode,resident_mb,limit_mb,value_size,keys,populate_sec,bench_sec,checksum_errors,pswpin_delta,pswpout_delta,bench_pswpout_delta\n' > "$CSV"

export REDIS_WORKSET_MB REDIS_VALUE_SIZE REDIS_PORT

for kb in $PAGE_SIZES_KB; do
  order=$(page_order "$kb") || rdma_die "unsupported page size: ${kb} KiB"
  configure_page_size "$kb" || rdma_die "THP size ${kb} KiB is unavailable"

  for repeat in $(seq 1 "$BENCH_REPEATS"); do
    run_dir="$RESULT_DIR/${kb}k/r${repeat}"
    redis_log="$run_dir/redis.log"
    worker_log="$run_dir/redis-bench.log"
    mkdir -p "$run_dir"
    if [ "$MODE" = cgroup-linux ]; then
      sudo_write max "$CGROUP/memory.max"
    fi

    start_redis_server "$redis_log"
    wait_for_redis_ready "$redis_log"

    taskset -c "$BENCH_CPU" python3 "$WORKER" > "$worker_log" 2>&1 &
    current_worker_pid=$!
    if [ "$MODE" = cgroup-linux ]; then
      move_pid_to_cgroup "$current_worker_pid"
    fi
    wait_for_log '^WAITING ' "$worker_log" "$READY_TIMEOUT_SEC"
    kill -USR1 "$current_worker_pid"
    wait_for_log '^READY ' "$worker_log" "$READY_TIMEOUT_SEC"

    populate_sec=$(awk -F'populate_sec=' '/^READY / { print $2; exit }' "$worker_log")
    keys=$(awk -F'keys=' '/^READY / { print $2; exit }' "$worker_log" | awk '{ print $1 }')
    value_size=$(awk -F'value_size=' '/^READY / { print $2; exit }' "$worker_log" | awk '{ print $1 }')
    [ -n "$populate_sec" ] || rdma_die "missing READY metrics; see $worker_log"

    resident_mb=0
    limit_mb=0
    pswpout_delta=0
    pswpin_delta=0
    bench_pswpout_delta=0

    if [ "$MODE" = cgroup-linux ]; then
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
      wait_for_pswpout_quiet
      pswpout_before=$(read_vmstat_key pswpout)
      oom_before=$(cgroup_event_value oom_kill)
      sudo_write "$((limit_mb * 1024 * 1024))" "$CGROUP/memory.max"
      previous=$(read_vmstat_key pswpout)
      quiet=0
      quiet_start=$(date +%s)
      while [ "$quiet" -lt "$QUIET_SAMPLES" ]; do
        sleep "$QUIET_INTERVAL_SEC"
        current=$(read_vmstat_key pswpout)
        if [ "$current" -ne "$previous" ]; then
          previous=$current
          quiet=0
        else
          quiet=$((quiet + 1))
        fi
        if [ "$quiet" -lt "$QUIET_SAMPLES" ]; then
          [ "$(( $(date +%s) - quiet_start ))" -lt "$QUIET_TIMEOUT_SEC" ] || \
            rdma_die "swap-out did not become quiet within ${QUIET_TIMEOUT_SEC}s; see $run_dir"
        fi
      done
      pswpout_after=$(read_vmstat_key pswpout)
      oom_after=$(cgroup_event_value oom_kill)
      [ "$oom_after" -eq "$oom_before" ] || rdma_die "cgroup OOM killed redis; see $run_dir"
      kill -0 "$current_redis_pid" 2>/dev/null || rdma_die "redis-server exited during reclaim; see $redis_log"
      kill -0 "$current_worker_pid" 2>/dev/null || rdma_die "redis harness exited during reclaim; see $worker_log"
      pswpout_delta=$((pswpout_after - pswpout_before))
      sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-after.txt"
      sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after.txt"
      sudo_write max "$CGROUP/memory.max"
    fi

    pswpin_before=$(read_vmstat_key pswpin)
    bench_pswpout_before=$(read_vmstat_key pswpout)
    bench_start_ns=$(date +%s%N)
    kill -USR2 "$current_worker_pid"
    wait_for_log '^BENCH ' "$worker_log" "$BENCH_TIMEOUT_SEC" 1
    bench_end_ns=$(date +%s%N)
    pswpin_after=$(read_vmstat_key pswpin)
    bench_pswpout_after=$(read_vmstat_key pswpout)
    if [ "$MODE" = cgroup-linux ]; then
      sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-after-swapin.txt"
      sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after-swapin.txt"
    fi

    bench_sec=$(awk -F'sec=' '/^BENCH / { print $2; exit }' "$worker_log" | awk '{ print $1 }')
    checksum_errors=$(awk -F'checksum_errors=' '/^BENCH / { print $2; exit }' "$worker_log" | awk '{ print $1 }')
    [ -n "$bench_sec" ] || rdma_die "missing BENCH metrics; see $worker_log"
    [ "${checksum_errors:-1}" -eq 0 ] || rdma_die "redis GET checksum mismatch; see $worker_log"

    pswpin_delta=$((pswpin_after - pswpin_before))
    bench_pswpout_delta=$((bench_pswpout_after - bench_pswpout_before))

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$kb" "$order" "$repeat" "$MODE" "$resident_mb" "$limit_mb" \
      "$value_size" "$keys" "$populate_sec" "$bench_sec" "$checksum_errors" \
      "$pswpin_delta" "$pswpout_delta" "$bench_pswpout_delta" >> "$CSV"
    rdma_log "page=${kb}k repeat=$repeat mode=$MODE bench_sec=$bench_sec checksum_errors=$checksum_errors pswpin_delta=$pswpin_delta pswpout_delta=$pswpout_delta"
    cleanup_processes
  done
done

trap - EXIT INT TERM
cleanup
