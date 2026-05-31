#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'USAGE'
usage:
  memcached.sh start
  memcached.sh stop
  memcached.sh status
  memcached.sh stats

Common environment:
  MODE=local|cgroup-linux|cgroup-hermit
  PORT=11211
  MEMCACHED_MEM_MB=16384
  MEMCACHED_BIN=/path/to/memcached
  RESULT_DIR=...
USAGE
}

wait_for_memcached() {
  local i
  for i in $(seq 1 50); do
    if memcached_stats | grep -q '^STAT'; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

start_memcached() {
  local bin pid log_file
  mkdir -p "$RESULT_DIR"
  detect_core_layout
  save_config

  if [[ "$MODE" == cgroup-* ]]; then
    setup_cgroup
    set_cgroup_limit_mb "${LOAD_CGROUP_LIMIT_MB:-max}"
  fi

  bin="../../../memcached/memcached"
  log_file="$RESULT_DIR/memcached.log"

  if [ -s "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    rdma_log "memcached already running: pid $(cat "$PID_FILE")"
    return 0
  fi

  rdma_log "starting memcached on cores $MEMCACHED_CORES, threads $MEMCACHED_THREADS"
  if [ "$(id -u)" -eq 0 ]; then
    taskset -c "$MEMCACHED_CORES" "$bin" -u "${MEMCACHED_USER:-root}" \
      -p "$PORT" -t "$MEMCACHED_THREADS" -m "$MEMCACHED_MEM_MB" \
      -c "$MEMCACHED_MAX_CONN" ${MEMCACHED_EXTRA_ARGS:-} \
      > "$log_file" 2>&1 &
  else
    taskset -c "$MEMCACHED_CORES" "$bin" \
      -p "$PORT" -t "$MEMCACHED_THREADS" -m "$MEMCACHED_MEM_MB" \
      -c "$MEMCACHED_MAX_CONN" ${MEMCACHED_EXTRA_ARGS:-} \
      > "$log_file" 2>&1 &
  fi

  pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"

  if [[ "$MODE" == cgroup-* ]]; then
    move_pid_to_cgroup "$pid"
  fi

  if ! wait_for_memcached; then
    rdma_log "memcached did not answer; see $log_file"
    return 1
  fi

  rdma_log "memcached pid=$pid port=$PORT"
}

stop_memcached() {
  if [ -s "$PID_FILE" ]; then
    local pid
    pid=$(cat "$PID_FILE")
    if kill -0 "$pid" 2>/dev/null; then
      rdma_log "stopping memcached pid=$pid"
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  else
    rdma_log "no pid file at $PID_FILE"
  fi
}

action=${1:-status}

case "$action" in
  start)
    start_memcached
    ;;
  stop)
    stop_memcached
    ;;
  status)
    if [ -s "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      rdma_log "running pid $(cat "$PID_FILE")"
    else
      rdma_log "not running"
      exit 1
    fi
    ;;
  stats)
    memcached_stats
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage
    exit 2
    ;;
esac
