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
  MODE=cgroup-hermit REDIS_WORKSET_MB=16384 LOCAL_RATIO_PCT=70 \
    ./run_redis_page_sweep.sh

Redis large-value page-size sweep under Hermit. For every requested page size
the script starts a Redis server and a signal-driven Python harness in a cgroup,
loads deterministic large string values, lowers memory.max to force RDMA stores,
keeps that fixed limit, and GET-scans the keys while allowing concurrent reclamation.

Important environment:
  PAGE_SIZES_KB="4 16 32 64 128 256 512 1024 2048"
  REDIS_WORKSET_MB=16384            (total value bytes to load)
  REDIS_VALUE_SIZE=2097152          (bytes per value; default 2 MiB)
  REDIS_SCAN_CHUNK=65536            (read first 64 KiB of each value during
                                     BENCH via GETRANGE; 0 = whole value)
  REDIS_ACTIVE_RATIOS="100"          (percent of keys read per run)
  REDIS_ACCESS_ORDER=sequential     (sequential or random)
  REDIS_ACCESS_SEED=1
  REDIS_CLIENTS=1                   (concurrent GET connections)
  REDIS_INSTANCES=1                 (independent redis-server processes)
  REDIS_CHECKSUM=Y                  (Y: full CRC32; N: length-only fast path)
  REDIS_PORT=6391
  REDIS_SERVER_BIN=/path/to/redis-server
  LOCAL_RATIO_PCT=70                (limit = resident MiB * ratio / 100)
  BENCH_REPEATS=3
  BENCH_CPUS=0                      (Python harness CPU set, e.g. 8-15)
  BENCH_CPU=0                       (legacy alias if BENCH_CPUS is unset)
  REDIS_SERVER_CPU=0                (redis-server CPU)
  REDIS_SERVER_CPUS=0-7             (defaults to REDIS_SERVER_CPU)
  QUIET_INTERVAL_SEC=1
  QUIET_SAMPLES=3
  QUIET_TIMEOUT_SEC=120
  READY_TIMEOUT_SEC=600
  BENCH_TIMEOUT_SEC=3600
  RESTORE_THP=1

Requirements: cgroup v2, active Hermit RDMA backend, debugfs, zswap disabled,
and exactly one block-device swap area. The script owns
/sys/fs/cgroup/hermit-redis.
USAGE
}

if [ "${1:-}" = -h ] || [ "${1:-}" = --help ]; then
  usage
  exit 0
fi

MODE=$(normalize_mode "${requested_mode:-cgroup-hermit}")
[ "$MODE" = cgroup-hermit ] || rdma_die "this benchmark requires MODE=cgroup-hermit"

PAGE_SIZES_KB=${PAGE_SIZES_KB:-"4 16 32 64 128 256 512 1024 2048"}
REDIS_WORKSET_MB=${REDIS_WORKSET_MB:-16384}
REDIS_VALUE_SIZE=${REDIS_VALUE_SIZE:-2097152}
if [ -z "${REDIS_SCAN_CHUNK:-}" ]; then
  if [ "$REDIS_VALUE_SIZE" -ge 65536 ]; then
    REDIS_SCAN_CHUNK=65536
  else
    REDIS_SCAN_CHUNK=0
  fi
