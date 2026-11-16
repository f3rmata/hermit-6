#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
requested_mode=${MODE-}
requested_run_id=${RUN_ID-}
requested_result_dir=${RESULT_DIR-}
# shellcheck source=sweep_common.sh
source "$SCRIPT_DIR/sweep_common.sh"

usage() {
  cat <<'USAGE'
usage:
  MODE=cgroup-hermit GAPBS_DIR=/path/to/gapbs GAPBS_KERNEL=bfs \
    ./run_gapbs_page_sweep.sh

GAPBS graph-kernel page-size sweep under Hermit. For every maximum page size
the script builds an unmodified GAPBS kernel with gapbs_hermit_hook.h, lets it
generate the graph in a cgroup, lowers memory.max so part of the graph is
swapped out, keeps that fixed limit, and runs the kernel's timed trials while
allowing concurrent reclamation. Every trial must pass GAPBS verification.

Important environment:
  PAGE_SIZES_KB="4 16 32 64 128 256 512 1024 2048"
    Each value is a maximum allocation size; smaller orders remain eligible.
  GAPBS_DIR=                         (GAPBS checkout containing src/; required)
  GAPBS_KERNEL=bfs                   (bfs or pr)
  GAPBS_SCALE=24                     (Kronecker graph with 2^scale vertices)
  GAPBS_DEGREE=16                    (average degree)
  GAPBS_TRIALS=4                     (timed trials, each verified)
  GAPBS_PR_ITERS=20                  (PageRank only: maximum iterations)
  GAPBS_THREADS=8                    (OpenMP threads)
  BENCH_CPUS=0-7                     (taskset CPU list)
  LOCAL_RATIO_PCT=70                 (limit = resident MiB * ratio / 100)
  MEMORY_LIMIT_MB=                   (absolute limit shared across comparison cases)
  BENCH_REPEATS=3
  QUIET_INTERVAL_SEC=1
  QUIET_SAMPLES=3
  QUIET_TIMEOUT_SEC=120
  READY_TIMEOUT_SEC=900
  RUN_TIMEOUT_SEC=3600
  RESULT_DIR=tools/rdma/results/<run-id>
  RESTORE_THP=1

Requirements: cgroup v2, active Hermit RDMA backend, debugfs, zswap disabled,
g++ with OpenMP, and exactly one block-device swap area. The script owns
/sys/fs/cgroup/hermit-gapbs.
USAGE
}

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
  usage
  exit 0
fi

MODE=$(normalize_mode "${requested_mode:-cgroup-hermit}")
[ "$MODE" = cgroup-hermit ] || rdma_die "this benchmark requires MODE=cgroup-hermit"

PAGE_SIZES_KB=${PAGE_SIZES_KB:-"4 16 32 64 128 256 512 1024 2048"}
LOCAL_RATIO_PCT=${LOCAL_RATIO_PCT:-70}
BENCH_REPEATS=${BENCH_REPEATS:-3}
GAPBS_DIR=${GAPBS_DIR:-}
GAPBS_KERNEL=${GAPBS_KERNEL:-bfs}
GAPBS_SCALE=${GAPBS_SCALE:-24}
GAPBS_DEGREE=${GAPBS_DEGREE:-16}
GAPBS_TRIALS=${GAPBS_TRIALS:-4}
GAPBS_PR_ITERS=${GAPBS_PR_ITERS:-20}
GAPBS_THREADS=${GAPBS_THREADS:-8}
BENCH_CPUS=${BENCH_CPUS:-0-7}
READY_TIMEOUT_SEC=${READY_TIMEOUT_SEC:-900}
RUN_TIMEOUT_SEC=${RUN_TIMEOUT_SEC:-3600}
RUN_ID=${requested_run_id:-"$(date +%Y%m%d-%H%M%S)-gapbs-${GAPBS_KERNEL}-swapio"}
RESULT_DIR=${requested_result_dir:-"$RESULT_ROOT/$RUN_ID"}
HOOK="$SCRIPT_DIR/gapbs_hermit_hook.h"
CGROUP=${PEBS_CGROUP:-/sys/fs/cgroup/hermit-gapbs}
CSV="$RESULT_DIR/gapbs-swapio-summary.csv"
current_pid=

case "$GAPBS_KERNEL" in bfs|pr) ;; *) rdma_die "GAPBS_KERNEL must be bfs or pr" ;; esac
for name in GAPBS_SCALE GAPBS_DEGREE GAPBS_TRIALS GAPBS_PR_ITERS GAPBS_THREADS BENCH_REPEATS; do
  [[ "${!name}" =~ ^[1-9][0-9]*$ ]] || rdma_die "$name must be a positive integer"
