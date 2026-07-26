#!/usr/bin/env bash

set -euo pipefail

rdma_log() {
  printf '[rdma-bench] %s\n' "$*" >&2
}

rdma_die() {
  printf '[rdma-bench] error: %s\n' "$*" >&2
  exit 1
}

detect_kernel_tag() {
  local rel
  rel=$(uname -r)
  case "$rel" in
    *5.14*) printf 'hermit-5' ;;
    *6.6*) printf 'hermit-6' ;;
    *) printf '%s' "$rel" ;;
  esac
}

bool_to_yn() {
  case "${1:-}" in
    1|y|Y|yes|YES|true|TRUE|on|ON) printf 'Y' ;;
    *) printf 'N' ;;
  esac
}

normalize_mode() {
  case "${1:-local}" in
    cgroup_linux) printf 'cgroup-linux' ;;
    cgroup_hermit) printf 'cgroup-hermit' ;;
    *) printf '%s' "${1:-local}" ;;
  esac
}

min_int() {
  if [ "$1" -lt "$2" ]; then
    printf '%s' "$1"
  else
    printf '%s' "$2"
  fi
}

max_int() {
  if [ "$1" -gt "$2" ]; then
    printf '%s' "$1"
  else
    printf '%s' "$2"
  fi
}

cpu_range() {
  local start=$1
  local count=$2

  if [ "$count" -le 0 ]; then
    printf ''
  elif [ "$count" -eq 1 ]; then
    printf '%s' "$start"
  else
    printf '%s-%s' "$start" "$((start + count - 1))"
  fi
}

join_csv() {
  local IFS=,
  printf '%s' "$*"
}

socket_primary_cpus() {
  local socket=$1
  lscpu -p=CPU,NODE,SOCKET,CORE 2>/dev/null |
    awk -F, -v sock="$socket" '$0 !~ /^#/ && $3 == sock && !seen[$4]++ { print $1 }'
}

socket_all_cpus() {
  local socket=$1
  lscpu -p=CPU,NODE,SOCKET,CORE 2>/dev/null |
    awk -F, -v sock="$socket" '$0 !~ /^#/ && $3 == sock { print $1 }'
}

socket_numa_node() {
  local socket=$1
  lscpu -p=CPU,NODE,SOCKET,CORE 2>/dev/null |
    awk -F, -v sock="$socket" '$0 !~ /^#/ && $3 == sock { print $2; exit }'
}

