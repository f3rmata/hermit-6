#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'USAGE'
usage:
  MODES="local local-cgroup hermit-cgroup" ./run_memcached_page_sweep.sh

Run local, local-cgroup and Hermit-cgroup memcached benchmarks for each page
size from 4 KiB through 2 MiB. Every page-size/mode pair gets its own
memcached instance, load phase, and result directory.

Important environment:
  MODES="local local-cgroup hermit-cgroup" (default; aliases are accepted)
  PAGE_SIZES_KB="4 16 32 64 128 256 512 1024 2048"
  BASE_RUN_ID=20260727-page-sweep
  LOADS="100000 250000 500000"
  BENCH_REPEATS=3
  LOCAL_RATIO_PCT=85
  RESTORE_THP=1                      (restore THP and Hermit mask on exit)

The script always stops the memcached instance that it started, including on
an error or Ctrl-C. A previously orphaned instance must be stopped separately.
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

MODES=${MODES:-"local local-cgroup hermit-cgroup"}

PAGE_SIZES_KB=${PAGE_SIZES_KB:-"4 16 32 64 128 256 512 1024 2048"}
BASE_RUN_ID=${BASE_RUN_ID:-"$(date +%Y%m%d-%H%M%S)-${KERNEL_TAG}-page-sweep"}
SWEEP_DIR=${SWEEP_DIR:-"$RESULT_ROOT/$BASE_RUN_ID"}
SWEEP_CSV="$SWEEP_DIR/page-sweep.csv"
COMBINED_CSV="$SWEEP_DIR/page-sweep-summary.csv"
THP_ROOT=/sys/kernel/mm/transparent_hugepage
REMOTE_MASK_FILE=/sys/kernel/debug/hermit/remote_order_mask
EFFECTIVE_MASK_FILE=/sys/kernel/debug/hermit/effective_order_mask
RESTORE_THP=${RESTORE_THP:-1}
current_result_dir=
current_label=
current_mode=
remote_mask_before=
has_hermit_mode=0
declare -a thp_files=()
declare -A thp_policy=()

page_order() {
  case "$1" in
    4) printf 0 ;;
    16) printf 2 ;;
    32) printf 3 ;;
    64) printf 4 ;;
    128) printf 5 ;;
    256) printf 6 ;;
    512) printf 7 ;;
    1024) printf 8 ;;
    2048) printf 9 ;;
    *) return 1 ;;
  esac
}

selected_policy() {
  awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^\[.*\]$/) { gsub(/\[|\]/, "", $i); print $i; exit } }' "$1"
}

save_thp_state() {
  local file policy

  [ -r "$THP_ROOT/enabled" ] || rdma_die "THP sysfs is unavailable: $THP_ROOT/enabled"
  for file in "$THP_ROOT/enabled" "$THP_ROOT"/hugepages-*kB/enabled; do
    [ -e "$file" ] || continue
    policy=$(selected_policy "$file")
    [ -n "$policy" ] || rdma_die "cannot read selected THP policy from $file"
    thp_files+=("$file")
    thp_policy["$file"]=$policy
  done
  if [ "$has_hermit_mode" = "1" ]; then
    sudo_test -r "$REMOTE_MASK_FILE" || rdma_die "Hermit remote_order_mask is unavailable"
    sudo_test -r "$EFFECTIVE_MASK_FILE" || rdma_die "Hermit effective_order_mask is unavailable"
    remote_mask_before=$(sudo_cat "$REMOTE_MASK_FILE")
  fi
}

restore_state() {
  local ret=$? file

  if [ -n "$current_result_dir" ]; then
    rdma_log "cleanup page=$current_label result=$current_result_dir"
    env MODE="$current_mode" KERNEL_TAG="$KERNEL_TAG" RESULT_DIR="$current_result_dir" \
      PID_FILE="$current_result_dir/memcached.pid" \
      "$SCRIPT_DIR/memcached.sh" stop || true
  fi
  if [ "$RESTORE_THP" = "1" ]; then
    for file in "${thp_files[@]}"; do
      sudo_write "${thp_policy[$file]}" "$file" || true
    done
    [ -n "$remote_mask_before" ] && sudo_write "$remote_mask_before" "$REMOTE_MASK_FILE" || true
  fi
  exit "$ret"
}

