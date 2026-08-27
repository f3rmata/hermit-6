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
  MODE=cgroup-hermit WORKSET_MB=16384 LOCAL_RATIO_PCT=70 \
    ./run_anon_swapout_sweep.sh

Anonymous-memory sparse/random swap-out/swap-in protocol benchmark. For every
requested page size it fully populates a fresh working set, lowers memory.max
to measure RDMA stores, restores memory.max, and accesses a configurable subset
of base pages in each folio to measure and verify RDMA loads.

Important environment:
  PAGE_SIZES_KB="4 16 32 64 128 256 512 1024 2048"
  ACCESS_RATIOS="100 50 25 6.25 1p" (1p means one base page per folio)
  ACCESS_ORDERS="sequential random" (folio traversal order)
  ACCESS_LOCALITIES="high low"       (contiguous vs scattered base pages)
  ACCESS_SEED=1
  WORKSET_MB=16384
  LOCAL_RATIO_PCT=70                 (or set LIMIT_MB explicitly)
  LIMIT_MB=                          (overrides LOCAL_RATIO_PCT)
  BENCH_REPEATS=3
  BENCH_THREADS=1                    (parallel populate and swap-in faults)
  BENCH_CPUS=0                       (taskset CPU list, e.g. 0-7)
  BENCH_CPU=0                        (legacy alias used if BENCH_CPUS is unset)
  SWAPOUT_TRIGGER=memory-max         (memory-max or parallel-fault)
  QUIET_INTERVAL_SEC=1
  QUIET_SAMPLES=3
  QUIET_TIMEOUT_SEC=120
  SWAPIN_TIMEOUT_SEC=300
  RESULT_DIR=tools/rdma/results/<run-id>
  RESTORE_THP=1

Requirements: cgroup v2, active Hermit RDMA backend, debugfs, zswap disabled,
and exactly one block-device swap area. The script owns
/sys/fs/cgroup/hermit-anon-swapout.
USAGE
}

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
  usage
  exit 0
fi

MODE=$(normalize_mode "${requested_mode:-cgroup-hermit}")
[ "$MODE" = cgroup-hermit ] || rdma_die "this benchmark requires MODE=cgroup-hermit"
PAGE_SIZES_KB=${PAGE_SIZES_KB:-"4 16 32 64 128 256 512 1024 2048"}
ACCESS_RATIOS=${ACCESS_RATIOS:-"100 50 25 6.25 1p"}
ACCESS_ORDERS=${ACCESS_ORDERS:-"sequential random"}
ACCESS_LOCALITIES=${ACCESS_LOCALITIES:-"high low"}
ACCESS_SEED=${ACCESS_SEED:-1}
WORKSET_MB=${WORKSET_MB:-16384}
LOCAL_RATIO_PCT=${LOCAL_RATIO_PCT:-70}
LIMIT_MB=${LIMIT_MB:-$((WORKSET_MB * LOCAL_RATIO_PCT / 100))}
BENCH_REPEATS=${BENCH_REPEATS:-3}
BENCH_CPU=${BENCH_CPU:-0}
BENCH_CPUS=${BENCH_CPUS:-$BENCH_CPU}
BENCH_THREADS=${BENCH_THREADS:-1}
SWAPOUT_TRIGGER=${SWAPOUT_TRIGGER:-memory-max}
QUIET_INTERVAL_SEC=${QUIET_INTERVAL_SEC:-1}
QUIET_SAMPLES=${QUIET_SAMPLES:-3}
QUIET_TIMEOUT_SEC=${QUIET_TIMEOUT_SEC:-120}
READY_TIMEOUT_SEC=${READY_TIMEOUT_SEC:-300}
SWAPIN_TIMEOUT_SEC=${SWAPIN_TIMEOUT_SEC:-300}
RESTORE_THP=${RESTORE_THP:-1}
RUN_ID=${requested_run_id:-"$(date +%Y%m%d-%H%M%S)-anon-sparse-swapio"}
RESULT_DIR=${requested_result_dir:-"$RESULT_ROOT/$RUN_ID"}
BIN=${ANON_WORKSET_BIN:-"${TMPDIR:-/tmp}/hermit-anon-sparse-workset-$(id -u)"}
SRC="$SCRIPT_DIR/anon_seq_workset.c"
CGROUP=/sys/fs/cgroup/hermit-anon-swapout
THP_ROOT=/sys/kernel/mm/transparent_hugepage
REMOTE_MASK_FILE=/sys/kernel/debug/hermit/remote_order_mask
EFFECTIVE_MASK_FILE=/sys/kernel/debug/hermit/effective_order_mask
CSV="$RESULT_DIR/swapio-summary.csv"
current_pid=
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

