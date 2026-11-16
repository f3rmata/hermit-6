#!/usr/bin/env bash
#
# Swap-in granularity microbenchmark: how expensive is one demand fault as a
# function of the accessed extent (S pages at a random 2 MiB-aligned start)?
#
# For every THP allocation ceiling and every policy mode the driver maps
# SIZE_GIB anonymous memory in a cgroup, populates it, lowers memory.max so
# most of the mapping is swapped out to the Hermit backend, and then lets
# swapin_granularity touch S-page segments. Per trial it records wall time,
# faults and a data check; per cell it diffs the kernel's per-order swap-in
# cost table (/sys/kernel/debug/hermit/swapin_cost_stats) and the backend
# load counters.
#
# usage:
#   sudo ./run_swapin_granularity.sh [options]
#
# Options (or the matching environment variables):
#   --pages "4 64 2048"        PAGE_SIZES_KB
#   --policy-features "0x1bf"  POLICY_FEATURES  ("off" adds a PEBS-disabled cell;
#                              also accepts default/no-pebs/no-batch and commas)
#   --segments "1 4 16 32 64 128"  SEGMENT_PAGES
#   --trials N                 TRIALS
#   --size-gib N               SIZE_GIB
#   --ratio PCT                LOCAL_RATIO_PCT
#   --result-dir PATH          RESULT_DIR
#   --no-cost                  do not enable the kernel cost counters
#
# Requirements: cgroup v2, active Hermit RDMA backend, debugfs, zswap disabled,
# exactly one block-device swap area, a compiler, taskset. The driver owns the
# cgroup /sys/fs/cgroup/hermit-swapin-granularity.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=../sweep_common.sh
source "$SCRIPT_DIR/../sweep_common.sh"

usage() {
  sed -n '3,36p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

SIZE_GIB=${SIZE_GIB:-4}
SEGMENT_PAGES=${SEGMENT_PAGES:-"1 4 16 32 64 128"}
TRIALS=${TRIALS:-5}
PAGE_SIZES_KB=${PAGE_SIZES_KB:-"4 64 2048"}
POLICY_FEATURES=${POLICY_FEATURES:-"off 0x1bf"}
LOCAL_RATIO_PCT=${LOCAL_RATIO_PCT:-60}
BENCH_CPU=${BENCH_CPU:-0}
SWAPOUT_TRIGGER=${SWAPOUT_TRIGGER:-cgroup}
RUN_TIMEOUT_SEC=${RUN_TIMEOUT_SEC:-1800}
READY_TIMEOUT_SEC=${READY_TIMEOUT_SEC:-300}
COST_STATS=${COST_STATS:-1}
CGROUP=${PEBS_CGROUP:-/sys/fs/cgroup/hermit-swapin-granularity}
current_pid=
bin=${SWAPIN_GRANULARITY_BIN:-}
policy_stats_saved=
cost_enabled_saved=

while [ $# -gt 0 ]; do
  case "$1" in
    --pages) PAGE_SIZES_KB=$2; shift 2 ;;
    --policy-features) POLICY_FEATURES=$2; shift 2 ;;
    --segments) SEGMENT_PAGES=$2; shift 2 ;;
    --trials) TRIALS=$2; shift 2 ;;
    --size-gib) SIZE_GIB=$2; shift 2 ;;
    --ratio) LOCAL_RATIO_PCT=$2; shift 2 ;;
    --result-dir) RESULT_DIR=$2; shift 2 ;;
    --no-cost) COST_STATS=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) rdma_die "unknown argument: $1" ;;
  esac
done

RESULT_DIR=${RESULT_DIR:-"$PWD/tools/rdma/results/$(date +%Y%m%d-%H%M%S)-swapin-granularity"}
POLICY_FEATURES=${POLICY_FEATURES//,/ }
SEGMENT_PAGES=${SEGMENT_PAGES//,/ }
TRIALS_CSV="$RESULT_DIR/swapin-granularity.csv"
COST_CSV="$RESULT_DIR/swapin-cost.csv"

[[ "$SIZE_GIB" =~ ^[1-9][0-9]*$ ]] || rdma_die "SIZE_GIB must be a positive integer"
[[ "$TRIALS" =~ ^[1-9][0-9]*$ ]] || rdma_die "TRIALS must be a positive integer"
[[ "$LOCAL_RATIO_PCT" =~ ^[1-9][0-9]?$ ]] || rdma_die "LOCAL_RATIO_PCT must be 1-99"
case "$SWAPOUT_TRIGGER" in cgroup|madv-pageout) ;; *) rdma_die "SWAPOUT_TRIGGER must be cgroup or madv-pageout" ;; esac
for page in $SEGMENT_PAGES; do
  [[ "$page" =~ ^[1-9][0-9]*$ ]] && [ "$page" -le 512 ] || \
    rdma_die "segment size $page must be 1-512 pages (2 MiB region)"
