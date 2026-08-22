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
  MODE=local ./run_xgboost_native_sweep.sh
  MODE=cgroup-linux LOCAL_RATIO_PCT=70 ./run_xgboost_native_sweep.sh

Native local / local-swap XGBoost page-size baseline. The script uses the
same signal-driven XGBoost worker and THP page-size sweep as the Hermit
script, but never touches Hermit debugfs. For cgroup-linux it lowers
memory.max to force native swap-out and then runs training to measure
native swap-in; for local it simply runs training with unlimited memory.

Important environment:
  PAGE_SIZES_KB="4 16 32 64 128 256 512 1024 2048"
  WORKSET_MB=16384                  (synthetic matrix size)
  XGB_DATA_FILE=/path/to/train.libsvm (optional real dataset)
  XGB_DATA_FORMAT=csv|libsvm        (inferred from extension when unset)
  LOCAL_RATIO_PCT=70                (limit = resident MiB * ratio / 100)
  BENCH_REPEATS=3
  BENCH_CPU=0
  XGB_ROUNDS=30
  XGB_FEATURES=28
  XGB_MAX_DEPTH=8
  XGB_NTHREAD=4
  XGB_TREE_METHOD=hist
  XGB_EVAL_METRIC=auc
  XGB_EXPECTED_METRIC_MIN=          (optional sanity window)
  XGB_EXPECTED_METRIC_MAX=          (optional sanity window)
  QUIET_INTERVAL_SEC=1
  QUIET_SAMPLES=3
  QUIET_TIMEOUT_SEC=120
  READY_TIMEOUT_SEC=300
  TRAIN_TIMEOUT_SEC=3600
  RESTORE_THP=1

Requirements: for MODE=cgroup-linux, cgroup v2 and exactly one active
block-device swap area; the script owns /sys/fs/cgroup/hermit-xgboost-native.
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
WORKSET_MB=${WORKSET_MB:-16384}
LOCAL_RATIO_PCT=${LOCAL_RATIO_PCT:-70}
XGB_DATA_FILE=${XGB_DATA_FILE:-}
BENCH_REPEATS=${BENCH_REPEATS:-3}
BENCH_CPU=${BENCH_CPU:-0}
XGB_ROUNDS=${XGB_ROUNDS:-30}
XGB_FEATURES=${XGB_FEATURES:-28}
XGB_MAX_DEPTH=${XGB_MAX_DEPTH:-8}
XGB_NTHREAD=${XGB_NTHREAD:-4}
XGB_TREE_METHOD=${XGB_TREE_METHOD:-hist}
XGB_EVAL_METRIC=${XGB_EVAL_METRIC:-auc}
XGB_EXPECTED_METRIC_MIN=${XGB_EXPECTED_METRIC_MIN:-}
XGB_EXPECTED_METRIC_MAX=${XGB_EXPECTED_METRIC_MAX:-}
QUIET_INTERVAL_SEC=${QUIET_INTERVAL_SEC:-1}
QUIET_SAMPLES=${QUIET_SAMPLES:-3}
QUIET_TIMEOUT_SEC=${QUIET_TIMEOUT_SEC:-120}
READY_TIMEOUT_SEC=${READY_TIMEOUT_SEC:-300}
TRAIN_TIMEOUT_SEC=${TRAIN_TIMEOUT_SEC:-3600}
RESTORE_THP=${RESTORE_THP:-1}
RUN_ID=${requested_run_id:-"$(date +%Y%m%d-%H%M%S)-xgboost-native"}
RESULT_DIR=${requested_result_dir:-"$RESULT_ROOT/$RUN_ID"}
WORKER="$SCRIPT_DIR/xgboost_train.py"
CGROUP_NAME=hermit-xgboost-native
CGROUP=/sys/fs/cgroup/$CGROUP_NAME
THP_ROOT=/sys/kernel/mm/transparent_hugepage
CSV="$RESULT_DIR/xgboost-native-summary.csv"
current_pid=
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

cleanup_process() {
  if [ -n "$current_pid" ] && kill -0 "$current_pid" 2>/dev/null; then
    kill -TERM "$current_pid" 2>/dev/null || true
    wait "$current_pid" 2>/dev/null || true
  fi
  current_pid=
  if [ "$MODE" = cgroup-linux ]; then
    sudo_write max "$CGROUP/memory.max" 2>/dev/null || true
  fi
}