fi
REDIS_ACTIVE_RATIOS=${REDIS_ACTIVE_RATIOS:-100}
REDIS_ACCESS_ORDER=${REDIS_ACCESS_ORDER:-sequential}
REDIS_ACCESS_SEED=${REDIS_ACCESS_SEED:-1}
REDIS_CLIENTS=${REDIS_CLIENTS:-1}
REDIS_INSTANCES=${REDIS_INSTANCES:-1}
REDIS_CHECKSUM=${REDIS_CHECKSUM:-Y}
REDIS_PORT=${REDIS_PORT:-6391}
REDIS_SERVER_BIN=${REDIS_SERVER_BIN:-}
LOCAL_RATIO_PCT=${LOCAL_RATIO_PCT:-70}
BENCH_REPEATS=${BENCH_REPEATS:-3}
BENCH_CPU=${BENCH_CPU:-0}
BENCH_CPUS=${BENCH_CPUS:-$BENCH_CPU}
REDIS_SERVER_CPU=${REDIS_SERVER_CPU:-0}
REDIS_SERVER_CPUS=${REDIS_SERVER_CPUS:-$REDIS_SERVER_CPU}
READY_TIMEOUT_SEC=${READY_TIMEOUT_SEC:-600}
BENCH_TIMEOUT_SEC=${BENCH_TIMEOUT_SEC:-3600}
RUN_ID=${requested_run_id:-"$(date +%Y%m%d-%H%M%S)-redis-swapio"}
RESULT_DIR=${requested_result_dir:-"$RESULT_ROOT/$RUN_ID"}
WORKER="$SCRIPT_DIR/redis_bench.py"
CGROUP=${PEBS_CGROUP:-/sys/fs/cgroup/hermit-redis}
CSV="$RESULT_DIR/redis-swapio-summary.csv"
current_worker_pid=
declare -a current_redis_pids=()
declare -a current_redis_logs=()

start_redis_servers() {
  local run_dir=$1 bin index port log pid
  bin=$(find_redis_server_bin)
  current_redis_pids=()
  current_redis_logs=()
  for ((index = 0; index < REDIS_INSTANCES; index++)); do
    port=$((REDIS_PORT + index))
    if [ "$index" -eq 0 ]; then log="$run_dir/redis.log"; else log="$run_dir/redis-${index}.log"; fi
    taskset -c "$REDIS_SERVER_CPUS" "$bin" \
      --port "$port" --bind 127.0.0.1 --protected-mode no \
      --save "" --appendonly no --daemonize no \
      --stop-writes-on-bgsave-error no --rdbcompression no --maxmemory 0 \
      > "$log" 2>&1 &
    pid=$!
    current_redis_pids+=("$pid")
    current_redis_logs+=("$log")
    sudo_write "$pid" "$CGROUP/cgroup.procs"
    enable_benchmark_pebs_pid "$pid"
  done
}

assert_redis_servers_alive() {
  local context=$1 index pid
  for index in "${!current_redis_pids[@]}"; do
    pid=${current_redis_pids[$index]}
    kill -0 "$pid" 2>/dev/null || \
      rdma_die "redis-server instance $index exited $context; see ${current_redis_logs[$index]}"
  done
}

wait_for_redis_ready() {
  local start index log
  start=$(date +%s)
  for index in "${!current_redis_pids[@]}"; do
    log=${current_redis_logs[$index]}
    until grep -q 'Ready to accept connections' "$log" 2>/dev/null; do
      assert_redis_servers_alive "during startup"
      [ "$(( $(date +%s) - start ))" -lt "$READY_TIMEOUT_SEC" ] || \
        rdma_die "timeout waiting for redis-server instance $index; see $log"
      sleep 0.2
    done
  done
}