done

validate_sweep_host
save_thp_state

compile_bench() {
  local source="$SCRIPT_DIR/swapin_granularity.c"
  local target

  target="${TMPDIR:-/tmp}/hermit-swapin-granularity-$(id -u)"
  if [ -n "$bin" ] && [ -x "$bin" ]; then
    return 0
  fi
  bin=$target
  [ -r "$source" ] || rdma_die "missing $source"
  if [ ! -x "$bin" ] || [ "$source" -nt "$bin" ]; then
    command -v "${CC:-cc}" >/dev/null 2>&1 || rdma_die "a C compiler is required (set CC)"
    rdma_log "building $bin"
    "${CC:-cc}" -std=gnu11 -O2 -Wall -Wextra -Werror -Wno-unused-parameter \
      -o "$bin" "$source"
  fi
}

cleanup_process() {
  if [ -n "$current_pid" ] && kill -0 "$current_pid" 2>/dev/null; then
    kill -TERM "$current_pid" 2>/dev/null || true
    wait "$current_pid" 2>/dev/null || true
  fi
  current_pid=
  [ -z "${CGROUP:-}" ] || sudo_write max "$CGROUP/memory.max" 2>/dev/null || true
}

restore_controls() {
  if [ -n "$policy_stats_saved" ] && sudo_test -e /sys/kernel/debug/hermit/policy_features; then
    sudo_write "$policy_stats_saved" /sys/kernel/debug/hermit/policy_features || true
  fi
  if [ -n "$cost_enabled_saved" ] && sudo_test -e "$COST_ENABLE_FILE"; then
    sudo_write "$cost_enabled_saved" "$COST_ENABLE_FILE" || true
  fi
}

cleanup() {
  local ret=$?
  cleanup_process
  restore_controls
  restore_thp_state
  sudo rmdir "$CGROUP" 2>/dev/null || true
  exit "$ret"
}

wait_for_log() {
  local pattern=$1 log=$2 timeout=$3 start
  start=$(date +%s)
  until grep -q "$pattern" "$log" 2>/dev/null; do
    if [ -n "$current_pid" ] && ! kill -0 "$current_pid" 2>/dev/null; then
      rdma_die "benchmark process exited before $pattern; see $log"
    fi
    [ "$(( $(date +%s) - start ))" -lt "$timeout" ] || \
      rdma_die "timeout waiting for $pattern; see $log"
    sleep 1
  done
}

snapshot_cost() {
  local out=$1
  if sudo_test -r "$COST_STATS_FILE"; then
    sudo_cat "$COST_STATS_FILE" > "$out"
  else
    printf 'enabled 0\n' > "$out"
  fi
}

cost_columns() {
  local before=$1 after=$2 key value
  while read -r key; do
    if value=$(cost_delta "$before" "$after" "$key"); then :; else value=0; fi
    printf ',%s' "$value"
  done < <(cost_keys)
}

emit_trials() {
  local log=$1 page_kb=$2 mode=$3 mask=$4
  awk -v kb="$page_kb" -v mode="$mode" -v mask="$mask" '
    /^TRIAL / {
      seg=""; trial=""; start=""; bytes=""; sec=""; gib=""; minflt=""; majflt=""; err=""
      for (i = 2; i <= NF; i++) {
        split($i, kv, "=")
        if (kv[1] == "seg") seg = kv[2]
        else if (kv[1] == "trial") trial = kv[2]
        else if (kv[1] == "start_page") start = kv[2]
        else if (kv[1] == "bytes") bytes = kv[2]
        else if (kv[1] == "sec") sec = kv[2]
        else if (kv[1] == "gib_per_sec") gib = kv[2]
        else if (kv[1] == "minflt") minflt = kv[2]
        else if (kv[1] == "majflt") majflt = kv[2]
        else if (kv[1] == "checksum_errors") err = kv[2]
      }
      printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n",
             kb, mode, mask, seg, trial, start, bytes, sec, gib, minflt, majflt, err
    }' "$log"
}