cleanup_process() {
  if [ -n "$current_pid" ] && kill -0 "$current_pid" 2>/dev/null; then
    kill -TERM "$current_pid" 2>/dev/null || true
    wait "$current_pid" 2>/dev/null || true
  fi
  current_pid=
  sudo_write max "$CGROUP/memory.max" 2>/dev/null || true
}

cleanup() {
  local ret=$? file
  cleanup_process
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
    kill -0 "$current_pid" 2>/dev/null || rdma_die "workset process exited; see $log"
    [ "$(( $(date +%s) - start ))" -lt "$timeout" ] || rdma_die "timeout waiting for $pattern; see $log"
    sleep "$poll"
  done
}

log_field() {
  local record=$1 key=$2 log=$3
  awk -v record="$record" -v key="$key" '
    $1 == record {
      for (i = 2; i <= NF; i++) {
        split($i, pair, "=")
        if (pair[1] == key)
          value = pair[2]
      }
    }
    END { print value }
  ' "$log"
}

[[ "$WORKSET_MB" =~ ^[0-9]+$ ]] && [ "$WORKSET_MB" -gt 0 ] || rdma_die "invalid WORKSET_MB"
[[ "$LIMIT_MB" =~ ^[0-9]+$ ]] && [ "$LIMIT_MB" -gt 0 ] && [ "$LIMIT_MB" -lt "$WORKSET_MB" ] || rdma_die "LIMIT_MB must be between 1 and WORKSET_MB-1"
[[ "$ACCESS_SEED" =~ ^[0-9]+$ ]] || rdma_die "ACCESS_SEED must be an unsigned integer"
[[ "$BENCH_THREADS" =~ ^[0-9]+$ ]] && [ "$BENCH_THREADS" -gt 0 ] || \
  rdma_die "BENCH_THREADS must be a positive integer"
command -v taskset >/dev/null 2>&1 || rdma_die "taskset is required"
taskset -c "$BENCH_CPUS" true >/dev/null 2>&1 || \
  rdma_die "invalid or unavailable BENCH_CPUS CPU list: $BENCH_CPUS"
case "$SWAPOUT_TRIGGER" in
  memory-max|parallel-fault) ;;
  *) rdma_die "SWAPOUT_TRIGGER must be memory-max or parallel-fault" ;;
esac
for access_ratio in $ACCESS_RATIOS; do
  [[ "$access_ratio" =~ ^([0-9]+([.][0-9]+)?|1p)$ ]] || \
    rdma_die "invalid access ratio: $access_ratio"
  if [ "$access_ratio" != 1p ]; then
    awk -v ratio="$access_ratio" 'BEGIN { exit !(ratio > 0 && ratio <= 100) }' || \
      rdma_die "access ratio must be in (0, 100]: $access_ratio"
  fi
done
for access_order in $ACCESS_ORDERS; do
  case "$access_order" in sequential|random) ;; *) rdma_die "invalid access order: $access_order" ;; esac
done
for access_locality in $ACCESS_LOCALITIES; do
  case "$access_locality" in high|low) ;; *) rdma_die "invalid access locality: $access_locality" ;; esac
done
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

required_swap_mb=$((WORKSET_MB - LIMIT_MB))
free_swap_mb=$(awk 'NR > 1 { free += $3 - $4 } END { printf "%d", free / 1024 }' /proc/swaps)
[ "$free_swap_mb" -ge "$required_swap_mb" ] || \
  rdma_die "only ${free_swap_mb} MiB swap is free; at least ${required_swap_mb} MiB is required"

