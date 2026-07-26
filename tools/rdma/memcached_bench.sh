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
      echo "usage: MODE=local|cgroup-linux|cgroup-hermit BENCH_REPEATS=3 CORE_LAYOUT=socket BENCH_SOCKET=1 $0 [--loads \"...\"] [--ratio 70]"
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
build_bench_prefix
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
WAIT_LOG="$RESULT_DIR/swap-wait.csv"

CSV_HEADER="kernel_tag,uname,mode,hermit_swapout_policy,rswap_backend,local_ratio_pct,cgroup_limit_mb,offered_qps,repeat,wait_before_sec,wait_status,wait_samples,wait_last_delta,achieved_qps,read_avg_us,read_p99_us,update_avg_us,update_p99_us,miss_rate_pct,skipped_txs_pct,pswpin_delta,pswpout_delta,backend_loads_delta,backend_stores_delta,backend_load_misses_delta,backend_errors_delta,hermit_swapout_backend_stores_delta,hermit_swapout_backend_store_errors_delta,hermit_swapout_backend_poll_errors_delta,hermit_swapout_native_fallbacks_delta,hermit_swapout_exclusive_completions_delta,hermit_swapout_writethrough_completions_delta,hermit_swapout_large_folio_fallbacks_delta,memcached_curr_items,memcached_evictions,memcached_bytes,log_file"
SUMMARY_HEADER="$CSV_HEADER,samples,achieved_qps_min,achieved_qps_max,read_p99_us_min,read_p99_us_max"

printf '%s\n' "$CSV_HEADER" > "$CSV_FILE"
printf '%s\n' "$SUMMARY_HEADER" > "$SUMMARY_FILE"
if [ -s "$AGGREGATE_CSV" ] && [ "$(head -n 1 "$AGGREGATE_CSV" 2>/dev/null)" != "$SUMMARY_HEADER" ]; then
  AGGREGATE_CSV="$RESULT_ROOT/all_runs_stable.csv"
fi
if [ ! -s "$AGGREGATE_CSV" ]; then
  mkdir -p "$(dirname "$AGGREGATE_CSV")"
  printf '%s\n' "$SUMMARY_HEADER" > "$AGGREGATE_CSV"
fi

for load in $LOADS; do
  for repeat in $(seq 1 "$BENCH_REPEATS"); do
    rdma_log "running offered load: $load QPS repeat $repeat/$BENCH_REPEATS"

    wait_for_swap_stable "before-${load}-r${repeat}" "$WAIT_LOG"
    wait_before_sec=$WAIT_LAST_SECONDS
    wait_status=$WAIT_LAST_STATUS
    wait_samples=$WAIT_LAST_SAMPLES
    wait_last_delta=$WAIT_LAST_DELTA

    require_memcached_healthy
    reset_hermit_stats "before-${load}-r${repeat}" > "$RESULT_DIR/hermit-reset-${load}-r${repeat}.log" 2>&1 || true

    before_kv="$RESULT_DIR/counters-before-${load}-r${repeat}.kv"
    after_kv="$RESULT_DIR/counters-after-${load}-r${repeat}.kv"
    before_stats="$RESULT_DIR/memcached-before-${load}-r${repeat}.stats"
    after_stats="$RESULT_DIR/memcached-after-${load}-r${repeat}.stats"
    temp_log="$RESULT_DIR/mutilate-${load}-r${repeat}.log"

    capture_counters "$before_kv" "$before_stats"

    "${BENCH_CMD_PREFIX[@]}" taskset -c "$MUTILATE_CORES" "$MUTILATE_BIN" \
      -s "$SERVER_ADDR:$PORT" --noload -r "$RECORDS" \
      -T "$MUTILATE_THREADS" -c "${MUTILATE_CONNECTIONS:-64}" \
      --keysize="$KEYSIZE" --valuesize="$VALUESIZE" --iadist="$IADIST" \
      --update="$UPDATE_RATIO" -q "$load" -t "$DURATION" \
      > "$temp_log" 2>&1

    capture_counters "$after_kv" "$after_stats"
    dump_hermit_stats "after-${load}-r${repeat}" > "$RESULT_DIR/hermit-stats-${load}-r${repeat}.log" 2>&1 || true

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
    hermit_swapout_backend_stores_delta=$(delta_from_files "$before_kv" "$after_kv" hermit_swapout_backend_stores)
    hermit_swapout_backend_store_errors_delta=$(delta_from_files "$before_kv" "$after_kv" hermit_swapout_backend_store_errors)
    hermit_swapout_backend_poll_errors_delta=$(delta_from_files "$before_kv" "$after_kv" hermit_swapout_backend_poll_errors)
    hermit_swapout_native_fallbacks_delta=$(delta_from_files "$before_kv" "$after_kv" hermit_swapout_native_fallbacks)
    hermit_swapout_exclusive_completions_delta=$(delta_from_files "$before_kv" "$after_kv" hermit_swapout_exclusive_completions)
    hermit_swapout_writethrough_completions_delta=$(delta_from_files "$before_kv" "$after_kv" hermit_swapout_writethrough_completions)
    hermit_swapout_large_folio_fallbacks_delta=$(delta_from_files "$before_kv" "$after_kv" hermit_swapout_large_folio_fallbacks)

    curr_items=$(kv_from_file "$after_kv" memcached_curr_items)
    evictions=$(kv_from_file "$after_kv" memcached_evictions)
    bytes=$(kv_from_file "$after_kv" memcached_bytes)

    row="$KERNEL_TAG,$(uname -r),$MODE,$HERMIT_SWAPOUT_POLICY,$(detect_rswap_backend),${LOCAL_RATIO_PCT:-},${CGROUP_LIMIT_MB:-},$load,$repeat,$wait_before_sec,$wait_status,$wait_samples,$wait_last_delta,$achieved_qps,$read_avg,$read_p99,$update_avg,$update_p99,$miss_rate,$skipped_rate,$pswpin_delta,$pswpout_delta,$backend_loads_delta,$backend_stores_delta,$backend_load_misses_delta,$backend_errors_delta,$hermit_swapout_backend_stores_delta,$hermit_swapout_backend_store_errors_delta,$hermit_swapout_backend_poll_errors_delta,$hermit_swapout_native_fallbacks_delta,$hermit_swapout_exclusive_completions_delta,$hermit_swapout_writethrough_completions_delta,$hermit_swapout_large_folio_fallbacks_delta,$curr_items,$evictions,$bytes,$temp_log"
    printf '%s\n' "$row" >> "$CSV_FILE"

    rdma_log "repeat=$repeat achieved=$achieved_qps qps read_p99=${read_p99}us wait=${wait_before_sec}s status=$wait_status pswpin_delta=$pswpin_delta pswpout_delta=$pswpout_delta"
    if [ "${BETWEEN_RUN_SLEEP_SEC:-0}" -gt 0 ]; then
      sleep "$BETWEEN_RUN_SLEEP_SEC"
    fi
  done