disable_all_thp() {
  local file

  for file in "${thp_files[@]}"; do
    sudo_write never "$file"
  done
}

record_run() {
  local label=$1 kb=$2 order=$3 mode=$4 mask=$5 effective=$6 status=$7 result_dir=$8

  printf '%s,%s,%s,%s,%s,%s,%s,%s\n' \
    "$label" "$kb" "$order" "$mode" "$mask" "$effective" "$status" "$result_dir" \
    >> "$SWEEP_CSV"
}

append_summary() {
  local label=$1 kb=$2 order=$3 mode=$4 mask=$5 effective=$6 result_dir=$7 summary

  summary="$result_dir/summary.csv"
  [ -s "$summary" ] || return 0
  if [ ! -s "$COMBINED_CSV" ]; then
    printf 'page_label,page_kb,folio_order,mode,requested_mask,effective_mask,' > "$COMBINED_CSV"
    head -n 1 "$summary" >> "$COMBINED_CSV"
  fi
  tail -n +2 "$summary" | while IFS= read -r row; do
    [ -n "$row" ] || continue
    printf '%s,%s,%s,%s,%s,%s,%s\n' "$label" "$kb" "$order" "$mode" "$mask" "$effective" "$row" \
      >> "$COMBINED_CSV"
  done
}

configure_page_size() {
  local kb=$1 selected=$2

  disable_all_thp
  if [ "$kb" = "4" ]; then
    return 0
  fi
  [ -e "$selected" ] || return 1
  sudo_write always "$selected"
  if [ "$kb" = "2048" ]; then
    sudo_write always "$THP_ROOT/enabled"
  fi
}