trial_summary() {
  awk -v seg="$1" '
    /^TRIAL / {
      s=""; mi=""; ma=""; er=""; sec=""; bytes=""
      for (i = 2; i <= NF; i++) {
        split($i, kv, "=")
        if (kv[1] == "seg") s = kv[2]
        else if (kv[1] == "minflt") mi = kv[2]
        else if (kv[1] == "majflt") ma = kv[2]
        else if (kv[1] == "checksum_errors") er = kv[2]
        else if (kv[1] == "sec") sec = kv[2]
        else if (kv[1] == "bytes") bytes = kv[2]
      }
      if (s + 0 != seg + 0) next
      faults += mi + ma; errors += er; seconds += sec; total += bytes
    }
    END { printf "%d,%d,%.6f,%d\n", faults, errors, seconds, total }' "$2"
}

compile_bench
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$RESULT_DIR"

# Save, then own, the two Hermit controls this benchmark needs.
if sudo_test -e /sys/kernel/debug/hermit/policy_features; then
  policy_stats_saved=$(sudo_cat /sys/kernel/debug/hermit/policy_features)
fi
if [ "$COST_STATS" = 1 ] && sudo_test -e "$COST_ENABLE_FILE"; then
  cost_enabled_saved=$(sudo_cat "$COST_ENABLE_FILE")
  sudo_write 1 "$COST_ENABLE_FILE"
fi
if [ "$COST_STATS" = 1 ] && ! sudo_test -r "$COST_STATS_FILE"; then
  rdma_log "the running kernel has no swapin_cost_stats; per-order cost columns stay zero"
  COST_STATS=0
fi
prepare_sweep_cgroup

{
  printf 'date=%s\n' "$(date --iso-8601=seconds)"
  printf 'kernel=%s\n' "$(uname -r)"
  printf 'size_gib=%s\ntrials=%s\nsegments=%s\n' "$SIZE_GIB" "$TRIALS" "$SEGMENT_PAGES"
  printf 'page_sizes_kb=%s\npolicy_features=%s\n' "$PAGE_SIZES_KB" "$POLICY_FEATURES"
  printf 'local_ratio_pct=%s\nswapout_trigger=%s\ncost_stats=%s\n' \
    "$LOCAL_RATIO_PCT" "$SWAPOUT_TRIGGER" "$COST_STATS"
  printf 'bench_cpu=%s\n' "$BENCH_CPU"
  printf 'backend=%s\n' "$(detect_rswap_backend)"
} > "$RESULT_DIR/environment.txt"

printf 'page_kb,mode,policy_features,segment_pages,trial,start_page,bytes,sec,gib_per_sec,minflt,majflt,checksum_errors\n' > "$TRIALS_CSV"
{ printf 'page_kb,mode,policy_features'; while read -r key; do printf ',%s' "$key"; done < <(cost_keys); printf '\n'; } > "$COST_CSV"