done
[ "$GAPBS_SCALE" -le 30 ] || rdma_die "GAPBS_SCALE above 30 is not supported"
[ -n "$GAPBS_DIR" ] && [ -r "$GAPBS_DIR/src/$GAPBS_KERNEL.cc" ] && [ -r "$GAPBS_DIR/src/benchmark.h" ] || \
  rdma_die "GAPBS_DIR must contain src/$GAPBS_KERNEL.cc and src/benchmark.h"
[ -r "$HOOK" ] || rdma_die "missing hook header: $HOOK"
command -v "${CXX:-g++}" >/dev/null 2>&1 || rdma_die "a C++ compiler is required (set CXX)"
command -v taskset >/dev/null 2>&1 || rdma_die "taskset is required"
taskset -c "$BENCH_CPUS" true >/dev/null 2>&1 || \
  rdma_die "invalid or unavailable BENCH_CPUS CPU list: $BENCH_CPUS"

# The binary is derived from the GAPBS source and the hook, never prebuilt.
BIN=${GAPBS_BIN:-"${TMPDIR:-/tmp}/hermit-gapbs-${GAPBS_KERNEL}-$(id -u)"}
if [ ! -x "$BIN" ] || [ "$HOOK" -nt "$BIN" ] || [ "$GAPBS_DIR/src/$GAPBS_KERNEL.cc" -nt "$BIN" ] || \
   [ -n "$(find "$GAPBS_DIR/src" -name '*.h' -newer "$BIN" -print -quit)" ]; then
  rdma_log "building $BIN"
  "${CXX:-g++}" -std=c++11 -O3 -Wall -fopenmp -I "$GAPBS_DIR/src" -include "$HOOK" \
    -o "$BIN" "$GAPBS_DIR/src/$GAPBS_KERNEL.cc"
fi

cmd=("$BIN" -g "$GAPBS_SCALE" -k "$GAPBS_DEGREE" -n "$GAPBS_TRIALS" -v)
[ "$GAPBS_KERNEL" != pr ] || cmd+=(-i "$GAPBS_PR_ITERS")

cleanup_process() {
  if [ -n "$current_pid" ] && kill -0 "$current_pid" 2>/dev/null; then
    kill -TERM "$current_pid" 2>/dev/null || true
    wait "$current_pid" 2>/dev/null || true
  fi
  current_pid=
  sudo_write max "$CGROUP/memory.max" 2>/dev/null || true
}

cleanup() {
  local ret=$?
  cleanup_process
  restore_thp_state
  sudo rmdir "$CGROUP" 2>/dev/null || true
  exit "$ret"
}

wait_for_log() {
  local pattern=$1 log=$2 timeout=$3 poll=${4:-1} start
  start=$(date +%s)
  until grep -q "$pattern" "$log" 2>/dev/null; do
    kill -0 "$current_pid" 2>/dev/null || rdma_die "gapbs process exited; see $log"
    [ "$(( $(date +%s) - start ))" -lt "$timeout" ] || \
      rdma_die "timeout waiting for $pattern; see $log"
    sleep "$poll"
  done
}

validate_sweep_host

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
save_thp_state
configure_hermit_mode

prepare_sweep_cgroup

mkdir -p "$RESULT_DIR"
{
  printf 'date=%s\n' "$(date --iso-8601=seconds)"
  printf 'kernel=%s\n' "$(uname -r)"
  printf 'mode=%s\nlocal_ratio_pct=%s\n' "$MODE" "$LOCAL_RATIO_PCT"
  printf 'page_sizes_kb=%s\nrepeats=%s\nbench_cpus=%s\n' "$PAGE_SIZES_KB" "$BENCH_REPEATS" "$BENCH_CPUS"
  printf 'gapbs_dir=%s\ngapbs_kernel=%s\ngapbs_scale=%s\ngapbs_degree=%s\n' \
    "$GAPBS_DIR" "$GAPBS_KERNEL" "$GAPBS_SCALE" "$GAPBS_DEGREE"
  printf 'gapbs_trials=%s\ngapbs_pr_iters=%s\ngapbs_threads=%s\n' \
    "$GAPBS_TRIALS" "$GAPBS_PR_ITERS" "$GAPBS_THREADS"
  printf 'gapbs_command=%s\n' "${cmd[*]}"
  printf 'gapbs_source_sha256=%s\n' "$(sha256sum "$GAPBS_DIR/src/$GAPBS_KERNEL.cc" | cut -d' ' -f1)"
  printf 'hook_sha256=%s\n' "$(sha256sum "$HOOK" | cut -d' ' -f1)"
  printf 'backend=%s\n' "$(detect_rswap_backend)"
  printf 'swap:\n'
  sed 's/^/  /' /proc/swaps
} > "$RESULT_DIR/environment.txt"
printf 'page_kb,order,repeat,mode,kernel,scale,degree,nodes,edges,resident_mb,limit_mb,trials,verified_trials,trial_sec_total,run_sec,trials_per_sec,pswpout_delta,target_stores_delta,total_store_bytes,large_store_pct,store_phase_sec,store_phase_gib_per_sec,pswpin_delta,run_pswpout_delta,target_loads_delta,target_fallback_delta,target_errors_delta,total_load_bytes,large_load_pct,load_phase_sec,load_phase_gib_per_sec,checksum_errors\n' > "$CSV"

