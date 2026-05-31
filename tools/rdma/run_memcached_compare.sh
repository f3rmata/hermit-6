#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'USAGE'
usage:
  run_memcached_compare.sh

This runs the selected modes on the currently booted kernel. To compare
Hermit 5.14 and Hermit 6.6, run it once after booting each kernel and set
KERNEL_TAG explicitly if auto-detection is not enough:

  KERNEL_TAG=hermit-5 MODES="local cgroup-linux cgroup-hermit" ./run_memcached_compare.sh
  KERNEL_TAG=hermit-6 MODES="local cgroup-linux cgroup-hermit" ./run_memcached_compare.sh

Important environment:
  MODES="local cgroup-linux cgroup-hermit"
  LOCAL_RATIO_PCT=70
  LOADS="500000 750000 1000000 1250000 1500000 2000000"
  RECORDS=32000000
  MEMCACHED_MEM_MB=16384
  PORT=11211
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

MODES=${MODES:-"local cgroup-linux cgroup-hermit"}
BASE_RUN_ID=${BASE_RUN_ID:-"$(date +%Y%m%d-%H%M%S)-${KERNEL_TAG}"}

for mode in $MODES; do
  mode=$(normalize_mode "$mode")
  run_id="${BASE_RUN_ID}-${mode}"
  result_dir="$RESULT_ROOT/$run_id"
  rdma_log "=== kernel=$KERNEL_TAG mode=$mode result=$result_dir ==="

  MODE=$mode KERNEL_TAG=$KERNEL_TAG RESULT_DIR=$result_dir PID_FILE="$result_dir/memcached.pid" \
    "$SCRIPT_DIR/memcached_load.sh" --mode "$mode" --kernel "$KERNEL_TAG" --result-dir "$result_dir" --port "$PORT"

  MODE=$mode KERNEL_TAG=$KERNEL_TAG RESULT_DIR=$result_dir PID_FILE="$result_dir/memcached.pid" \
    "$SCRIPT_DIR/memcached_bench.sh" --mode "$mode" --kernel "$KERNEL_TAG" --result-dir "$result_dir" --port "$PORT"

  MODE=$mode KERNEL_TAG=$KERNEL_TAG RESULT_DIR=$result_dir PID_FILE="$result_dir/memcached.pid" \
    "$SCRIPT_DIR/memcached.sh" stop || true

  sleep "${BETWEEN_MODE_SLEEP_SEC:-10}"
done

rdma_log "aggregate CSV: $AGGREGATE_CSV"
