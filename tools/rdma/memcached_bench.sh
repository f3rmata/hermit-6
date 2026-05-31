#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

ulimit -n 65535

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE=$(normalize_mode "$2"); shift 2 ;;
    --kernel) KERNEL_TAG=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --loads) LOADS=$2; shift 2 ;;
    --duration) DURATION=$2; shift 2 ;;
    --ratio) LOCAL_RATIO_PCT=$2; shift 2 ;;
    --limit-mb) CGROUP_LIMIT_MB=$2; shift 2 ;;
    --result-dir) RESULT_DIR=$2; PID_FILE="$RESULT_DIR/memcached.pid"; shift 2 ;;
    -h|--help)
      echo "usage: MODE=local|cgroup-linux|cgroup-hermit $0 [--loads \"...\"] [--ratio 70]"
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

if [[ "$MODE" == cgroup-* ]]; then
  applied_limit=$(apply_requested_cgroup_limit)
  rdma_log "using cgroup $(cgroup_path), memory limit ${applied_limit} MB"
  save_config
fi

MUTILATE_BIN=$(find_mutilate_bin)
CSV_FILE="$RESULT_DIR/mutilate_load_vs_latency.csv"
SUMMARY_FILE="$RESULT_DIR/summary.csv"

CSV_HEADER="kernel_tag,uname,mode,local_ratio_pct,cgroup_limit_mb,offered_qps,achieved_qps,read_avg_us,read_p99_us,update_avg_us,update_p99_us,miss_rate_pct,skipped_txs_pct,pswpin_delta,pswpout_delta,backend_loads_delta,backend_stores_delta,backend_load_misses_delta,backend_errors_delta,memcached_curr_items,memcached_evictions,memcached_bytes,log_file"

printf '%s\n' "$CSV_HEADER" > "$CSV_FILE"
printf '%s\n' "$CSV_HEADER" > "$SUMMARY_FILE"
if [ ! -s "$AGGREGATE_CSV" ]; then
  mkdir -p "$(dirname "$AGGREGATE_CSV")"
  printf '%s\n' "$CSV_HEADER" > "$AGGREGATE_CSV"
fi

for load in $LOADS; do
  rdma_log "running offered load: $load QPS"

  reset_hermit_stats "before-${load}" > "$RESULT_DIR/hermit-reset-${load}.log" 2>&1 || true

  before_kv="$RESULT_DIR/counters-before-${load}.kv"
  after_kv="$RESULT_DIR/counters-after-${load}.kv"
  before_stats="$RESULT_DIR/memcached-before-${load}.stats"
  after_stats="$RESULT_DIR/memcached-after-${load}.stats"
  temp_log="$RESULT_DIR/mutilate-${load}.log"

  capture_counters "$before_kv" "$before_stats"

  taskset -c "$MUTILATE_CORES" "$MUTILATE_BIN" \
    -s "$SERVER_ADDR:$PORT" --noload -r "$RECORDS" \
    -T "$MUTILATE_THREADS" -c "${MUTILATE_CONNECTIONS:-64}" \
    --keysize="$KEYSIZE" --valuesize="$VALUESIZE" --iadist="$IADIST" \
    --update="$UPDATE_RATIO" -q "$load" -t "$DURATION" \
    > "$temp_log" 2>&1

  capture_counters "$after_kv" "$after_stats"
  dump_hermit_stats "after-${load}" > "$RESULT_DIR/hermit-stats-${load}.log" 2>&1 || true

  achieved_qps=$(awk '/Total QPS/ { print $4 }' "$temp_log")
  read_avg=$(awk '/^read[[:space:]]/ { print $2 }' "$temp_log")
  read_p99=$(awk '/^#type/ { for (i=1; i<=NF; i++) if ($i=="p99" || $i=="99th") p99=i } /^read[[:space:]]/ { if (p99) print $p99; else print $9 }' "$temp_log")
  update_avg=$(awk '/^update[[:space:]]/ { print $2 }' "$temp_log")
  update_p99=$(awk '/^#type/ { for (i=1; i<=NF; i++) if ($i=="p99" || $i=="99th") p99=i } /^update[[:space:]]/ { if (p99) print $p99; else print $9 }' "$temp_log")
  miss_rate=$(awk '/^Misses/ { gsub(/[()%]/, "", $4); print $4 }' "$temp_log")
  skipped_rate=$(awk '/^Skipped TXs/ { gsub(/[()%]/, "", $4); print $4 }' "$temp_log")

  achieved_qps=${achieved_qps:-0}
  read_avg=${read_avg:-0}
  read_p99=${read_p99:-0}
  update_avg=${update_avg:-0}
  update_p99=${update_p99:-0}
  miss_rate=${miss_rate:-0}
  skipped_rate=${skipped_rate:-0}

  pswpin_delta=$(delta_from_files "$before_kv" "$after_kv" pswpin)
  pswpout_delta=$(delta_from_files "$before_kv" "$after_kv" pswpout)
  backend_loads_delta=$(delta_from_files "$before_kv" "$after_kv" backend_loads)
  backend_stores_delta=$(delta_from_files "$before_kv" "$after_kv" backend_stores)
  backend_load_misses_delta=$(delta_from_files "$before_kv" "$after_kv" backend_load_misses)
  backend_errors_delta=$(delta_from_files "$before_kv" "$after_kv" backend_errors)

  curr_items=$(kv_from_file "$after_kv" memcached_curr_items)
  evictions=$(kv_from_file "$after_kv" memcached_evictions)
  bytes=$(kv_from_file "$after_kv" memcached_bytes)

  row="$KERNEL_TAG,$(uname -r),$MODE,${LOCAL_RATIO_PCT:-},${CGROUP_LIMIT_MB:-},$load,$achieved_qps,$read_avg,$read_p99,$update_avg,$update_p99,$miss_rate,$skipped_rate,$pswpin_delta,$pswpout_delta,$backend_loads_delta,$backend_stores_delta,$backend_load_misses_delta,$backend_errors_delta,$curr_items,$evictions,$bytes,$temp_log"
  printf '%s\n' "$row" >> "$CSV_FILE"
  printf '%s\n' "$row" >> "$SUMMARY_FILE"
  printf '%s\n' "$row" >> "$AGGREGATE_CSV"

  rdma_log "achieved=$achieved_qps qps read_p99=${read_p99}us misses=${miss_rate}% pswpin_delta=$pswpin_delta pswpout_delta=$pswpout_delta"
  sleep "${BETWEEN_RUN_SLEEP_SEC:-5}"
done

rdma_log "test finished: $CSV_FILE"