detect_socket_core_layout() {
  local socket=$1
  local primary=()
  local all_cpus=()
  local selected=()
  local reserve=()
  local cpu used selected_cpu mem_threads mutilate_threads primary_count

  while read -r cpu; do
    [ -n "$cpu" ] && primary+=("$cpu")
  done < <(socket_primary_cpus "$socket")

  if [ "${#primary[@]}" -eq 0 ]; then
    return 1
  fi

  if [ "$BENCH_USE_SMT" = "1" ]; then
    primary=()
    while read -r cpu; do
      [ -n "$cpu" ] && primary+=("$cpu")
    done < <(socket_all_cpus "$socket")
  fi

  while read -r cpu; do
    [ -n "$cpu" ] && all_cpus+=("$cpu")
  done < <(socket_all_cpus "$socket")

  primary_count=${#primary[@]}
  if [ -z "${MEMCACHED_THREADS:-}" ]; then
    mem_threads=$(max_int 1 $((primary_count / 2)))
  else
    mem_threads=$MEMCACHED_THREADS
  fi

  if [ -z "${MUTILATE_THREADS:-}" ]; then
    mutilate_threads=$(max_int 1 $((primary_count - mem_threads)))
  else
    mutilate_threads=$MUTILATE_THREADS
  fi

  if [ "$((mem_threads + mutilate_threads))" -gt "$primary_count" ]; then
    mem_threads=$(max_int 1 $((primary_count / 2)))
    mutilate_threads=$(max_int 1 $((primary_count - mem_threads)))
  fi

  if [ -z "${MEMCACHED_CORES:-}" ]; then
    MEMCACHED_CORES=$(join_csv "${primary[@]:0:$mem_threads}")
  fi
  if [ -z "${MUTILATE_CORES:-}" ]; then
    MUTILATE_CORES=$(join_csv "${primary[@]:$mem_threads:$mutilate_threads}")
  fi

  selected=("${primary[@]:0:$((mem_threads + mutilate_threads))}")
  for cpu in "${all_cpus[@]}"; do
    used=0
    for selected_cpu in "${selected[@]}"; do
      if [ "$cpu" = "$selected_cpu" ]; then
        used=1
        break
      fi
    done
    [ "$used" = "0" ] && reserve+=("$cpu")
  done

  HERMIT_RESERVED_CORES=$(join_csv "${reserve[@]}")
  MEMCACHED_THREADS=$mem_threads
  MUTILATE_THREADS=$mutilate_threads
  BENCH_NUMA_NODE=${BENCH_NUMA_NODE:-$(socket_numa_node "$socket")}
  return 0
}

RDMA_DIR=${RDMA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}
HERMIT6_ROOT=${HERMIT6_ROOT:-$(cd "$RDMA_DIR/../.." && pwd)}
WORKSPACE_ROOT=${WORKSPACE_ROOT:-$(cd "$HERMIT6_ROOT/.." && pwd)}
HERMIT5_ROOT=${HERMIT5_ROOT:-"$WORKSPACE_ROOT/hermit-5.14"}

MODE=$(normalize_mode "${MODE:-local}")
KERNEL_TAG=${KERNEL_TAG:-$(detect_kernel_tag)}
PORT=${PORT:-11211}
SERVER_ADDR=${SERVER_ADDR:-127.0.0.1}
RECORDS=${RECORDS:-32000000}
KEYSIZE=${KEYSIZE:-fb_key}
VALUESIZE=${VALUESIZE:-fb_value}
IADIST=${IADIST:-fb_ia}
UPDATE_RATIO=${UPDATE_RATIO:-0.002}
DURATION=${DURATION:-40}
LOADS=${LOADS:-"500000 750000 1000000 1250000 1500000 2000000 3000000 4000000"}
BENCH_REPEATS=${BENCH_REPEATS:-3}

CGROUP_NAME=${CGROUP_NAME:-mc}
CGROUP_LIMIT_MB=${CGROUP_LIMIT_MB:-}
LOCAL_RATIO_PCT=${LOCAL_RATIO_PCT:-}
MEMCACHED_MEM_MB=${MEMCACHED_MEM_MB:-16384}
MEMCACHED_MAX_CONN=${MEMCACHED_MAX_CONN:-32768}
STHD_CNT=${STHD_CNT:-16}
RECLAIM_MODE=${RECLAIM_MODE:-1}
RECLAIM_HEADROOM_PAGES=${RECLAIM_HEADROOM_PAGES:-65536}
LAZY_POLL=${LAZY_POLL:-N}
BYPASS_SWAPCACHE=${BYPASS_SWAPCACHE:-Y}
HERMIT_SWAPOUT_POLICY=${HERMIT_SWAPOUT_POLICY:-exclusive}
RSWAP_REQUIRED_BACKEND=${RSWAP_REQUIRED_BACKEND:-rdma}

CORE_LAYOUT=${CORE_LAYOUT:-socket}
BENCH_SOCKET=${BENCH_SOCKET:-1}
BENCH_NUMA_NODE=${BENCH_NUMA_NODE:-}
BENCH_NUMACTL=${BENCH_NUMACTL:-1}
BENCH_USE_SMT=${BENCH_USE_SMT:-0}
WAIT_SWAP_STABLE=${WAIT_SWAP_STABLE:-1}
SWAP_STABLE_INTERVAL_SEC=${SWAP_STABLE_INTERVAL_SEC:-5}
SWAP_STABLE_QUIET_SAMPLES=${SWAP_STABLE_QUIET_SAMPLES:-3}
SWAP_STABLE_TIMEOUT_SEC=${SWAP_STABLE_TIMEOUT_SEC:-300}
SWAP_STABLE_MAX_DELTA=${SWAP_STABLE_MAX_DELTA:-0}
WAIT_STABLE_KEYS=${WAIT_STABLE_KEYS:-"pswpin pswpout backend_loads backend_stores"}

RESULT_ROOT=${RESULT_ROOT:-"$RDMA_DIR/results"}
RUN_ID=${RUN_ID:-"$(date +%Y%m%d-%H%M%S)-${KERNEL_TAG}-${MODE}"}
RESULT_DIR=${RESULT_DIR:-"$RESULT_ROOT/$RUN_ID"}
PID_FILE=${PID_FILE:-"$RESULT_DIR/memcached.pid"}
AGGREGATE_CSV=${AGGREGATE_CSV:-"$RESULT_ROOT/all_runs.csv"}

find_memcached_bin() {
  local candidates=()
  if [ -n "${MEMCACHED_BIN:-}" ]; then
    candidates+=("$MEMCACHED_BIN")
  fi
  candidates+=("$PWD/memcached/memcached")
  candidates+=("$RDMA_DIR/memcached/memcached")

  local p
  for p in "${candidates[@]}"; do
    if [ -x "$p" ]; then
      printf '%s' "$p"
      return 0
    fi
  done

  if command -v memcached >/dev/null 2>&1; then
    command -v memcached
    return 0
  fi

  rdma_die "cannot find memcached; set MEMCACHED_BIN=/path/to/memcached"
}

find_mutilate_bin() {
  local candidates=()
  if [ -n "${MUTILATE_BIN:-}" ]; then
    candidates+=("$MUTILATE_BIN")
  fi
  candidates+=("$PWD/mutilate/mutilate")
  candidates+=("$RDMA_DIR/mutilate/mutilate")

  local p
  for p in "${candidates[@]}"; do
    if [ -x "$p" ]; then
      printf '%s' "$p"
      return 0
    fi
  done

  if command -v mutilate >/dev/null 2>&1; then
    command -v mutilate
    return 0
  fi

  rdma_die "cannot find mutilate; set MUTILATE_BIN=/path/to/mutilate"
}

detect_core_layout() {
  local total reserve available mem_threads mutilate_threads
  total=${CPU_TOTAL:-$(getconf _NPROCESSORS_ONLN)}

  if [ "$CORE_LAYOUT" = "socket" ] && detect_socket_core_layout "$BENCH_SOCKET"; then
    CPU_TOTAL=$total
    export CPU_TOTAL MEMCACHED_THREADS MUTILATE_THREADS
    export MEMCACHED_CORES MUTILATE_CORES HERMIT_RESERVED_CORES BENCH_NUMA_NODE
    return 0
  fi

  reserve=0

  if [[ "$MODE" == *hermit* ]]; then
    reserve=${HERMIT_RESERVE_CORES:-$STHD_CNT}
    if [ "$reserve" -ge "$total" ]; then
      reserve=$((total / 4))
    fi
  fi

  available=$((total - reserve))
  if [ "$available" -lt 2 ]; then
    reserve=0
    available=$total
  fi

  if [ -z "${MEMCACHED_THREADS:-}" ]; then
    if [ "$available" -ge 32 ]; then
      mem_threads=16
    else
      mem_threads=$(max_int 1 $((available / 2)))
    fi
  else
    mem_threads=$MEMCACHED_THREADS
  fi

  if [ -z "${MUTILATE_THREADS:-}" ]; then
    mutilate_threads=$(min_int 16 $((available - mem_threads)))
    mutilate_threads=$(max_int 1 "$mutilate_threads")
  else
    mutilate_threads=$MUTILATE_THREADS
  fi

  if [ "$((mem_threads + mutilate_threads))" -gt "$available" ]; then
    mem_threads=$(max_int 1 $((available / 2)))
    mutilate_threads=$(max_int 1 $((available - mem_threads)))
  fi

  if [ -z "${MEMCACHED_CORES:-}" ]; then
    MEMCACHED_CORES=$(cpu_range 0 "$mem_threads")
  fi
  if [ -z "${MUTILATE_CORES:-}" ]; then
    MUTILATE_CORES=$(cpu_range "$mem_threads" "$mutilate_threads")
  fi
  if [ "$reserve" -gt 0 ]; then
    HERMIT_RESERVED_CORES=$(cpu_range "$((total - reserve))" "$reserve")
  else
    HERMIT_RESERVED_CORES=""
  fi

  MEMCACHED_THREADS=$mem_threads
  MUTILATE_THREADS=$mutilate_threads
  CPU_TOTAL=$total
  export CPU_TOTAL MEMCACHED_THREADS MUTILATE_THREADS
  export MEMCACHED_CORES MUTILATE_CORES HERMIT_RESERVED_CORES
}

build_bench_prefix() {
  BENCH_CMD_PREFIX=()
  if [ "$BENCH_NUMACTL" = "1" ] && [ -n "${BENCH_NUMA_NODE:-}" ] &&
     command -v numactl >/dev/null 2>&1; then
    BENCH_CMD_PREFIX=(numactl "--cpunodebind=$BENCH_NUMA_NODE" "--membind=$BENCH_NUMA_NODE")
  fi
}

sudo_write() {
  local value=$1
  local path=$2

  if [ "$(id -u)" -eq 0 ]; then
    printf '%s\n' "$value" > "$path"
  else
    printf '%s\n' "$value" | sudo tee "$path" >/dev/null
  fi
}

sudo_mkdir() {
  if [ "$(id -u)" -eq 0 ]; then
    mkdir -p "$1"
  else
    sudo mkdir -p "$1"
  fi
}

sudo_test() {
  if [ "$(id -u)" -eq 0 ]; then
    test "$@"
  else
    sudo test "$@"
  fi
}

sudo_cat() {
  if [ "$(id -u)" -eq 0 ]; then
    cat "$1"
  else
    sudo cat "$1"
  fi
}

cgroup_version() {
  if [ -f /sys/fs/cgroup/cgroup.controllers ]; then
    printf '2'
  elif [ -d /sys/fs/cgroup/memory ]; then
    printf '1'
  else
    printf '0'
  fi
}

cgroup_path() {
  case "$(cgroup_version)" in
    2) printf '/sys/fs/cgroup/%s' "$CGROUP_NAME" ;;
    1) printf '/sys/fs/cgroup/memory/%s' "$CGROUP_NAME" ;;
    *) rdma_die "memory cgroup is not mounted" ;;
  esac
}

