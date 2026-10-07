#!/usr/bin/env bash
# Continue untouched page configurations after a failed Memcached page sweep.
set -euo pipefail
ORIGINAL_ROOT=${ORIGINAL_ROOT:?}
CONTINUATION_ROOT=${CONTINUATION_ROOT:?}
[[ "$CONTINUATION_ROOT" = /* && ! -e "$CONTINUATION_ROOT" ]]
sudo -n test -r /sys/kernel/debug/hermit/order_stats
[ "$(uname -r)" = 6.18.38-hermit ]
[ "$(git -C "$HOME/hermit-6/linux-stable" rev-parse HEAD)" = 330b5850d22319962469dbe48461881054f15ae4 ]
! pgrep -x memcached >/dev/null
! pgrep -x mutilate >/dev/null
exec 9>"/tmp/hermit-prepebs-baseline-$(id -u).lock"
flock -n 9
mkdir -p "$CONTINUATION_ROOT/state" "$CONTINUATION_ROOT/results/memcached"
cp "$0" "$CONTINUATION_ROOT/driver.sh"
sudo -n dmesg > "$CONTINUATION_ROOT/state/dmesg-before.txt"
sudo -n sh -c 'for key in apt_reclaim batch_account batch_io batch_swapout batch_tlb bypass_swapcache lazy_poll reclaim_headroom_pages reclaim_mode remote_order_mask speculative_io speculative_lock sthd_cnt vaddr_swapout; do f=/sys/kernel/debug/hermit/$key; printf "%s\t%s\n" "$f" "$(cat "$f")"; done' > "$CONTINUATION_ROOT/state/controls.tsv"
for f in /sys/kernel/mm/transparent_hugepage/enabled /sys/kernel/mm/transparent_hugepage/hugepages-*kB/enabled; do
  printf '%s\t%s\n' "$f" "$(sed -n 's/.*\[\([^]]*\)\].*/\1/p' "$f")"
done > "$CONTINUATION_ROOT/state/thp.tsv"
cleanup() {
  local rc=$? path value
  trap - EXIT
  while IFS=$'\t' read -r path value; do
    printf '%s\n' "$value" | sudo -n tee "$path" >/dev/null || rc=1
  done < "$CONTINUATION_ROOT/state/controls.tsv"
  while IFS=$'\t' read -r path value; do
    printf '%s\n' "$value" | sudo -n tee "$path" >/dev/null || rc=1
  done < "$CONTINUATION_ROOT/state/thp.tsv"
  sudo -n dmesg > "$CONTINUATION_ROOT/state/dmesg-after.txt" || rc=1
  sudo -n cat /sys/kernel/debug/hermit/order_stats > "$CONTINUATION_ROOT/state/order-after.txt" || rc=1
  printf 'exit_code=%s\nfinished=%s\n' "$rc" "$(date -Is)" > "$CONTINUATION_ROOT/completion.txt"
  exit "$rc"
}
trap cleanup EXIT
export BASELINE_ROOT="$ORIGINAL_ROOT" HERMIT6_ROOT="$HOME/hermit-6"
export MODE=cgroup-hermit MODES=hermit-cgroup KERNEL_TAG=hermit-prepebs-330b5850d2
export RESULT_ROOT="$CONTINUATION_ROOT/results" BENCH_REPEATS=3 LOCAL_RATIO_PCT=70
export BYPASS_SWAPCACHE=Y LAZY_POLL=N RSWAP_REQUIRED_BACKEND=rdma
export RECLAIM_MODE=0 RECLAIM_HEADROOM_PAGES=2048 STHD_CNT=16
export RESTORE_THP=1 BENCH_SOCKET=0 BENCH_NUMA_NODE=0 BENCH_NUMACTL=1
export MEMCACHED_BIN="$HOME/memcached/memcached" MUTILATE_BIN="$HOME/mutilate/mutilate"
export CGROUP_NAME=hermit-baseline-mc RECORDS=32000000 MEMCACHED_MEM_MB=16384
export MEMCACHED_CORES=0-7 MUTILATE_CORES=8-15 MEMCACHED_THREADS=8 MUTILATE_THREADS=8
export LOADS='100000 250000 500000' DURATION=40 PORT=11219 BASE_RUN_ID=memcached
printf 'page_kb\tstart\tend\texit_code\n' > "$CONTINUATION_ROOT/stages.tsv"
failed=0
for page in 16 32 64 128 256 512 1024 2048; do
  ! pgrep -x memcached >/dev/null
  ! pgrep -x mutilate >/dev/null
  start=$(date -Is)
  printf '%s START memcached-%sk\n' "$start" "$page" | tee -a "$CONTINUATION_ROOT/progress.log"
  rc=0
  env PAGE_SIZES_KB="$page" SWEEP_DIR="$CONTINUATION_ROOT/results/memcached/${page}k-sweep" \
    timeout --signal=TERM --kill-after=120s 40m bash "$ORIGINAL_ROOT/source/run_memcached_page_sweep.sh" \
    > "$CONTINUATION_ROOT/memcached-${page}k.log" 2>&1 || rc=$?
  printf '%s\t%s\t%s\t%s\n' "$page" "$start" "$(date -Is)" "$rc" >> "$CONTINUATION_ROOT/stages.tsv"
  printf '%s END memcached-%sk rc=%s\n' "$(date -Is)" "$page" "$rc" | tee -a "$CONTINUATION_ROOT/progress.log"
  sudo -n dmesg > "$CONTINUATION_ROOT/state/dmesg-after-${page}k.txt"
  [ "$rc" = 0 ] || failed=1
done
exit "$failed"