export OMP_NUM_THREADS="$GAPBS_THREADS" OMP_PROC_BIND=true OMP_PLACES=cores

for kb in $PAGE_SIZES_KB; do
  order=$(page_order "$kb") || rdma_die "unsupported page size: ${kb} KiB"
  configure_page_size "$kb" || rdma_die "THP size ${kb} KiB is unavailable"

  for repeat in $(seq 1 "$BENCH_REPEATS"); do
    run_dir="$RESULT_DIR/${kb}k/r${repeat}"
    log="$run_dir/gapbs.log"
    before="$run_dir/order-before.txt"
    after="$run_dir/order-after.txt"
    mkdir -p "$run_dir"
    sudo_write max "$CGROUP/memory.max"

    # Join the cgroup before exec: graph generation is part of the residency.
    taskset -c "$BENCH_CPUS" bash -c 'echo $$ | sudo -n tee "$1/cgroup.procs" >/dev/null && shift && exec "$@"' \
      gapbs "$CGROUP" "${cmd[@]}" > "$log" 2>&1 &
    current_pid=$!
    wait_for_log '^READY ' "$log" "$READY_TIMEOUT_SEC"
    enable_benchmark_pebs_pid "$current_pid"
    require_process_thp_enabled "$current_pid" "$run_dir/status-before.txt"
    nodes=$(log_field READY nodes "$log")
    edges=$(log_field READY edges "$log")
    [ -n "$nodes" ] && [ -n "$edges" ] || rdma_die "missing READY metrics; see $log"

    resident_bytes=$(sudo_cat "$CGROUP/memory.current")
    resident_mb=$((resident_bytes / 1024 / 1024))
    limit_mb=$(benchmark_memory_limit_mb "$resident_mb" "$LOCAL_RATIO_PCT")
    [ "$limit_mb" -lt "$resident_mb" ] || \
      rdma_die "computed limit ${limit_mb} MiB is not below resident ${resident_mb} MiB; lower LOCAL_RATIO_PCT or raise GAPBS_SCALE"
    check_swap_capacity "$((resident_mb - limit_mb))"

    sudo_cat "/proc/$current_pid/smaps_rollup" > "$run_dir/smaps-rollup-before.txt"
    sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-before.txt"
    wait_for_stores_quiet
    # Store bytes are counted between these two snapshots, so the interval is
    # the time up to the last store change, not the script's total runtime.
    store_start_ns=$(date +%s%N)
    snapshot_sweep_stats "$before"
    pswpout_before=$(read_vmstat_key pswpout)
    oom_before=$(memory_event oom_kill)
    sudo_write "$((limit_mb * 1024 * 1024))" "$CGROUP/memory.max"
    wait_for_stores_quiet "$(date +%s%N)"
    snapshot_sweep_stats "$after"
    pswpout_after=$(read_vmstat_key pswpout)
    sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after.txt"
    [ "$(memory_event oom_kill)" -eq "$oom_before" ] || rdma_die "cgroup OOM during swap-out; see $run_dir"
    kill -0 "$current_pid" 2>/dev/null || rdma_die "gapbs process exited during reclaim; see $run_dir"

    completion_ms=$(( (last_change_ns - store_start_ns) / 1000000 ))
    [ "$completion_ms" -gt 0 ] || completion_ms=1
    store_phase_sec=$(elapsed_seconds "$store_start_ns" "$last_change_ns") || \
      rdma_die "invalid store interval; see $run_dir"
    target_stores=$(( $(order_field "$after" "$order" 3) - $(order_field "$before" "$order" 3) ))
    pswpout_delta=$((pswpout_after - pswpout_before))
    summarize_stores "$before" "$after" "$completion_ms"

    # Keep the same hard budget throughout the timed trials.
    snapshot_fixed_memory_budget "$CGROUP" "$run_dir" before-swapin "$((limit_mb * 1024 * 1024))"
    load_before="$run_dir/order-before-swapin.txt"
    load_after="$run_dir/order-after-swapin.txt"
    # The load counters below are sampled between these two snapshots, so the
    # interval that pairs with them is the whole application phase: timed
    # trials plus the verification GAPBS runs after each trial. Dividing the
    # same bytes by the trial timer alone (trial_sec_total) overstated the
    # remote-read bandwidth, so phase time is used and labelled as such.
    load_start_ns=$(date +%s%N)
    snapshot_sweep_stats "$load_before"
    pswpin_before=$(read_vmstat_key pswpin)
    run_pswpout_before=$(read_vmstat_key pswpout)
    kill -USR2 "$current_pid"
    wait_for_log '^DONE ' "$log" "$RUN_TIMEOUT_SEC" 1
    snapshot_sweep_stats "$load_after"
    load_end_ns=$(date +%s%N)
    pswpin_after=$(read_vmstat_key pswpin)
    run_pswpout_after=$(read_vmstat_key pswpout)
    sudo_cat "/proc/$current_pid/smaps_rollup" > "$run_dir/smaps-rollup-after-swapin.txt"
    sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-after-swapin.txt"
    sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after-swapin.txt"
    snapshot_fixed_memory_budget "$CGROUP" "$run_dir" after-swapin "$((limit_mb * 1024 * 1024))"
    [ "$(memory_event oom_kill)" -eq "$oom_before" ] || rdma_die "cgroup OOM during trials; see $run_dir"

    # The kernel's own trial times exclude graph build and verification.
    trials=$(awk '/^Trial Time:/ { n++ } END { print n + 0 }' "$log")
    trial_sec=$(awk '/^Trial Time:/ { s += $3 } END { printf "%.6f", s }' "$log")
    verified=$(awk '/^Verification:/ && $2 == "PASS" { n++ } END { print n + 0 }' "$log")
    run_sec=$(log_field DONE sec "$log")
    [ "$trials" -eq "$GAPBS_TRIALS" ] || rdma_die "expected $GAPBS_TRIALS trials, found $trials; see $log"
    [ "$verified" -eq "$GAPBS_TRIALS" ] || rdma_die "only $verified of $GAPBS_TRIALS trials passed verification; see $log"
    awk -v s="$trial_sec" 'BEGIN { exit !(s > 0) }' || rdma_die "invalid trial time; see $log"
    trials_per_sec=$(awk -v n="$trials" -v s="$trial_sec" 'BEGIN { printf "%.6f", n / s }')

    pswpin_delta=$((pswpin_after - pswpin_before))
    run_pswpout_delta=$((run_pswpout_after - run_pswpout_before))
    target_loads=$(( $(order_field "$load_after" "$order" 4) - $(order_field "$load_before" "$order" 4) ))
    target_fallback=$(( $(order_field "$load_after" "$order" 5) - $(order_field "$load_before" "$order" 5) ))
    target_errors=$(( $(order_field "$load_after" "$order" 6) - $(order_field "$load_before" "$order" 6) ))
    load_phase_sec=$(elapsed_seconds "$load_start_ns" "$load_end_ns") || \
      rdma_die "invalid swap-in interval; see $run_dir"
    awk -v s="$load_phase_sec" 'BEGIN { exit !(s > 0) }' || \
      rdma_die "zero-length swap-in interval; see $run_dir"
    summarize_loads "$load_before" "$load_after" "$load_phase_sec"
    [ "$pswpin_delta" -gt 0 ] || rdma_die "trials caused no swap-ins; lower LOCAL_RATIO_PCT; see $run_dir"
    [[ "$total_load_bytes" =~ ^[0-9]+$ ]] && [ "$total_load_bytes" -gt 0 ] || \
      rdma_die "Hermit recorded no backend loads; see $run_dir"

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$kb" "$order" "$repeat" "$MODE" "$GAPBS_KERNEL" "$GAPBS_SCALE" "$GAPBS_DEGREE" \
      "$nodes" "$edges" "$resident_mb" "$limit_mb" "$trials" "$verified" "$trial_sec" \
      "$run_sec" "$trials_per_sec" "$pswpout_delta" "$target_stores" "$total_store_bytes" \
      "$large_store_pct" "$store_phase_sec" "$protocol_gib_per_sec" "$pswpin_delta" \
      "$run_pswpout_delta" "$target_loads" "$target_fallback" "$target_errors" \
      "$total_load_bytes" "$large_load_pct" "$load_phase_sec" "$load_protocol_gib_per_sec" "0" >> "$CSV"
    rdma_log "page=${kb}k repeat=$repeat kernel=$GAPBS_KERNEL trials/s=$trials_per_sec resident=${resident_mb}MiB limit=${limit_mb}MiB swapin=${load_protocol_gib_per_sec}GiB/s over ${load_phase_sec}s phase load_large=${large_load_pct}% fallback=$target_fallback errors=$target_errors"
    kill -USR2 "$current_pid"
    wait "$current_pid" || rdma_die "gapbs exited with an error; see $log"
    current_pid=
    sudo_write max "$CGROUP/memory.max"
  done
done

trap - EXIT INT TERM
cleanup