setup_cgroup() {
  local ver path
  ver=$(cgroup_version)
  path=$(cgroup_path)

  if [ "$ver" = "2" ] && [ -w /sys/fs/cgroup/cgroup.subtree_control ]; then
    sudo_write "+memory" /sys/fs/cgroup/cgroup.subtree_control || true
  fi

  sudo_mkdir "$path"

  if [ "$ver" = "2" ]; then
    [ -e "$path/memory.max" ] && sudo_write max "$path/memory.max"
    [ -e "$path/memory.swap.max" ] && sudo_write max "$path/memory.swap.max"
  else
    [ -e "$path/memory.limit_in_bytes" ] && sudo_write -1 "$path/memory.limit_in_bytes"
  fi
}

set_cgroup_limit_mb() {
  local mb=$1
  local path ver bytes
  path=$(cgroup_path)
  ver=$(cgroup_version)

  if [ "$mb" = "max" ] || [ "$mb" = "0" ] || [ -z "$mb" ]; then
    if [ "$ver" = "2" ]; then
      sudo_write max "$path/memory.max"
    else
      sudo_write 9223372036854771712 "$path/memory.limit_in_bytes"
    fi
    return 0
  fi

  bytes=$((mb * 1024 * 1024))
  if [ "$ver" = "2" ]; then
    sudo_write "$bytes" "$path/memory.max"
  else
    sudo_write "$bytes" "$path/memory.limit_in_bytes"
  fi
}