cleanup() {
  local ret=$? file
  cleanup_process
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
    kill -0 "$current_pid" 2>/dev/null || rdma_die "xgboost process exited; see $log"
    [ "$(( $(date +%s) - start ))" -lt "$timeout" ] || \
      rdma_die "timeout waiting for $pattern; see $log"
    sleep "$poll"
  done
}

[ -x "$WORKER" ] || rdma_die "missing executable worker: $WORKER"
command -v python3 >/dev/null 2>&1 || rdma_die "python3 is required"
python3 -c 'import xgboost' 2>/dev/null || \
  rdma_die "python3 xgboost module is required (pip install xgboost)"
if [ "$MODE" = cgroup-linux ]; then
  [ -r /sys/fs/cgroup/cgroup.controllers ] || rdma_die "cgroup v2 is required"
  swap_count=$(awk 'NR > 1 { count++ } END { print count + 0 }' /proc/swaps)
  [ "$swap_count" -eq 1 ] || \
    rdma_die "exactly one active swap device is required; found $swap_count"
  awk 'NR == 2 && $2 == "partition" { ok=1 } END { exit !ok }' /proc/swaps || \
    rdma_die "the active swap area must be a block device, not a swapfile"
fi
if [ -n "$XGB_DATA_FILE" ] && [ ! -r "$XGB_DATA_FILE" ]; then
  rdma_die "XGB_DATA_FILE is not readable: $XGB_DATA_FILE"
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
  printf 'page_sizes_kb=%s\nrepeats=%s\nbench_cpu=%s\n' \
    "$PAGE_SIZES_KB" "$BENCH_REPEATS" "$BENCH_CPU"
  printf 'xgboost_data_file=%s\nxgboost_rounds=%s\nxgboost_features=%s\n' \
    "${XGB_DATA_FILE:-synthetic}" "$XGB_ROUNDS" "$XGB_FEATURES"
  printf 'xgboost_max_depth=%s\nxgboost_nthread=%s\nxgboost_tree_method=%s\n' \
    "$XGB_MAX_DEPTH" "$XGB_NTHREAD" "$XGB_TREE_METHOD"
  printf 'xgboost_eval_metric=%s\n' "$XGB_EVAL_METRIC"
  printf 'swap:\n'
  sed 's/^/  /' /proc/swaps
} > "$RESULT_DIR/environment.txt"
printf 'page_kb,order,repeat,mode,resident_mb,limit_mb,populate_sec,rows,features,rounds,train_metric,train_metric_value,train_sec,pswpin_delta,pswpout_delta,train_pswpout_delta\n' > "$CSV"

export XGB_WORKSET_MB=${XGB_WORKSET_MB:-$WORKSET_MB}
export XGB_ROUNDS XGB_FEATURES XGB_MAX_DEPTH XGB_NTHREAD XGB_TREE_METHOD
export XGB_EVAL_METRIC
[ -z "$XGB_DATA_FILE" ] || export XGB_DATA_FILE