if [ ! -x "$BIN" ] || [ "$SRC" -nt "$BIN" ]; then
  rdma_log "building $BIN"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror -std=c11 -pthread -o "$BIN" "$SRC"
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
  printf 'mode=%s\nworkset_mb=%s\nlimit_mb=%s\n' "$MODE" "$WORKSET_MB" "$LIMIT_MB"
  printf 'page_sizes_kb=%s\nrepeats=%s\nbench_threads=%s\nbench_cpus=%s\nswapout_trigger=%s\n' \
    "$PAGE_SIZES_KB" "$BENCH_REPEATS" "$BENCH_THREADS" "$BENCH_CPUS" \
    "$SWAPOUT_TRIGGER"
  printf 'access_ratios=%s\naccess_orders=%s\naccess_localities=%s\naccess_seed=%s\n' \
    "$ACCESS_RATIOS" "$ACCESS_ORDERS" "$ACCESS_LOCALITIES" "$ACCESS_SEED"
  printf 'backend=%s\n' "$(detect_rswap_backend)"
  printf 'swap:\n'
  sed 's/^/  /' /proc/swaps
} > "$RESULT_DIR/environment.txt"
printf 'page_kb,order,repeat,bench_threads,swapout_trigger,access_ratio,actual_access_pct,access_order,access_locality,access_seed,pages_per_folio,accessed_pages,accessed_bytes,workset_mb,limit_mb,populate_sec,anon_huge_kb,memory_max_write_ms,completion_ms,pswpout_delta,target_stores_delta,target_loads_delta,target_fallback_delta,target_errors_delta,total_store_bytes,large_store_bytes,large_store_pct,swapout_gib,protocol_gib,protocol_gib_per_sec,measured_write_amplification,swapin_scan_sec,swapin_wall_ms,swapin_pswpin_delta,swapin_pswpout_delta,swapin_target_loads_delta,swapin_target_fallback_delta,swapin_target_errors_delta,total_load_bytes,large_load_bytes,large_load_pct,swapin_gib,load_protocol_gib,load_protocol_gib_per_sec,workset_scan_gib_per_sec,accessed_scan_gib_per_sec,load_to_accessed_ratio,expected_read_amplification,measured_read_amplification,checksum_errors\n' > "$CSV"
page_count=$(awk '{ print NF }' <<< "$PAGE_SIZES_KB")
ratio_count=$(awk '{ print NF }' <<< "$ACCESS_RATIOS")
order_count=$(awk '{ print NF }' <<< "$ACCESS_ORDERS")
locality_count=$(awk '{ print NF }' <<< "$ACCESS_LOCALITIES")
rdma_log "planned runs: $((page_count * ratio_count * order_count * locality_count * BENCH_REPEATS)); threads=$BENCH_THREADS cpus=$BENCH_CPUS swapout_trigger=$SWAPOUT_TRIGGER"