move_pid_to_cgroup() {
  local pid=$1
  sudo_write "$pid" "$(cgroup_path)/cgroup.procs"
}

cgroup_current_bytes() {
  local path
  path=$(cgroup_path)
  if [ -r "$path/memory.current" ]; then
    cat "$path/memory.current"
  elif [ -r "$path/memory.usage_in_bytes" ]; then
    cat "$path/memory.usage_in_bytes"
  else
    printf '0'
  fi
}

cgroup_value() {
  local file=$1
  local path

  [[ "$MODE" == cgroup-* ]] || {
    printf '0'
    return 0
  }
  path=$(cgroup_path)
  if [ -r "$path/$file" ]; then
    cat "$path/$file"
  else
    printf '0'
  fi
}

cgroup_event_value() {
  local key=$1
  local path

  [[ "$MODE" == cgroup-* ]] || {
    printf '0'
    return 0
  }
  path=$(cgroup_path)
  if [ -r "$path/memory.events" ]; then
    awk -v key="$key" '$1 == key { print $2; found=1 } END { if (!found) print 0 }' \
      "$path/memory.events"
  else
    printf '0'
  fi
}

apply_requested_cgroup_limit() {
  local cur limit_mb

  [[ "$MODE" == cgroup-* ]] || return 0
  setup_cgroup

  if [ -n "$CGROUP_LIMIT_MB" ]; then
    set_cgroup_limit_mb "$CGROUP_LIMIT_MB"
    printf '%s' "$CGROUP_LIMIT_MB"
    return 0
  fi

  if [ -z "$LOCAL_RATIO_PCT" ]; then
    LOCAL_RATIO_PCT=70
  fi

  cur=$(cgroup_current_bytes)
  if [ "$cur" -le 0 ]; then
    set_cgroup_limit_mb max
    printf 'max'
    return 0
  fi

  limit_mb=$((cur * LOCAL_RATIO_PCT / 100 / 1024 / 1024))
  limit_mb=$(max_int 256 "$limit_mb")
  set_cgroup_limit_mb "$limit_mb"
  CGROUP_LIMIT_MB=$limit_mb
  export CGROUP_LIMIT_MB LOCAL_RATIO_PCT
  printf '%s' "$limit_mb"
}