for kb in $PAGE_SIZES_KB; do
  order=$(page_order "$kb") || rdma_die "unsupported page size: ${kb} KiB"
  configure_page_size "$kb" || rdma_die "THP size ${kb} KiB is unavailable"

  for repeat in $(seq 1 "$BENCH_REPEATS"); do
    run_dir="$RESULT_DIR/${kb}k/r${repeat}"
    log="$run_dir/xgboost.log"
    mkdir -p "$run_dir"
    if [ "$MODE" = cgroup-linux ]; then
      sudo_write max "$CGROUP/memory.max"
    fi

    taskset -c "$BENCH_CPU" python3 "$WORKER" > "$log" 2>&1 &
    current_pid=$!
    if [ "$MODE" = cgroup-linux ]; then
      move_pid_to_cgroup "$current_pid"
    fi
    wait_for_log '^WAITING ' "$log" "$READY_TIMEOUT_SEC"
    kill -USR1 "$current_pid"
    wait_for_log '^READY ' "$log" "$READY_TIMEOUT_SEC"

    populate_sec=$(awk -F'populate_sec=' '/^READY / { print $2; exit }' "$log")
    rows=$(awk -F'rows=' '/^READY / { print $2; exit }' "$log" | awk '{ print $1 }')
    features=$(awk -F'features=' '/^READY / { print $2; exit }' "$log" | awk '{ print $1 }')
    [ -n "$populate_sec" ] || rdma_die "missing READY metrics; see $log"

    resident_mb=0
    limit_mb=0
    pswpout_delta=0
    pswpin_delta=0
    train_pswpout_delta=0

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

      sudo_cat "/proc/$current_pid/smaps_rollup" > "$run_dir/smaps-rollup-before.txt"
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
      [ "$oom_after" -eq "$oom_before" ] || rdma_die "cgroup OOM killed the xgboost process; see $run_dir"
      kill -0 "$current_pid" 2>/dev/null || rdma_die "xgboost process exited during reclaim; see $run_dir"
      pswpout_delta=$((pswpout_after - pswpout_before))
      sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-after.txt"
      sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after.txt"
      sudo_write max "$CGROUP/memory.max"
    fi

    pswpin_before=$(read_vmstat_key pswpin)
    train_pswpout_before=$(read_vmstat_key pswpout)
    train_start_ns=$(date +%s%N)
    kill -USR2 "$current_pid"
    wait_for_log '^TRAIN ' "$log" "$TRAIN_TIMEOUT_SEC" 1
    train_end_ns=$(date +%s%N)
    pswpin_after=$(read_vmstat_key pswpin)
    train_pswpout_after=$(read_vmstat_key pswpout)
    if [ "$MODE" = cgroup-linux ]; then
      sudo_cat "/proc/$current_pid/smaps_rollup" > "$run_dir/smaps-rollup-after-swapin.txt"
      sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-after-swapin.txt"
      sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after-swapin.txt"
    fi

    train_sec=$(awk -F'sec=' '/^TRAIN / { print $2; exit }' "$log" | awk '{ print $1 }')
    train_metric=$(awk -F'metric=' '/^TRAIN / { print $2; exit }' "$log" | awk '{ print $1 }')
    train_metric_value=$(awk -F'value=' '/^TRAIN / { print $2; exit }' "$log" | awk '{ print $1 }')
    [ -n "$train_sec" ] || rdma_die "missing TRAIN metrics; see $log"
    [ -n "$train_metric_value" ] || rdma_die "missing TRAIN metric value; see $log"
    if [ -n "$XGB_EXPECTED_METRIC_MIN" ] || [ -n "$XGB_EXPECTED_METRIC_MAX" ]; then
      metric_ok=1
      if [ -n "$XGB_EXPECTED_METRIC_MIN" ]; then
        awk -v v="$train_metric_value" -v lo="$XGB_EXPECTED_METRIC_MIN" \
          'BEGIN { exit !(v >= lo) }' || metric_ok=0
      fi
      if [ -n "$XGB_EXPECTED_METRIC_MAX" ]; then
        awk -v v="$train_metric_value" -v hi="$XGB_EXPECTED_METRIC_MAX" \
          'BEGIN { exit !(v <= hi) }' || metric_ok=0
      fi
      [ "$metric_ok" = 1 ] || \
        rdma_die "training metric $train_metric=$train_metric_value is outside the expected window; see $run_dir"
    fi

    pswpin_delta=$((pswpin_after - pswpin_before))
    train_pswpout_delta=$((train_pswpout_after - train_pswpout_before))

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$kb" "$order" "$repeat" "$MODE" "$resident_mb" "$limit_mb" \
      "$populate_sec" "$rows" "$features" "$XGB_ROUNDS" "$train_metric" \
      "$train_metric_value" "$train_sec" "$pswpin_delta" "$pswpout_delta" \
      "$train_pswpout_delta" >> "$CSV"
    rdma_log "page=${kb}k repeat=$repeat mode=$MODE train_sec=$train_sec $train_metric=$train_metric_value pswpin_delta=$pswpin_delta pswpout_delta=$pswpout_delta train_pswpout_delta=$train_pswpout_delta"
    cleanup_process
  done
done

trap - EXIT INT TERM
cleanup
