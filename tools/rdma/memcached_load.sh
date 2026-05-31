#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE=$2; shift 2 ;;
    --kernel) KERNEL_TAG=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --result-dir) RESULT_DIR=$2; PID_FILE="$RESULT_DIR/memcached.pid"; shift 2 ;;
    -h|--help)
      echo "usage: MODE=local|cgroup-linux|cgroup-hermit $0 [--port 11211]"
      exit 0
      ;;
    *)
      PORT=$1
      shift
      ;;
  esac
done

mkdir -p "$RESULT_DIR"
detect_core_layout
configure_hermit_mode
save_config

if [ "${START_MEMCACHED:-1}" = "1" ]; then
  MODE=$MODE KERNEL_TAG=$KERNEL_TAG PORT=$PORT RESULT_DIR=$RESULT_DIR \
    PID_FILE=$PID_FILE "$SCRIPT_DIR/memcached.sh" start
fi

MUTILATE_BIN="../../../mutilate/mutilate"
LOAD_LOG="$RESULT_DIR/load.log"

rdma_log "loading $RECORDS records into memcached at $SERVER_ADDR:$PORT"
taskset -c "$MUTILATE_CORES" "$MUTILATE_BIN" \
  -s "$SERVER_ADDR:$PORT" --loadonly -r "$RECORDS" \
  --keysize="$KEYSIZE" --valuesize="$VALUESIZE" \
  > "$LOAD_LOG" 2>&1

memcached_stats > "$RESULT_DIR/memcached-after-load.stats"

rdma_log "load phase finished; stats saved under $RESULT_DIR"
sleep "${LOAD_STABILIZE_SEC:-10}"