mount_debugfs_if_needed() {
  if sudo_test -d /sys/kernel/debug/hermit 2>/dev/null; then
    return 0
  fi
  if mountpoint -q /sys/kernel/debug; then
    return 0
  fi
  if [ "$(id -u)" -eq 0 ]; then
    mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
  else
    sudo mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
  fi
}

set_debugfs_file() {
  local file=$1
  local value=$2

  if sudo_test -e "$file" 2>/dev/null; then
    sudo_write "$value" "$file"
  fi
}

set_hermit_flag() {
  set_debugfs_file "/sys/kernel/debug/hermit/$1" "$2"
}

read_hermit_counter() {
  local key=$1
  local file="/sys/kernel/debug/hermit/$key"

  if sudo_test -r "$file" 2>/dev/null; then
    sudo_cat "$file"
    return 0
  fi
  printf '0'
}

detect_rswap_backend() {
  if sudo_test -d /sys/kernel/debug/rswap_rdma 2>/dev/null; then
    printf 'rdma'
  elif [ -d /sys/module/rswap_client ] &&
       [ -r /sys/module/rswap_client/parameters/sip ]; then
    # The hardware OFED backend does not expose the optional rswap_rdma
    # debugfs counters in every revision. A loaded module with live RDMA
    # connection parameters is still an RDMA backend.
    printf 'rdma'
  elif sudo_test -d /sys/kernel/debug/rswap_dram 2>/dev/null; then
    printf 'dram'
  else
    printf 'none'
  fi
}

configure_hermit_mode() {
  local flag backend
  mount_debugfs_if_needed

  if ! sudo_test -d /sys/kernel/debug/hermit 2>/dev/null; then
    rdma_log "Hermit debugfs is absent; skip Hermit mode configuration"
    return 0
  fi

  for flag in bypass_swapcache batch_swapout batch_tlb batch_io batch_account \
              vaddr_swapout speculative_io speculative_lock lazy_poll \
              apt_reclaim exclusive_swapout swap_thread prefetch_thread; do
    set_hermit_flag "$flag" N
  done

  set_debugfs_file /sys/kernel/debug/hermit/sthd_cnt "$STHD_CNT"
  set_debugfs_file /sys/kernel/debug/hermit/reclaim_mode "$RECLAIM_MODE"
  set_debugfs_file /sys/kernel/debug/hermit/reclaim_headroom_pages \
    "$RECLAIM_HEADROOM_PAGES"

  case "$MODE" in
    cgroup-hermit|hermit)
      set_hermit_flag vaddr_swapout Y
      set_hermit_flag batch_swapout Y
      set_hermit_flag batch_tlb Y
      set_hermit_flag batch_io Y
      set_hermit_flag batch_account Y
      set_hermit_flag bypass_swapcache "$(bool_to_yn "$BYPASS_SWAPCACHE")"
      set_hermit_flag speculative_io Y
      set_hermit_flag speculative_lock Y
      set_hermit_flag lazy_poll "$(bool_to_yn "$LAZY_POLL")"
      set_hermit_flag apt_reclaim Y
      set_hermit_flag swap_thread Y
      set_hermit_flag prefetch_thread N
      case "$HERMIT_SWAPOUT_POLICY" in
        exclusive)
          set_hermit_flag exclusive_swapout Y
          ;;
        writethrough|write-through)
          set_hermit_flag exclusive_swapout N
          ;;
        *)
          rdma_die "unknown HERMIT_SWAPOUT_POLICY=$HERMIT_SWAPOUT_POLICY; use exclusive or writethrough"
          ;;
      esac
      backend=$(detect_rswap_backend)
      if [ -n "$RSWAP_REQUIRED_BACKEND" ] && [ "$backend" != "$RSWAP_REQUIRED_BACKEND" ]; then
        rdma_die "Hermit benchmark requires rswap backend '$RSWAP_REQUIRED_BACKEND', found '$backend'. Load the RDMA client or set RSWAP_REQUIRED_BACKEND= to disable this check."
      fi
      set_debugfs_file /sys/kernel/debug/hermit/sthd_cnt "$STHD_CNT"
      set_debugfs_file /sys/kernel/debug/hermit/reclaim_mode "$RECLAIM_MODE"
      set_debugfs_file /sys/kernel/debug/hermit/reclaim_headroom_pages \
        "$RECLAIM_HEADROOM_PAGES"
      ;;
    cgroup-linux|linux|local)
      if sudo_test -d /sys/kernel/debug/rswap_rdma 2>/dev/null || \
         sudo_test -d /sys/kernel/debug/rswap_dram 2>/dev/null; then
        rdma_log "warning: an rswap backend is loaded; $MODE is not a pure Linux baseline unless rswap-client is unloaded"
      fi
      ;;
    *)
      rdma_die "unknown MODE=$MODE; use local, cgroup-linux, or cgroup-hermit"
      ;;
  esac
}