cleanup_processes() {
  if [ -n "$current_worker_pid" ] && kill -0 "$current_worker_pid" 2>/dev/null; then
    kill -TERM "$current_worker_pid" 2>/dev/null || true
    wait "$current_worker_pid" 2>/dev/null || true
  fi
  local pid
  for pid in "${current_redis_pids[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in "${current_redis_pids[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
  current_worker_pid=
  current_redis_pids=()
  current_redis_logs=()
  sudo_write max "$CGROUP/memory.max" 2>/dev/null || true
}

cleanup() {
  local ret=$?
  cleanup_processes
  restore_thp_state
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

[[ "$REDIS_CLIENTS" =~ ^[0-9]+$ ]] && [ "$REDIS_CLIENTS" -gt 0 ] || \
  rdma_die "REDIS_CLIENTS must be a positive integer"
[[ "$REDIS_INSTANCES" =~ ^[0-9]+$ ]] && [ "$REDIS_INSTANCES" -gt 0 ] || \
  rdma_die "REDIS_INSTANCES must be a positive integer"
[[ "$REDIS_PORT" =~ ^[0-9]+$ ]] && [ "$REDIS_PORT" -gt 0 ] && \
  [ "$REDIS_PORT" -le 65535 ] || rdma_die "REDIS_PORT must be in [1, 65535]"
[ "$((REDIS_PORT + REDIS_INSTANCES - 1))" -le 65535 ] || \
  rdma_die "Redis instance port range exceeds 65535"
[[ "$REDIS_ACCESS_SEED" =~ ^[0-9]+$ ]] || \
  rdma_die "REDIS_ACCESS_SEED must be an unsigned integer"
[[ "$REDIS_VALUE_SIZE" =~ ^[0-9]+$ ]] && [ "$REDIS_VALUE_SIZE" -gt 0 ] || \
  rdma_die "REDIS_VALUE_SIZE must be a positive integer"
[[ "$REDIS_SCAN_CHUNK" =~ ^[0-9]+$ ]] && \
  [ "$REDIS_SCAN_CHUNK" -le "$REDIS_VALUE_SIZE" ] || \
  rdma_die "REDIS_SCAN_CHUNK must be between 0 and REDIS_VALUE_SIZE"
if [ "$REDIS_VALUE_SIZE" -lt 65536 ] || [ "$REDIS_ACCESS_ORDER" != sequential ]; then
  rdma_log "warning: small or random Redis values form few large folios; large-page benefit will be weak"
fi
[[ "$REDIS_ACTIVE_RATIOS" =~ [^[:space:]] ]] || \
  rdma_die "REDIS_ACTIVE_RATIOS must not be empty"
case "$REDIS_ACCESS_ORDER" in
  sequential|random) ;;
  *) rdma_die "REDIS_ACCESS_ORDER must be sequential or random" ;;
esac
for active_ratio in $REDIS_ACTIVE_RATIOS; do
  awk -v ratio="$active_ratio" 'BEGIN { exit !(ratio > 0 && ratio <= 100) }' || \
    rdma_die "REDIS_ACTIVE_RATIOS entries must be in (0, 100]: $active_ratio"
done

[ -x "$WORKER" ] || rdma_die "missing executable worker: $WORKER"
command -v python3 >/dev/null 2>&1 || rdma_die "python3 is required"
taskset -c "$BENCH_CPUS" true >/dev/null 2>&1 || \
  rdma_die "invalid or unavailable BENCH_CPUS CPU list: $BENCH_CPUS"
taskset -c "$REDIS_SERVER_CPUS" true >/dev/null 2>&1 || \
  rdma_die "invalid or unavailable REDIS_SERVER_CPUS CPU list: $REDIS_SERVER_CPUS"
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
  printf 'page_sizes_kb=%s\nrepeats=%s\n' "$PAGE_SIZES_KB" "$BENCH_REPEATS"
  printf 'bench_cpus=%s\nredis_server_cpus=%s\n' "$BENCH_CPUS" "$REDIS_SERVER_CPUS"
  printf 'redis_workset_mb=%s\nredis_value_size=%s\nredis_scan_chunk=%s\nredis_port=%s\n' \
    "$REDIS_WORKSET_MB" "$REDIS_VALUE_SIZE" "$REDIS_SCAN_CHUNK" "$REDIS_PORT"
  printf 'redis_active_ratios=%s\nredis_access_order=%s\nredis_access_seed=%s\nredis_clients=%s\n' \
    "$REDIS_ACTIVE_RATIOS" "$REDIS_ACCESS_ORDER" "$REDIS_ACCESS_SEED" \
    "$REDIS_CLIENTS"
  printf 'redis_checksum=%s\nredis_instances=%s\n' "$REDIS_CHECKSUM" "$REDIS_INSTANCES"
  printf 'redis_server_bin=%s\n' "$(find_redis_server_bin)"
  printf 'backend=%s\n' "$(detect_rswap_backend)"
  printf 'swap:\n'
  sed 's/^/  /' /proc/swaps
} > "$RESULT_DIR/environment.txt"
printf 'page_kb,order,repeat,mode,active_ratio_requested,actual_access_pct,access_order,access_seed,clients,instances,clients_per_instance,resident_mb,limit_mb,value_size,keys,active_keys,useful_bytes,populate_sec,bench_sec,get_qps,useful_gib_per_sec,checksum_errors,pswpout_delta,target_stores_delta,target_loads_delta,target_fallback_delta,target_errors_delta,total_store_bytes,large_store_bytes,large_store_pct,swapout_gib,protocol_gib,protocol_gib_per_sec,pswpin_delta,bench_pswpout_delta,backend_loads_delta,total_load_bytes,large_load_bytes,large_load_pct,swapin_gib,load_protocol_gib,load_protocol_gib_per_sec,remote_bytes_per_useful_byte,normalized_read_amplification,backend_loads_per_get\n' > "$CSV"