capture_page_state() {
  local output=$1 selected=$2 stats_file

  {
    grep -E '^(AnonHugePages|ShmemHugePages|SwapTotal|SwapFree):' /proc/meminfo || true
    [ -e "$THP_ROOT/enabled" ] && cat "$THP_ROOT/enabled"
    [ -e "$selected" ] && cat "$selected"
    if [ -d "${selected%/enabled}/stats" ]; then
      for stats_file in "${selected%/enabled}/stats"/*; do
        [ -f "$stats_file" ] || continue
        printf '%s=' "$(basename "$stats_file")"
        cat "$stats_file"
      done
    fi
    if sudo_test -r /sys/kernel/debug/hermit/order_stats; then
      sudo_cat /sys/kernel/debug/hermit/order_stats
    fi
  } > "$output"
}

mkdir -p "$SWEEP_DIR"
printf 'page_label,page_kb,folio_order,mode,requested_mask,effective_mask,status,result_dir\n' > "$SWEEP_CSV"
for requested_mode in $MODES; do
  mode=$(normalize_mode "$requested_mode")
  case "$mode" in
    local|cgroup-linux) ;;
    cgroup-hermit|hermit) has_hermit_mode=1 ;;
    *) rdma_die "unsupported mode: $requested_mode" ;;
  esac
done
save_thp_state
trap restore_state EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for kb in $PAGE_SIZES_KB; do
  order=$(page_order "$kb") || {
    rdma_log "skip unsupported requested page size: ${kb} KiB"
    for requested_mode in $MODES; do
      record_run "${kb}k" "$kb" "" "$(normalize_mode "$requested_mode")" "" "" \
        "skipped-invalid-size" ""
    done
    continue
  }
  label="${kb}k"
  selected="$THP_ROOT/hugepages-${kb}kB/enabled"
  if [ "$kb" = "2048" ] && [ ! -e "$selected" ]; then
    selected="$THP_ROOT/enabled"
  fi
  mask=$(printf '0x%x' $((1 | (1 << order))))
  current_label=$label

  if ! configure_page_size "$kb" "$selected"; then
    rdma_log "skip $label: $selected is unavailable"
    for requested_mode in $MODES; do
      mode=$(normalize_mode "$requested_mode")
      result_dir="$SWEEP_DIR/$label/$mode"
      mkdir -p "$result_dir"
      record_run "$label" "$kb" "$order" "$mode" "$mask" "" \
        "skipped-thp-unavailable" "$result_dir"
    done
    continue
  fi

  for requested_mode in $MODES; do
    mode=$(normalize_mode "$requested_mode")
    current_mode=$mode
    current_result_dir="$SWEEP_DIR/$label/$mode"
    mkdir -p "$current_result_dir"
    requested_mask=not-applicable
    effective=not-applicable

    if [ "$mode" = "cgroup-hermit" ] || [ "$mode" = "hermit" ]; then
      requested_mask=$mask
      sudo_write "$mask" "$REMOTE_MASK_FILE"
      effective=$(sudo_cat "$EFFECTIVE_MASK_FILE")
      if ! (( (effective & (1 << order)) != 0 )); then
        rdma_log "skip page=$label mode=$mode: effective_order_mask=$effective lacks order $order"
        record_run "$label" "$kb" "$order" "$mode" "$requested_mask" "$effective" \
          "skipped-backend-capability" "$current_result_dir"
        current_result_dir=
        continue
      fi
    fi

    {
      printf 'label=%s\npage_kb=%s\nfolio_order=%s\nmode=%s\nrequested_mask=%s\neffective_mask=%s\n' \
        "$label" "$kb" "$order" "$mode" "$requested_mask" "$effective"
      cat "$THP_ROOT/enabled"
      [ -e "$selected" ] && cat "$selected"
      if [ "$mode" = "cgroup-hermit" ] || [ "$mode" = "hermit" ]; then
        sudo_cat "$REMOTE_MASK_FILE"
        sudo_cat "$EFFECTIVE_MASK_FILE"
      fi
    } > "$current_result_dir/page-config.txt"
    capture_page_state "$current_result_dir/page-state-before.txt" "$selected"

    rdma_log "=== page=$label mode=$mode order=$order result=$current_result_dir ==="
    env MODE="$mode" KERNEL_TAG="$KERNEL_TAG" RESULT_DIR="$current_result_dir" \
      PID_FILE="$current_result_dir/memcached.pid" \
      "$SCRIPT_DIR/memcached_load.sh" --mode "$mode" --kernel "$KERNEL_TAG" \
        --result-dir "$current_result_dir" --port "$PORT"

    env MODE="$mode" KERNEL_TAG="$KERNEL_TAG" RESULT_DIR="$current_result_dir" \
      PID_FILE="$current_result_dir/memcached.pid" \
      "$SCRIPT_DIR/memcached_bench.sh" --mode "$mode" --kernel "$KERNEL_TAG" \
        --result-dir "$current_result_dir" --port "$PORT"

    capture_page_state "$current_result_dir/page-state-after.txt" "$selected"
    env MODE="$mode" KERNEL_TAG="$KERNEL_TAG" RESULT_DIR="$current_result_dir" \
      PID_FILE="$current_result_dir/memcached.pid" \
      "$SCRIPT_DIR/memcached.sh" stop || true
    append_summary "$label" "$kb" "$order" "$mode" "$requested_mask" "$effective" "$current_result_dir"
    record_run "$label" "$kb" "$order" "$mode" "$requested_mask" "$effective" "completed" "$current_result_dir"
    current_result_dir=
  done
  sleep "${BETWEEN_PAGE_SLEEP_SEC:-10}"
done

trap - EXIT INT TERM
if [ "$RESTORE_THP" = "1" ]; then
  for file in "${thp_files[@]}"; do
    sudo_write "${thp_policy[$file]}" "$file"
  done
  [ -n "$remote_mask_before" ] && sudo_write "$remote_mask_before" "$REMOTE_MASK_FILE"
fi
rdma_log "page sweep index: $SWEEP_CSV"
rdma_log "combined median summary: $COMBINED_CSV"