hermit_stats_helper() {
  local p
  for p in /bin/hermit_swap_stats \
           "$HERMIT6_ROOT/tools/qemu-dram/_work/hermit_swap_stats" \
           "$HERMIT5_ROOT/tools/qemu-dram/hermit_swap_stats"; do
    if [ -x "$p" ]; then
      printf '%s' "$p"
      return 0
    fi
  done
  return 1
}

reset_hermit_stats() {
  local helper
  if helper=$(hermit_stats_helper); then
    "$helper" reset "${1:-bench}" || true
  elif [ -f "$HERMIT5_ROOT/linux-5.14-rc5/tools/hermit/syscaller.py" ]; then
    python3 "$HERMIT5_ROOT/linux-5.14-rc5/tools/hermit/syscaller.py" reset || true
  fi
}

dump_hermit_stats() {
  local helper
  if helper=$(hermit_stats_helper); then
    "$helper" stats "${1:-bench}" || true
  elif [ -f "$HERMIT5_ROOT/linux-5.14-rc5/tools/hermit/syscaller.py" ]; then
    python3 "$HERMIT5_ROOT/linux-5.14-rc5/tools/hermit/syscaller.py" stats || true
  fi
}

read_vmstat_key() {
  awk -v k="$1" '$1 == k { print $2; found=1 } END { if (!found) print 0 }' /proc/vmstat
}

read_debug_counter() {
  local key=$1
  local dir file

  for dir in /sys/kernel/debug/rswap_rdma /sys/kernel/debug/rswap_dram; do
    file="$dir/$key"
    if sudo_test -r "$file" 2>/dev/null; then
      sudo_cat "$file"
      return 0
    fi
  done
  printf '0'
}

read_activity_counter() {
  case "$1" in
    pswpin|pswpout|pgfault|pgmajfault)
      read_vmstat_key "$1"
      ;;
    backend_loads)
      read_debug_counter loads
      ;;
    backend_stores)
      read_debug_counter stores
      ;;
    backend_load_misses)
      read_debug_counter load_misses
      ;;
    backend_errors)
      read_debug_counter errors
      ;;
    hermit_swapout_native_fallbacks)
      read_hermit_counter swapout_native_fallbacks
      ;;
    hermit_swapout_exclusive_completions)
      read_hermit_counter swapout_exclusive_completions
      ;;
    hermit_swapout_writethrough_completions)
      read_hermit_counter swapout_writethrough_completions
      ;;
    *)
      printf '0'
      ;;
  esac
}

read_activity_total() {
  local key val total=0
  for key in $WAIT_STABLE_KEYS; do
    val=$(read_activity_counter "$key")
    if [[ "$val" =~ ^[0-9]+$ ]]; then
      total=$((total + val))
    fi
  done
  printf '%s' "$total"
}

memcached_is_healthy() {
  local pid

  [ -s "$PID_FILE" ] || return 1
  pid=$(cat "$PID_FILE")
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  memcached_stats | grep -q '^STAT '
}

require_memcached_healthy() {
  if ! memcached_is_healthy; then
    rdma_die "memcached is not alive or not responding (pid file: $PID_FILE)"
  fi
}