done

if command -v python3 >/dev/null 2>&1; then
  python3 - "$CSV_FILE" "$SUMMARY_FILE" "$AGGREGATE_CSV" "$SUMMARY_HEADER" <<'PY'
import csv
import statistics
import sys
from collections import defaultdict

raw_path, summary_path, aggregate_path, summary_header = sys.argv[1:]
summary_fields = summary_header.split(",")
numeric_fields = {
    "local_ratio_pct", "cgroup_limit_mb", "offered_qps", "wait_before_sec",
    "wait_samples", "wait_last_delta", "achieved_qps", "read_avg_us",
    "read_p99_us", "update_avg_us", "update_p99_us", "miss_rate_pct",
    "skipped_txs_pct", "pswpin_delta", "pswpout_delta",
    "backend_loads_delta", "backend_stores_delta",
    "backend_load_misses_delta", "backend_errors_delta",
    "hermit_swapout_backend_stores_delta",
    "hermit_swapout_backend_store_errors_delta",
    "hermit_swapout_backend_poll_errors_delta",
    "hermit_swapout_native_fallbacks_delta",
    "hermit_swapout_exclusive_completions_delta",
    "hermit_swapout_writethrough_completions_delta",
    "hermit_swapout_large_folio_fallbacks_delta",
    "memcached_curr_items", "memcached_evictions", "memcached_bytes",
}

def as_float(row, key):
    val = str(row.get(key, "")).strip()
    if not val:
        return 0.0
    try:
        return float(val)
    except ValueError:
        return 0.0

def numeric_values(group, key):
    vals = []
    for row in group:
        val = str(row.get(key, "")).strip()
        if not val:
            continue
        try:
            vals.append(float(val))
        except ValueError:
            pass
    return vals

def fmt_num(x):
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.6f}".rstrip("0").rstrip(".")

with open(raw_path, newline="") as f:
    rows = list(csv.DictReader(f))

groups = defaultdict(list)
for row in rows:
    groups[row["offered_qps"]].append(row)

summary_rows = []
for load in sorted(groups, key=lambda x: int(float(x))):
    group = groups[load]
    first = group[0]
    out = {field: "" for field in summary_fields}
    for field in first:
        if field in numeric_fields:
            vals = numeric_values(group, field)
            out[field] = fmt_num(statistics.median(vals)) if vals else ""
        elif field == "repeat":
            out[field] = "median"
        elif field == "wait_status":
            out[field] = "stable" if all(row.get(field) == "stable" for row in group) else "mixed"
        elif field == "log_file":
            out[field] = ";".join(row.get(field, "") for row in group)
        elif field in out:
            out[field] = first.get(field, "")
    out["samples"] = str(len(group))
    out["achieved_qps_min"] = fmt_num(min(as_float(row, "achieved_qps") for row in group))
    out["achieved_qps_max"] = fmt_num(max(as_float(row, "achieved_qps") for row in group))
    out["read_p99_us_min"] = fmt_num(min(as_float(row, "read_p99_us") for row in group))
    out["read_p99_us_max"] = fmt_num(max(as_float(row, "read_p99_us") for row in group))
    summary_rows.append(out)

with open(summary_path, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=summary_fields)
    writer.writeheader()
    writer.writerows(summary_rows)

with open(aggregate_path, "a", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=summary_fields)
    writer.writerows(summary_rows)
PY
else
  rdma_log "python3 not found; copying raw rows to summary without median aggregation"
  cp "$CSV_FILE" "$SUMMARY_FILE"
fi

rdma_log "raw repeats: $CSV_FILE"
rdma_log "stable median summary: $SUMMARY_FILE"
rdma_log "swap wait log: $WAIT_LOG"