REDIS_PORTS=$(seq -s, "$REDIS_PORT" "$((REDIS_PORT + REDIS_INSTANCES - 1))")
export REDIS_WORKSET_MB REDIS_VALUE_SIZE REDIS_SCAN_CHUNK REDIS_PORT REDIS_PORTS
export REDIS_ACCESS_ORDER REDIS_ACCESS_SEED REDIS_CLIENTS REDIS_ACTIVE_RATIO
export REDIS_CHECKSUM

for kb in $PAGE_SIZES_KB; do
  order=$(page_order "$kb") || rdma_die "unsupported page size: ${kb} KiB"
  configure_page_size "$kb" "$order" || rdma_die "THP size ${kb} KiB is unavailable"
  effective=$(sudo_cat "$EFFECTIVE_MASK_FILE")
  (( (effective & (1 << order)) != 0 )) || rdma_die "effective mask $effective lacks order $order"

  for active_ratio in $REDIS_ACTIVE_RATIOS; do
    REDIS_ACTIVE_RATIO=$active_ratio
    ratio_label=${active_ratio//./p}
    for repeat in $(seq 1 "$BENCH_REPEATS"); do
    run_dir="$RESULT_DIR/${kb}k/${ratio_label}/r${repeat}"
    worker_log="$run_dir/redis-bench.log"
    before="$run_dir/order-before.txt"
    after="$run_dir/order-after.txt"
    mkdir -p "$run_dir"
    sudo_write max "$CGROUP/memory.max"

    start_redis_servers "$run_dir"
    wait_for_redis_ready

    taskset -c "$BENCH_CPUS" python3 "$WORKER" > "$worker_log" 2>&1 &
    current_worker_pid=$!
    wait_for_log '^WAITING ' "$worker_log" "$READY_TIMEOUT_SEC"
    kill -USR1 "$current_worker_pid"
    wait_for_log '^READY ' "$worker_log" "$READY_TIMEOUT_SEC"

    populate_sec=$(awk -F'populate_sec=' '/^READY / { print $2; exit }' "$worker_log")
    keys=$(awk -F'keys=' '/^READY / { print $2; exit }' "$worker_log" | awk '{ print $1 }')
    value_size=$(awk -F'value_size=' '/^READY / { print $2; exit }' "$worker_log" | awk '{ print $1 }')
    [ -n "$populate_sec" ] || rdma_die "missing READY metrics; see $worker_log"

    resident_bytes=$(sudo_cat "$CGROUP/memory.current")
    resident_mb=$((resident_bytes / 1024 / 1024))
    limit_mb=$(max_int 256 $((resident_mb * LOCAL_RATIO_PCT / 100)))
    [ "$limit_mb" -lt "$resident_mb" ] || \
      rdma_die "computed limit ${limit_mb} MiB is not below resident ${resident_mb} MiB; lower LOCAL_RATIO_PCT or enlarge the workset"
    required_swap_mb=$((resident_mb - limit_mb))
    check_swap_capacity "$required_swap_mb"

    for index in "${!current_redis_pids[@]}"; do
      sudo_cat "/proc/${current_redis_pids[$index]}/smaps_rollup" > \
        "$run_dir/redis-${index}-smaps-before.txt"
    done
    sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-before.txt"
    wait_for_stores_quiet
    snapshot_sweep_stats "$before"
    pswpout_before=$(read_vmstat_key pswpout)
    stores_before=$(order_total_stores)
    oom_before=$(memory_event oom_kill)
    start_ns=$(date +%s%N)
    sudo_write "$((limit_mb * 1024 * 1024))" "$CGROUP/memory.max"
    last_change_ns=$start_ns
    wait_for_stores_quiet "$last_change_ns"
    snapshot_sweep_stats "$after"
    pswpout_after=$(read_vmstat_key pswpout)
    sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-after.txt"
    sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after.txt"
    oom_after=$(memory_event oom_kill)
    [ "$oom_after" -eq "$oom_before" ] || rdma_die "cgroup OOM killed redis; see $run_dir"
    assert_redis_servers_alive "during reclaim"
    kill -0 "$current_worker_pid" 2>/dev/null || rdma_die "redis harness exited during reclaim; see $worker_log"

    target_stores=$(( $(order_field "$after" "$order" 3) - $(order_field "$before" "$order" 3) ))
    target_loads=$(( $(order_field "$after" "$order" 4) - $(order_field "$before" "$order" 4) ))
    target_fallback=$(( $(order_field "$after" "$order" 5) - $(order_field "$before" "$order" 5) ))
    target_errors=$(( $(order_field "$after" "$order" 6) - $(order_field "$before" "$order" 6) ))
    pswpout_delta=$((pswpout_after - pswpout_before))
    stores_after=$(order_total_stores)
    stores_delta=$((stores_after - stores_before))

    completion_ms=$(( (last_change_ns - start_ns) / 1000000 ))
    [ "$completion_ms" -gt 0 ] || completion_ms=1
    summarize_stores "$before" "$after" "$completion_ms"
    swapout_gib=$(awk -v p="$pswpout_delta" 'BEGIN { printf "%.6f", p*4096/1073741824 }')
    protocol_gib=$(awk -v b="$total_store_bytes" 'BEGIN { printf "%.6f", b/1073741824 }')

    # Keep the same hard budget throughout the GET scan.
    snapshot_fixed_memory_budget "$CGROUP" "$run_dir" before-swapin "$((limit_mb * 1024 * 1024))"
    load_before="$run_dir/order-before-swapin.txt"
    load_after="$run_dir/order-after-swapin.txt"
    snapshot_sweep_stats "$load_before"
    pswpin_before=$(read_vmstat_key pswpin)
    bench_pswpout_before=$(read_vmstat_key pswpout)
    kill -USR2 "$current_worker_pid"
    wait_for_log '^BENCH ' "$worker_log" "$BENCH_TIMEOUT_SEC" 1
    snapshot_sweep_stats "$load_after"
    pswpin_after=$(read_vmstat_key pswpin)
    bench_pswpout_after=$(read_vmstat_key pswpout)
    sudo_cat "$CGROUP/memory.stat" > "$run_dir/memory-stat-after-swapin.txt"
    sudo_cat "$CGROUP/memory.events" > "$run_dir/memory-events-after-swapin.txt"
    snapshot_fixed_memory_budget "$CGROUP" "$run_dir" after-swapin "$((limit_mb * 1024 * 1024))"

    bench_sec=$(log_field BENCH sec "$worker_log")
    active_keys=$(log_field BENCH active_keys "$worker_log")
    useful_bytes=$(log_field BENCH bytes "$worker_log")
    actual_access_pct=$(log_field BENCH actual_access_pct "$worker_log")
    actual_clients=$(log_field BENCH clients "$worker_log")
    actual_instances=$(log_field BENCH instances "$worker_log")
    actual_clients_per_instance=$(log_field BENCH clients_per_instance "$worker_log")
    get_qps=$(log_field BENCH get_qps "$worker_log")
    useful_gib_per_sec=$(log_field BENCH useful_gib_per_sec "$worker_log")
    checksum_errors=$(log_field BENCH checksum_errors "$worker_log")
    [ -n "$bench_sec" ] && [ -n "$active_keys" ] && \
      [ -n "$useful_bytes" ] && [ -n "$actual_access_pct" ] && \
      [ -n "$actual_clients" ] && [ -n "$actual_instances" ] && \
      [ -n "$actual_clients_per_instance" ] && [ -n "$get_qps" ] && \
      [ -n "$useful_gib_per_sec" ] || \
      rdma_die "missing BENCH metrics; see $worker_log"
    [ "${checksum_errors:-1}" -eq 0 ] || rdma_die "redis GET checksum mismatch; see $worker_log"

    pswpin_delta=$((pswpin_after - pswpin_before))
    bench_pswpout_delta=$((bench_pswpout_after - bench_pswpout_before))
    target_loads=$(( $(order_field "$load_after" "$order" 4) - $(order_field "$load_before" "$order" 4) ))
    target_fallback=$(( $(order_field "$load_after" "$order" 5) - $(order_field "$load_before" "$order" 5) ))
    target_errors=$(( $(order_field "$load_after" "$order" 6) - $(order_field "$load_before" "$order" 6) ))

    summarize_loads "$load_before" "$load_after" "$bench_sec"
    swapin_gib=$(awk -v p="$pswpin_delta" 'BEGIN { printf "%.6f", p*4096/1073741824 }')
    load_protocol_gib=$(awk -v b="$total_load_bytes" 'BEGIN { printf "%.6f", b/1073741824 }')
    remote_bytes_per_useful_byte=$(awk -v remote="$total_load_bytes" \
      -v useful="$useful_bytes" \
      'BEGIN { printf "%.6f", (useful > 0 ? remote / useful : 0) }')
    normalized_read_amplification=$(awk -v remote="$total_load_bytes" \
      -v useful="$useful_bytes" -v stored="$total_store_bytes" \
      -v keys="$keys" -v value_size="$value_size" '
      BEGIN {
        dataset = keys * value_size
        amplification = 0
        if (useful > 0 && stored > 0)
          amplification = remote * dataset / (useful * stored)
        printf "%.6f", amplification
      }')
    backend_loads_per_get=$(awk -v loads="$backend_loads_delta" \
      -v gets="$active_keys" \
      'BEGIN { printf "%.6f", (gets > 0 ? loads / gets : 0) }')
    oom_after_swapin=$(memory_event oom_kill)
    [ "$oom_after_swapin" -eq "$oom_before" ] || rdma_die "cgroup OOM during GET scan; see $run_dir"
    [ "$pswpin_delta" -gt 0 ] || rdma_die "GET scan caused no swap-ins; see $run_dir"
    [[ "$total_load_bytes" =~ ^[0-9]+$ ]] && [ "$total_load_bytes" -gt 0 ] || \
      rdma_die "Hermit recorded no backend loads; see $run_dir"

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$kb" "$order" "$repeat" "$MODE" "$active_ratio" \
      "$actual_access_pct" "$REDIS_ACCESS_ORDER" "$REDIS_ACCESS_SEED" \
      "$actual_clients" "$actual_instances" "$actual_clients_per_instance" \
      "$resident_mb" "$limit_mb" "$value_size" "$keys" \
      "$active_keys" "$useful_bytes" "$populate_sec" "$bench_sec" "$get_qps" \
      "$useful_gib_per_sec" "$checksum_errors" \
      "$pswpout_delta" "$target_stores" "$target_loads" "$target_fallback" \
      "$target_errors" "$total_store_bytes" "$large_store_bytes" \
      "$large_store_pct" "$swapout_gib" "$protocol_gib" \
      "$protocol_gib_per_sec" "$pswpin_delta" "$bench_pswpout_delta" \
      "$backend_loads_delta" "$total_load_bytes" "$large_load_bytes" "$large_load_pct" \
      "$swapin_gib" "$load_protocol_gib" "$load_protocol_gib_per_sec" \
      "$remote_bytes_per_useful_byte" "$normalized_read_amplification" \
      "$backend_loads_per_get" >> "$CSV"
    rdma_log "page=${kb}k ratio=$active_ratio repeat=$repeat instances=$actual_instances clients=$actual_clients stores=$stores_delta store_large=${large_store_pct}% swapout=${protocol_gib_per_sec}GiB/s loads=$backend_loads_delta load_large=${large_load_pct}% useful=${useful_gib_per_sec}GiB/s read_amp=${normalized_read_amplification} bench_sec=$bench_sec checksum_errors=$checksum_errors"
    cleanup_processes
    done
  done
done

trap - EXIT INT TERM
cleanup