for kb in $PAGE_SIZES_KB; do
  page_order "$kb" >/dev/null || rdma_die "unsupported page size: ${kb} KiB"
  configure_page_size "$kb" || rdma_die "THP size ${kb} KiB is unavailable"
  for feature in $POLICY_FEATURES; do
    if [ "$feature" = off ]; then
      mode=off; mask=
    else
      mask=$(policy_mask_of "$feature")
      valid_policy_mask "$mask" || rdma_die "invalid policy_features mask: $feature"
      [ -n "$policy_stats_saved" ] || rdma_die "the running kernel has no policy_features control"
      sudo_write "$mask" /sys/kernel/debug/hermit/policy_features
      mode=policy
    fi
    run_dir="$RESULT_DIR/${kb}k/$mode${mask:+-$mask}"
    log="$run_dir/bench.log"
    mkdir -p "$run_dir"
    # Each cell starts from a stopped sampler: an "off" cell must not inherit
    # the PEBS session of the previous policy cell.
    stop_pebs_session
    if [ "$mode" = policy ]; then
      export PEBS_BENCH_MODE=policy
    else
      export PEBS_BENCH_MODE=off
    fi
    sudo_write max "$CGROUP/memory.max"
    args=(--size-gib "$SIZE_GIB" --segments="${SEGMENT_PAGES// /,}" --trials "$TRIALS")
    if [ "$SWAPOUT_TRIGGER" = madv-pageout ]; then
      args+=(--madv-pageout)
    fi
    if [ "$COST_STATS" != 1 ]; then
      args+=(--no-cost)
    fi
    taskset -c "$BENCH_CPU" bash -c 'echo $$ | sudo -n tee "$1/cgroup.procs" >/dev/null && shift && exec "$@"' \
      swapin-granularity "$CGROUP" "$bin" "${args[@]}" > "$log" 2>&1 &
    current_pid=$!
    # Both triggers use the same handshake: READY after the mapping exists
    # (and, for --madv-pageout, after the pages were forced out), then SIGUSR1.
    wait_for_log '^READY ' "$log" "$READY_TIMEOUT_SEC"
    enable_benchmark_pebs_pid "$current_pid"
    if [ "$SWAPOUT_TRIGGER" = cgroup ]; then
      resident_bytes=$(sudo_cat "$CGROUP/memory.current")
      resident_mb=$((resident_bytes / 1024 / 1024))
      limit_mb=$(benchmark_memory_limit_mb "$resident_mb" "$LOCAL_RATIO_PCT")
      [ "$limit_mb" -lt "$resident_mb" ] || \
        rdma_die "computed limit ${limit_mb} MiB is not below resident ${resident_mb} MiB"
      check_swap_capacity "$((resident_mb - limit_mb))"
      wait_for_stores_quiet
      snapshot_cost "$run_dir/cost-before.txt"
      snapshot_sweep_stats "$run_dir/stats-before.txt"
      pswpin_before=$(read_vmstat_key pswpin)
      sudo_write "$((limit_mb * 1024 * 1024))" "$CGROUP/memory.max"
      wait_for_stores_quiet "$(date +%s%N)"
      # Counters below are sampled around the read phase only.
      snapshot_cost "$run_dir/cost-before-swapin.txt"
      snapshot_order_stats > "$run_dir/order-before-swapin.txt"
    else
      pswpin_before=$(read_vmstat_key pswpin)
      snapshot_cost "$run_dir/cost-before-swapin.txt"
      snapshot_order_stats > "$run_dir/order-before-swapin.txt"
    fi
    kill -USR1 "$current_pid"
    wait_for_log '^DONE ' "$log" "$RUN_TIMEOUT_SEC"
    snapshot_cost "$run_dir/cost-after-swapin.txt"
    snapshot_order_stats > "$run_dir/order-after-swapin.txt"
    pswpin_after=$(read_vmstat_key pswpin)
    if [ "$SWAPOUT_TRIGGER" = cgroup ]; then
      sudo_cat "/proc/$current_pid/smaps_rollup" > "$run_dir/smaps-rollup-after.txt"
      snapshot_fixed_memory_budget "$CGROUP" "$run_dir" after-swapin "$((limit_mb * 1024 * 1024))"
    fi
    kill -USR2 "$current_pid" 2>/dev/null || true
    wait "$current_pid" || rdma_die "benchmark exited with an error; see $log"
    current_pid=

    pswpin_delta=$((pswpin_after - pswpin_before))
    loads_total_before=$(awk 'NR > 1 { t += $4 } END { print t + 0 }' "$run_dir/order-before-swapin.txt")
    loads_total_after=$(awk 'NR > 1 { t += $4 } END { print t + 0 }' "$run_dir/order-after-swapin.txt")
    loads_delta=$((loads_total_after - loads_total_before))
    emit_trials "$log" "$kb" "$mode" "${mask:-none}" >> "$TRIALS_CSV"
    printf '%s,%s,%s' "$kb" "$mode" "${mask:-none}" >> "$COST_CSV"
    if [ "$COST_STATS" = 1 ]; then
      cost_columns "$run_dir/cost-before-swapin.txt" "$run_dir/cost-after-swapin.txt" >> "$COST_CSV"
    else
      while read -r _key; do printf ',0'; done < <(cost_keys) >> "$COST_CSV"
    fi
    printf '\n' >> "$COST_CSV"
    [ "$pswpin_delta" -gt 0 ] || rdma_die "no swap-ins in ${kb}k/$mode; lower LOCAL_RATIO_PCT"
    [ "$loads_delta" -gt 0 ] || rdma_die "no backend loads recorded for ${kb}k/$mode; see $run_dir"
    for seg in $SEGMENT_PAGES; do
      summary=$(trial_summary "$seg" "$log")
      IFS=, read -r faults errors seconds bytes <<< "$summary"
      [ -n "$seconds" ] || rdma_die "missing trial timing for ${kb}k/$mode seg=$seg; see $log"
      [ "$errors" -eq 0 ] || rdma_die "data check failed for ${kb}k/$mode seg=$seg; see $log"
      [ "$faults" -gt 0 ] || rdma_die "segment $seg caused no faults in ${kb}k/$mode; the pages were not swapped out"
      [ "$bytes" -gt 0 ] || rdma_die "segment $seg read no bytes in ${kb}k/$mode; see $log"
    done
    rdma_log "page=${kb}k mode=$mode mask=${mask:-none} pswpin=$pswpin_delta backend_loads=$loads_delta"
  done
done

trap - EXIT INT TERM
cleanup