wait_for_swap_stable() {
  local label=$1
  local wait_log=$2
  local start_ts now_ts elapsed start_total prev curr delta quiet samples status
  local oom_start oom_now failed

  WAIT_LAST_STATUS=disabled
  WAIT_LAST_SECONDS=0
  WAIT_LAST_SAMPLES=0
  WAIT_LAST_DELTA=0

  if [ "$WAIT_SWAP_STABLE" != "1" ]; then
    return 0
  fi

  mkdir -p "$(dirname "$wait_log")"
  if [ ! -s "$wait_log" ]; then
    printf 'label,start_ts,end_ts,wait_sec,status,samples,start_total,end_total,last_delta,keys\n' > "$wait_log"
  fi

  start_ts=$(date +%s)
  start_total=$(read_activity_total)
  prev=$start_total
  curr=$prev
  delta=0
  quiet=0
  samples=0
  status=timeout
  failed=0
  oom_start=$(cgroup_event_value oom_kill)
  now_ts=$start_ts
  elapsed=0

  while true; do
    oom_now=$(cgroup_event_value oom_kill)
    if [ "$oom_now" -gt "$oom_start" ]; then
      status=oom-killed
      failed=1
      break
    fi
    if ! memcached_is_healthy; then
      status=server-dead
      failed=1
      break
    fi

    sleep "$SWAP_STABLE_INTERVAL_SEC"
    samples=$((samples + 1))
    curr=$(read_activity_total)
    delta=$((curr - prev))
    if [ "$delta" -le "$SWAP_STABLE_MAX_DELTA" ]; then
      quiet=$((quiet + 1))
    else
      quiet=0
    fi
    prev=$curr

    now_ts=$(date +%s)
    elapsed=$((now_ts - start_ts))
    if [ "$quiet" -ge "$SWAP_STABLE_QUIET_SAMPLES" ]; then
      status=stable
      break
    fi
    if [ "$elapsed" -ge "$SWAP_STABLE_TIMEOUT_SEC" ]; then
      break
    fi
  done

  WAIT_LAST_STATUS=$status
  WAIT_LAST_SECONDS=$elapsed
  WAIT_LAST_SAMPLES=$samples
  WAIT_LAST_DELTA=$delta
  printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,"%s"\n' \
    "$label" "$start_ts" "$now_ts" "$elapsed" "$status" "$samples" \
    "$start_total" "$curr" "$delta" "$WAIT_STABLE_KEYS" >> "$wait_log"

  if [ "$failed" -ne 0 ]; then
    rdma_log "swap wait failed: status=$status label=$label"
    return 1
  fi
}

memcached_stats() {
  if ! command -v nc >/dev/null 2>&1; then
    return 0
  fi
  printf 'stats\r\n' | nc -w 2 "$SERVER_ADDR" "$PORT" 2>/dev/null || true
}

stat_from_file() {
  local file=$1
  local key=$2
  awk -v k="$key" '$1 == "STAT" && $2 == k { gsub(/\r/, "", $3); print $3; found=1 } END { if (!found) print 0 }' "$file"
}

kv_from_file() {
  local file=$1
  local key=$2
  awk -F= -v k="$key" '$1 == k { print $2; found=1 } END { if (!found) print 0 }' "$file"
}

delta_from_files() {
  local before=$1
  local after=$2
  local key=$3
  local b a
  b=$(kv_from_file "$before" "$key")
  a=$(kv_from_file "$after" "$key")
  if [[ "$a" =~ ^[0-9]+$ ]] && [[ "$b" =~ ^[0-9]+$ ]]; then
    printf '%s' "$((a - b))"
  else
    printf '0'
  fi
}

capture_counters() {
  local out=$1
  local stats_file=$2

  memcached_stats > "$stats_file"

  {
    printf 'timestamp=%s\n' "$(date +%s)"
    printf 'pswpin=%s\n' "$(read_vmstat_key pswpin)"
    printf 'pswpout=%s\n' "$(read_vmstat_key pswpout)"
    printf 'pgfault=%s\n' "$(read_vmstat_key pgfault)"
    printf 'pgmajfault=%s\n' "$(read_vmstat_key pgmajfault)"
    printf 'cgroup_memory_current=%s\n' "$(cgroup_value memory.current)"
    printf 'cgroup_memory_max=%s\n' "$(cgroup_value memory.max)"
    printf 'cgroup_swap_current=%s\n' "$(cgroup_value memory.swap.current)"
    printf 'cgroup_oom=%s\n' "$(cgroup_event_value oom)"
    printf 'cgroup_oom_kill=%s\n' "$(cgroup_event_value oom_kill)"
    printf 'backend_loads=%s\n' "$(read_debug_counter loads)"
    printf 'backend_stores=%s\n' "$(read_debug_counter stores)"
    printf 'backend_load_misses=%s\n' "$(read_debug_counter load_misses)"
    printf 'backend_post_errors=%s\n' "$(read_debug_counter post_errors)"
    printf 'backend_wc_errors=%s\n' "$(read_debug_counter wc_errors)"
    printf 'backend_errors=%s\n' "$(read_debug_counter errors)"
    printf 'hermit_swapout_backend_stores=%s\n' "$(read_hermit_counter swapout_backend_stores)"
    printf 'hermit_swapout_backend_store_errors=%s\n' "$(read_hermit_counter swapout_backend_store_errors)"
    printf 'hermit_swapout_backend_poll_errors=%s\n' "$(read_hermit_counter swapout_backend_poll_errors)"
    printf 'hermit_swapout_native_fallbacks=%s\n' "$(read_hermit_counter swapout_native_fallbacks)"
    printf 'hermit_swapout_exclusive_completions=%s\n' "$(read_hermit_counter swapout_exclusive_completions)"
    printf 'hermit_swapout_writethrough_completions=%s\n' "$(read_hermit_counter swapout_writethrough_completions)"
    printf 'hermit_swapout_large_folio_fallbacks=%s\n' "$(read_hermit_counter swapout_large_folio_fallbacks)"
    printf 'memcached_curr_items=%s\n' "$(stat_from_file "$stats_file" curr_items)"
    printf 'memcached_evictions=%s\n' "$(stat_from_file "$stats_file" evictions)"
    printf 'memcached_bytes=%s\n' "$(stat_from_file "$stats_file" bytes)"
    printf 'memcached_limit_maxbytes=%s\n' "$(stat_from_file "$stats_file" limit_maxbytes)"
  } > "$out"
}

