#!/usr/bin/env bash
# Shared mechanics for page-size sweeps. Workload lifecycle stays in each driver.
# Outputs are consumed by the sourcing drivers.
# shellcheck disable=SC2034
# shellcheck source=common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

THP_ROOT=/sys/kernel/mm/transparent_hugepage
REMOTE_MASK_FILE=/sys/kernel/debug/hermit/remote_order_mask
EFFECTIVE_MASK_FILE=/sys/kernel/debug/hermit/effective_order_mask
QUIET_INTERVAL_SEC=${QUIET_INTERVAL_SEC:-1}
QUIET_SAMPLES=${QUIET_SAMPLES:-3}
QUIET_TIMEOUT_SEC=${QUIET_TIMEOUT_SEC:-120}
RESTORE_THP=${RESTORE_THP:-1}
remote_mask_before=
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

snapshot_order_stats() {
  sudo_cat /sys/kernel/debug/hermit/order_stats
}

order_field() {
  local file=$1 order=$2 column=$3
  awk -v o="$order" -v c="$column" 'NR > 1 && $1 == o { print $c; found=1 } END { if (!found) print 0 }' "$file"
}

order_total_stores() {
  snapshot_order_stats | awk 'NR > 1 { total += $3 } END { printf "%d", total }'
}

memory_event() {
  local key=$1
  awk -v key="$key" '$1 == key { print $2; found=1 } END { if (!found) print 0 }' \
    "$CGROUP/memory.events"
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
  remote_mask_before=$(sudo_cat "$REMOTE_MASK_FILE")
}

configure_page_size() {
  local kb=$1 order=$2 file selected
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
  sudo_write "$(printf '0x%x' $((1 | (1 << order))))" "$REMOTE_MASK_FILE"
}

log_field() {
  local record=$1 key=$2 log=$3
  awk -v record="$record" -v key="$key" '
    $1 == record {
      for (i = 2; i <= NF; i++) {
        split($i, pair, "=")
        if (pair[1] == key)
          value = pair[2]
      }
    }
    END { print value }
  ' "$log"
}

find_redis_server_bin() {
  local bin
  for bin in "${REDIS_SERVER_BIN:-}" "$PWD/redis/src/redis-server" "$HOME/redis/src/redis-server"; do
    if [ -n "$bin" ] && [ -x "$bin" ]; then
      printf '%s' "$bin"
      return 0
    fi
  done
  if command -v redis-server >/dev/null 2>&1; then
    command -v redis-server
    return 0
  fi
  rdma_die "cannot find redis-server; set REDIS_SERVER_BIN=/path/to/redis-server"
}

# Optional initial completion timestamp; updated only when stores change.
wait_for_stores_quiet() {
  local previous current quiet=0 start
  last_change_ns=${1:-0}
  start=$(date +%s)
  previous=$(order_total_stores)
  while [ "$quiet" -lt "$QUIET_SAMPLES" ]; do
    sleep "$QUIET_INTERVAL_SEC"
    current=$(order_total_stores)
    if [ "$current" -eq "$previous" ]; then
      quiet=$((quiet + 1))
    else
      previous=$current
      last_change_ns=$(date +%s%N)
      quiet=0
    fi
    if [ "$quiet" -lt "$QUIET_SAMPLES" ]; then
      [ "$(( $(date +%s) - start ))" -lt "$QUIET_TIMEOUT_SEC" ] || \
        rdma_die "Hermit store counters did not become quiet; stop other swap workloads"
    fi
  done
}

validate_sweep_host() {
  local swap_count
  [ -r /sys/fs/cgroup/cgroup.controllers ] || rdma_die "cgroup v2 is required"
  mount_debugfs_if_needed
  sudo_test -r /sys/kernel/debug/hermit/order_stats || rdma_die "Hermit order_stats is unavailable"
  sudo_test -w "$REMOTE_MASK_FILE" || rdma_die "Hermit remote_order_mask is not writable"
  [ "$(detect_rswap_backend)" = rdma ] || rdma_die "Hermit RDMA backend is not active"
  swap_count=$(awk 'NR > 1 { count++ } END { print count + 0 }' /proc/swaps)
  [ "$swap_count" -eq 1 ] || \
    rdma_die "exactly one active swap device is required; found $swap_count"
  awk 'NR == 2 && $2 == "partition" { ok=1 } END { exit !ok }' /proc/swaps || \
    rdma_die "the active swap area must be a block device, not a swapfile"
  if [ -r /sys/module/zswap/parameters/enabled ] && grep -qi '^Y' /sys/module/zswap/parameters/enabled; then
    rdma_die "zswap is enabled; disable it before measuring RDMA swap-out"
  fi
}

prepare_sweep_cgroup() {
  local existing
  if sudo_test -d "$CGROUP"; then
    existing=$(sudo_cat "$CGROUP/cgroup.procs")
    [ -z "$existing" ] || rdma_die "$CGROUP is busy with PIDs: $existing"
  else
    sudo mkdir "$CGROUP"
  fi
  sudo_write max "$CGROUP/memory.max"
  sudo_write max "$CGROUP/memory.swap.max"
  configure_benchmark_pebs
}

restore_thp_state() {
  local file
  if [ "$RESTORE_THP" = 1 ]; then
    for file in "${thp_files[@]}"; do
      sudo_write "${thp_policy[$file]}" "$file" || true
    done
    [ -z "$remote_mask_before" ] || sudo_write "$remote_mask_before" "$REMOTE_MASK_FILE" || true
  fi
}

check_swap_capacity() {
  local required_swap_mb=$1 free_swap_mb
  free_swap_mb=$(awk 'NR > 1 { free += $3 - $4 } END { printf "%d", free / 1024 }' /proc/swaps)
  [ "$free_swap_mb" -ge "$required_swap_mb" ] || \
    rdma_die "only ${free_swap_mb} MiB swap is free; at least ${required_swap_mb} MiB is required"
}

snapshot_sweep_stats() {
  snapshot_order_stats > "$1"
  snapshot_benchmark_pebs "$1.pebs"
}

# order_stats measures folio requests, not actual WRs. Keep the historical
# fallback-adjusted large-byte estimate and CSV units identical across sweeps.
order_metrics() {
  local before=$1 after=$2 column=$3 duration=$4 units=${5:-1}
  awk -v before="$before" -v column="$column" -v duration="$duration" -v units="$units" '
    FILENAME == before && FNR > 1 { b[$1]=$column; fb[$1]=$5; next }
    FILENAME != before && FNR > 1 {
      d=$column-b[$1]; f=$5-fb[$1];
      if (f < 0) f=0; if (f > d) f=d;
      requests += d; total += d*$2; if ($1 > 0) large += (d-f)*$2
    }
    END {
      pct=total ? 100*large/total : 0;
      printf "%.0f,%.0f,%.3f,%.6f,%.0f\n", total, large, pct,
             (duration > 0 ? total / 1073741824 / (duration / units) : 0), requests
    }' "$before" "$after"
}

summarize_stores() {
  local metrics
  metrics=$(order_metrics "$1" "$2" 3 "$3" 1000) || return
  IFS=, read -r total_store_bytes large_store_bytes large_store_pct protocol_gib_per_sec _ <<< "$metrics"
}

summarize_loads() {
  local metrics
  metrics=$(order_metrics "$1" "$2" 4 "$3") || return
  IFS=, read -r total_load_bytes large_load_bytes large_load_pct load_protocol_gib_per_sec backend_loads_delta <<< "$metrics"
}