for kb in $PAGE_SIZES_KB; do
  order=$(page_order "$kb") || rdma_die "unsupported page size: ${kb} KiB"
  configure_page_size "$kb" "$order" || rdma_die "THP size ${kb} KiB is unavailable"
  effective=$(sudo_cat "$EFFECTIVE_MASK_FILE")
  (( (effective & (1 << order)) != 0 )) || rdma_die "effective mask $effective lacks order $order"

  for access_ratio in $ACCESS_RATIOS; do
    ratio_label=${access_ratio//./p}
    for access_order in $ACCESS_ORDERS; do
      for access_locality in $ACCESS_LOCALITIES; do
        for repeat in $(seq 1 "$BENCH_REPEATS"); do
    access_seed=$ACCESS_SEED
    run_dir="$RESULT_DIR/${kb}k/${ratio_label}/${access_order}-${access_locality}/r${repeat}"
    log="$run_dir/workset.log"
    before="$run_dir/order-before.txt"
    after="$run_dir/order-after.txt"
    mkdir -p "$run_dir"
    sudo_write max "$CGROUP/memory.max"
    advice=huge
    [ "$kb" != 4 ] || advice=base

    taskset -c "$BENCH_CPUS" "$BIN" "$WORKSET_MB" "$advice" "$kb" \
      "$access_ratio" "$access_order" "$access_locality" "$access_seed" \
      "$BENCH_THREADS" > "$log" 2>&1 &
    current_pid=$!
    sudo_write "$current_pid" "$CGROUP/cgroup.procs"

    if [ "$SWAPOUT_TRIGGER" = parallel-fault ]; then
      # Put the cgroup under its final limit before the worker threads fault
      # the mapping. Their concurrent allocations then enter direct reclaim
      # and submit stores in parallel instead of leaving reclaim to the one
      # task which writes memory.max.
      limit_write_start_ns=$(date +%s%N)
      sudo_write "$((LIMIT_MB * 1024 * 1024))" "$CGROUP/memory.max"
      write_end_ns=$(date +%s%N)
      wait_for_stores_quiet
      snapshot_order_stats > "$before"
      pswpout_before=$(read_vmstat_key pswpout)
      stores_before=$(order_total_stores)
      oom_before=$(memory_event oom_kill)
      sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-before.txt"
      start_ns=$(date +%s%N)
    fi

    kill -USR1 "$current_pid"
    wait_for_log '^READY ' "$log" "$READY_TIMEOUT_SEC" 0.01
    ready_ns=$(date +%s%N)
    populate_sec=$(log_field READY populate_sec "$log")
    actual_threads=$(log_field READY threads "$log")
    pages_per_folio=$(log_field READY pages_per_folio "$log")
    accessed_pages=$(log_field READY accessed_pages "$log")
    accessed_bytes=$(log_field READY accessed_bytes "$log")
    actual_access_pct=$(log_field READY actual_access_pct "$log")
    [ "$actual_threads" = "$BENCH_THREADS" ] && [ -n "$populate_sec" ] && \
      [ -n "$pages_per_folio" ] && \
      [ -n "$accessed_pages" ] && [ -n "$accessed_bytes" ] && \
      [ -n "$actual_access_pct" ] || rdma_die "missing READY metrics; see $log"
    sudo_cat "/proc/$current_pid/smaps_rollup" > "$run_dir/smaps-rollup-before.txt"
    anon_huge_kb=$(awk '/^AnonHugePages:/ { print $2 }' "$run_dir/smaps-rollup-before.txt")
    resident_bytes=$(sudo_cat "$CGROUP/memory.current")

    if [ "$SWAPOUT_TRIGGER" = parallel-fault ]; then
      minimum_resident_bytes=$((LIMIT_MB * 1024 * 1024 * 8 / 10))
      last_change_ns=$ready_ns
    else
      sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-before.txt"
      minimum_resident_bytes=$((WORKSET_MB * 1024 * 1024 * 9 / 10))
    fi
    [ "$resident_bytes" -ge "$minimum_resident_bytes" ] || \
      rdma_die "only $((resident_bytes / 1024 / 1024)) MiB is resident; expected at least $((minimum_resident_bytes / 1024 / 1024)) MiB"

    if [ "$SWAPOUT_TRIGGER" = memory-max ]; then
      wait_for_stores_quiet
      snapshot_order_stats > "$before"
      pswpout_before=$(read_vmstat_key pswpout)
      stores_before=$(order_total_stores)
      oom_before=$(memory_event oom_kill)
      start_ns=$(date +%s%N)
      limit_write_start_ns=$start_ns
      sudo_write "$((LIMIT_MB * 1024 * 1024))" "$CGROUP/memory.max"
      write_end_ns=$(date +%s%N)
      last_change_ns=$write_end_ns
    fi

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
    [ "$oom_after" -eq "$oom_before" ] || rdma_die "cgroup OOM killed the workset process; see $run_dir"
    kill -0 "$current_pid" 2>/dev/null || rdma_die "workset process exited during reclaim; see $run_dir"

    memory_max_write_ms=$(( (write_end_ns - limit_write_start_ns) / 1000000 ))
    completion_ms=$(( (last_change_ns - start_ns) / 1000000 ))
    [ "$completion_ms" -gt 0 ] || completion_ms=1
    target_stores=$(( $(order_field "$after" "$order" 3) - $(order_field "$before" "$order" 3) ))
    target_loads=$(( $(order_field "$after" "$order" 4) - $(order_field "$before" "$order" 4) ))
    target_fallback=$(( $(order_field "$after" "$order" 5) - $(order_field "$before" "$order" 5) ))
    target_errors=$(( $(order_field "$after" "$order" 6) - $(order_field "$before" "$order" 6) ))
    pswpout_delta=$((pswpout_after - pswpout_before))
    stores_after=$(order_total_stores)
    stores_delta=$((stores_after - stores_before))

    metrics=$(awk -v before="$before" -v after="$after" -v ms="$completion_ms" '
      FILENAME == before && NR > 1 { b[$1]=$3; fb[$1]=$5; size[$1]=$2; next }
      FILENAME == after && FNR > 1 {
        d=$3-b[$1]; f=$5-fb[$1];
        if (f < 0) f=0; if (f > d) f=d;
        total += d*$2; if ($1 > 0) large += (d-f)*$2
      }
      END {
        pct=total ? 100*large/total : 0;
        gib=total/1073741824;
        printf "%.0f,%.0f,%.3f,%.6f", total, large, pct, gib/(ms/1000)
      }' "$before" "$after")
    total_store_bytes=${metrics%%,*}
    rest=${metrics#*,}; large_store_bytes=${rest%%,*}
    rest=${rest#*,}; large_store_pct=${rest%%,*}
    protocol_gib_per_sec=${rest#*,}
    swapout_gib=$(awk -v p="$pswpout_delta" 'BEGIN { printf "%.6f", p*4096/1073741824 }')
    protocol_gib=$(awk -v b="$total_store_bytes" 'BEGIN { printf "%.6f", b/1073741824 }')
    measured_write_amplification=$(awk -v protocol_bytes="$total_store_bytes" \
      -v swapped_pages="$pswpout_delta" \
      'BEGIN { printf "%.6f", (swapped_pages > 0 ? protocol_bytes / (swapped_pages * 4096) : 0) }')

    # Restore headroom before the sparse scan. Otherwise each swap-in could
    # force another swap-out and measure memcg thrashing instead of loads.
    sudo_write max "$CGROUP/memory.max"
    load_before="$run_dir/order-before-swapin.txt"
    load_after="$run_dir/order-after-swapin.txt"
    snapshot_order_stats > "$load_before"
    pswpin_before=$(read_vmstat_key pswpin)
    scan_pswpout_before=$(read_vmstat_key pswpout)
    swapin_start_ns=$(date +%s%N)
    kill -USR2 "$current_pid"
    wait_for_log '^SCAN ' "$log" "$SWAPIN_TIMEOUT_SEC" 0.01
    swapin_end_ns=$(date +%s%N)
    snapshot_order_stats > "$load_after"
    pswpin_after=$(read_vmstat_key pswpin)
    scan_pswpout_after=$(read_vmstat_key pswpout)
    sudo_cat "/proc/$current_pid/smaps_rollup" > "$run_dir/smaps-rollup-after-swapin.txt"
    sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-after-swapin.txt"
    sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after-swapin.txt"

    swapin_scan_sec=$(log_field SCAN sec "$log")
    scan_accessed_pages=$(log_field SCAN accessed_pages "$log")
    scan_accessed_bytes=$(log_field SCAN accessed_bytes "$log")
    checksum_errors=$(log_field SCAN checksum_errors "$log")
    [ -n "$swapin_scan_sec" ] || rdma_die "missing SCAN duration; see $log"
    [ "$scan_accessed_pages" = "$accessed_pages" ] && \
      [ "$scan_accessed_bytes" = "$accessed_bytes" ] || \
      rdma_die "READY/SCAN access metrics differ; see $log"
    [ "${checksum_errors:-1}" -eq 0 ] || rdma_die "workset checksum mismatch; see $log"
    swapin_wall_ms=$(( (swapin_end_ns - swapin_start_ns) / 1000000 ))
    swapin_pswpin_delta=$((pswpin_after - pswpin_before))
    swapin_pswpout_delta=$((scan_pswpout_after - scan_pswpout_before))
    swapin_target_loads=$(( $(order_field "$load_after" "$order" 4) - $(order_field "$load_before" "$order" 4) ))
    swapin_target_fallback=$(( $(order_field "$load_after" "$order" 5) - $(order_field "$load_before" "$order" 5) ))
    swapin_target_errors=$(( $(order_field "$load_after" "$order" 6) - $(order_field "$load_before" "$order" 6) ))
    if ! load_metrics=$(awk -v before="$load_before" -v after="$load_after" -v sec="$swapin_scan_sec" '
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
    swapin_gib=$(awk -v p="$swapin_pswpin_delta" 'BEGIN { printf "%.6f", p*4096/1073741824 }')
    load_protocol_gib=$(awk -v b="$total_load_bytes" 'BEGIN { printf "%.6f", b/1073741824 }')
    workset_scan_gib_per_sec=$(awk -v mib="$WORKSET_MB" -v sec="$swapin_scan_sec" \
      'BEGIN { printf "%.6f", (sec > 0 ? mib / 1024 / sec : 0) }')
    accessed_scan_gib_per_sec=$(awk -v bytes="$accessed_bytes" -v sec="$swapin_scan_sec" \
      'BEGIN { printf "%.6f", (sec > 0 ? bytes / 1073741824 / sec : 0) }')
    load_to_accessed_ratio=$(awk -v load_bytes="$total_load_bytes" \
      -v accessed_bytes="$accessed_bytes" \
      'BEGIN { printf "%.6f", (accessed_bytes > 0 ? load_bytes / accessed_bytes : 0) }')
    expected_read_amplification=$(awk -v pct="$actual_access_pct" \
      'BEGIN { printf "%.6f", (pct > 0 ? 100 / pct : 0) }')
    measured_read_amplification=$(awk -v load_bytes="$total_load_bytes" \
      -v accessed_bytes="$accessed_bytes" -v store_bytes="$total_store_bytes" \
      -v workset_mb="$WORKSET_MB" '
      BEGIN {
        workset = workset_mb * 1024 * 1024
        amplification = 0
        if (accessed_bytes > 0 && store_bytes > 0)
          amplification = load_bytes * workset / (accessed_bytes * store_bytes)
        printf "%.6f", amplification
      }')
    oom_after_swapin=$(memory_event oom_kill)
    [ "$oom_after_swapin" -eq "$oom_before" ] || rdma_die "cgroup OOM during swap-in; see $run_dir"
    [ "$swapin_pswpin_delta" -gt 0 ] || rdma_die "scan caused no swap-ins; see $run_dir"
    [[ "$total_load_bytes" =~ ^[0-9]+$ ]] && [ "$total_load_bytes" -gt 0 ] || \
      rdma_die "Hermit recorded no backend loads; see $run_dir"
    [ "$swapin_pswpout_delta" -eq 0 ] || \
      rdma_die "swap-in caused $swapin_pswpout_delta new swap-outs; results are not isolated"

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$kb" "$order" "$repeat" "$BENCH_THREADS" "$SWAPOUT_TRIGGER" \
      "$access_ratio" "$actual_access_pct" \
      "$access_order" "$access_locality" "$access_seed" "$pages_per_folio" \
      "$accessed_pages" "$accessed_bytes" "$WORKSET_MB" "$LIMIT_MB" "$populate_sec" "$anon_huge_kb" \
      "$memory_max_write_ms" "$completion_ms" "$pswpout_delta" "$target_stores" \
      "$target_loads" "$target_fallback" "$target_errors" "$total_store_bytes" \
      "$large_store_bytes" "$large_store_pct" "$swapout_gib" "$protocol_gib" \
      "$protocol_gib_per_sec" "$measured_write_amplification" \
      "$swapin_scan_sec" "$swapin_wall_ms" \
      "$swapin_pswpin_delta" "$swapin_pswpout_delta" "$swapin_target_loads" \
      "$swapin_target_fallback" "$swapin_target_errors" "$total_load_bytes" \
      "$large_load_bytes" "$large_load_pct" "$swapin_gib" "$load_protocol_gib" \
      "$load_protocol_gib_per_sec" "$workset_scan_gib_per_sec" \
      "$accessed_scan_gib_per_sec" "$load_to_accessed_ratio" \
      "$expected_read_amplification" "$measured_read_amplification" \
      "$checksum_errors" >> "$CSV"
    rdma_log "page=${kb}k ratio=$access_ratio actual=${actual_access_pct}% order=$access_order locality=$access_locality repeat=$repeat threads=$BENCH_THREADS trigger=$SWAPOUT_TRIGGER swapout=${protocol_gib_per_sec}GiB/s write_amp=${measured_write_amplification} stores=$stores_delta store_large=${large_store_pct}% swapin=${load_protocol_gib_per_sec}GiB/s read_amp=${measured_read_amplification} loads=$swapin_target_loads load_large=${large_load_pct}% checksum_errors=$checksum_errors fallback=$swapin_target_fallback errors=$swapin_target_errors"
    cleanup_process
        done
      done
    done
  done
done

trap - EXIT INT TERM
cleanup