save_config() {
  mkdir -p "$RESULT_DIR"
  {
    printf 'kernel_tag=%s\n' "$KERNEL_TAG"
    printf 'uname=%s\n' "$(uname -r)"
    printf 'mode=%s\n' "$MODE"
    printf 'port=%s\n' "$PORT"
    printf 'records=%s\n' "$RECORDS"
    printf 'duration=%s\n' "$DURATION"
    printf 'loads=%s\n' "$LOADS"
    printf 'bench_repeats=%s\n' "$BENCH_REPEATS"
    printf 'core_layout=%s\n' "$CORE_LAYOUT"
    printf 'bench_socket=%s\n' "$BENCH_SOCKET"
    printf 'bench_numa_node=%s\n' "${BENCH_NUMA_NODE:-}"
    printf 'bench_numactl=%s\n' "$BENCH_NUMACTL"
    printf 'bench_use_smt=%s\n' "$BENCH_USE_SMT"
    printf 'memcached_mem_mb=%s\n' "$MEMCACHED_MEM_MB"
    printf 'memcached_threads=%s\n' "${MEMCACHED_THREADS:-}"
    printf 'memcached_cores=%s\n' "${MEMCACHED_CORES:-}"
    printf 'mutilate_threads=%s\n' "${MUTILATE_THREADS:-}"
    printf 'mutilate_cores=%s\n' "${MUTILATE_CORES:-}"
    printf 'hermit_reserved_cores=%s\n' "${HERMIT_RESERVED_CORES:-}"
    printf 'sthd_cnt=%s\n' "$STHD_CNT"
    printf 'reclaim_mode=%s\n' "$RECLAIM_MODE"
    printf 'reclaim_headroom_pages=%s\n' "$RECLAIM_HEADROOM_PAGES"
    printf 'bypass_swapcache=%s\n' "$BYPASS_SWAPCACHE"
    printf 'lazy_poll=%s\n' "$LAZY_POLL"
    printf 'hermit_swapout_policy=%s\n' "$HERMIT_SWAPOUT_POLICY"
    printf 'rswap_required_backend=%s\n' "$RSWAP_REQUIRED_BACKEND"
    printf 'rswap_backend=%s\n' "$(detect_rswap_backend)"
    printf 'local_ratio_pct=%s\n' "${LOCAL_RATIO_PCT:-}"
    printf 'cgroup_limit_mb=%s\n' "${CGROUP_LIMIT_MB:-}"
    printf 'wait_swap_stable=%s\n' "$WAIT_SWAP_STABLE"
    printf 'swap_stable_interval_sec=%s\n' "$SWAP_STABLE_INTERVAL_SEC"
    printf 'swap_stable_quiet_samples=%s\n' "$SWAP_STABLE_QUIET_SAMPLES"
    printf 'swap_stable_timeout_sec=%s\n' "$SWAP_STABLE_TIMEOUT_SEC"
    printf 'swap_stable_max_delta=%s\n' "$SWAP_STABLE_MAX_DELTA"
    printf 'wait_stable_keys=%s\n' "$WAIT_STABLE_KEYS"
  } > "$RESULT_DIR/config.env"
}
