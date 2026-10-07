#!/usr/bin/env bash
# Run as the benchmark user on dnet-61, after a scoped sudo authentication.
# No kernel/module install, swap formatting, reboot, or remote server changes.
set -euo pipefail
SOURCE_ROOT=${SOURCE_ROOT:-$HOME/hermit-6}
BASELINE_ROOT=${BASELINE_ROOT:?set a new, absolute result directory}
PROFILE=${PROFILE:-full}
EXPECTED_KERNEL_COMMIT=330b5850d223
case "$PROFILE" in smoke|full) ;; *) exit 2 ;; esac
[[ "$BASELINE_ROOT" = /* && ! -e "$BASELINE_ROOT" ]] || { echo 'Result directory must be new and absolute' >&2; exit 2; }
[ "$(id -u)" -ne 0 ] || { echo 'Run as the benchmark user, not root' >&2; exit 2; }
sudo -n test -r /sys/kernel/debug/hermit/order_stats
[ "$(uname -r)" = 6.18.38-hermit ]
[[ "$(git -C "$SOURCE_ROOT/linux-stable" rev-parse HEAD)" = "$EXPECTED_KERNEL_COMMIT"* ]]
[ -z "$(git -C "$SOURCE_ROOT/linux-stable" status --porcelain --untracked-files=no)" ]
[ ! -e "$SOURCE_ROOT/linux-stable/mm/hermit_pebs.c" ]
! sudo -n test -e /sys/kernel/debug/hermit/pebs_enabled
cmp "/boot/vmlinuz-$(uname -r)" "$SOURCE_ROOT/linux-stable/arch/x86/boot/bzImage"
[ "$(cat /sys/module/rswap_client/srcversion)" = "$(modinfo -F srcversion "$SOURCE_ROOT/remoteswap/client/rswap-client.ko")" ]
[ "$(cat /sys/module/rswap_client/parameters/max_order)" = 9 ]
awk 'NR>1 {n++; used+=$4; if ($2 != "partition") bad=1} END {exit !(n==1 && used==0 && !bad)}' /proc/swaps
for process in memcached redis-server mutilate; do
  if pgrep -x "$process" >/dev/null; then echo "Existing $process; refusing to interfere" >&2; exit 2; fi
done
for cg in hermit-anon-swapout hermit-redis hermit-xgboost hermit-ycsb hermit-baseline-mc; do
  if [ -e "/sys/fs/cgroup/$cg/cgroup.procs" ]; then
    [ -z "$(cat "/sys/fs/cgroup/$cg/cgroup.procs")" ] || { echo "Busy cgroup $cg" >&2; exit 2; }
  fi
done
exec 9>"/tmp/hermit-prepebs-baseline-$(id -u).lock"
flock -n 9 || { echo 'Baseline already running' >&2; exit 2; }
mkdir -p "$BASELINE_ROOT/source" "$BASELINE_ROOT/results" "$BASELINE_ROOT/state"
export BASELINE_ROOT
cp "$SOURCE_ROOT"/tools/rdma/*.{sh,py,c} "$BASELINE_ROOT/source/"
# Use the shared ABI-correct QEMU helper; do not change the old kernel tree.
DRIVER_DIR=$(cd "$(dirname "$0")" && pwd)
cc -O2 -Wall -Wextra -Werror "$DRIVER_DIR/../../qemu-dram/src/hermit_swap_stats.c" -o "$BASELINE_ROOT/hermit_swap_stats"
cat >> "$BASELINE_ROOT/source/common.sh" <<'HELPER'

# Baseline-local helper, matching the kernel's three long* syscall ABI.
hermit_stats_helper() { printf '%s\n' "$BASELINE_ROOT/hermit_swap_stats"; }
HELPER
snapshot() {
  local tag=$1 f
  {
    date -Is; uname -a; cat /proc/version; cat /proc/cmdline
    git -C "$SOURCE_ROOT/linux-stable" rev-parse HEAD
    git -C "$SOURCE_ROOT" status --short
    sha256sum "/boot/vmlinuz-$(uname -r)" "$SOURCE_ROOT/linux-stable/arch/x86/boot/bzImage" "$SOURCE_ROOT/remoteswap/client/rswap-client.ko"
    cat /sys/module/rswap_client/srcversion; cat /proc/swaps
    lscpu; free -h
    for f in /sys/module/rswap_client/parameters/* /sys/class/infiniband/mlx5_0/ports/1/state /sys/class/infiniband/mlx5_0/device/numa_node; do
      printf '%s=' "$f"; cat "$f"
    done
    for f in bypass_swapcache speculative_io lazy_poll apt_reclaim sthd_cnt reclaim_mode reclaim_headroom_pages remote_order_mask effective_order_mask order_stats; do
      printf '%s=' "$f"; sudo -n cat "/sys/kernel/debug/hermit/$f"
    done
  } > "$BASELINE_ROOT/state/$tag.txt"
  sudo -n dmesg > "$BASELINE_ROOT/state/dmesg-$tag.txt"
}
snapshot before
sudo -n sh -c 'for key in apt_reclaim batch_account batch_io batch_swapout batch_tlb bypass_swapcache lazy_poll reclaim_headroom_pages reclaim_mode remote_order_mask speculative_io speculative_lock sthd_cnt vaddr_swapout; do f=/sys/kernel/debug/hermit/$key; printf "%s\t%s\n" "$f" "$(cat "$f")"; done' > "$BASELINE_ROOT/state/controls.tsv"
for f in /sys/kernel/mm/transparent_hugepage/enabled /sys/kernel/mm/transparent_hugepage/hugepages-*kB/enabled; do
  printf '%s\t%s\n' "$f" "$(sed -n 's/.*\[\([^]]*\)\].*/\1/p' "$f")"
done > "$BASELINE_ROOT/state/thp.tsv"
cleanup() {
  local rc=$? path value
  trap - EXIT
  while IFS=$'\t' read -r path value; do
    printf '%s\n' "$value" | sudo -n tee "$path" >/dev/null || rc=1
  done < "$BASELINE_ROOT/state/controls.tsv"
  while IFS=$'\t' read -r path value; do
    printf '%s\n' "$value" | sudo -n tee "$path" >/dev/null || rc=1
  done < "$BASELINE_ROOT/state/thp.tsv"
  snapshot after || rc=1
  printf 'exit_code=%s\nfinished=%s\n' "$rc" "$(date -Is)" > "$BASELINE_ROOT/completion.txt"
  exit "$rc"
}
trap cleanup EXIT
export MODE=cgroup-hermit KERNEL_TAG=hermit-prepebs-330b5850d2
export HERMIT6_ROOT="$SOURCE_ROOT" RESULT_ROOT="$BASELINE_ROOT/results"
export PAGE_SIZES_KB='4 16 32 64 128 256 512 1024 2048' BENCH_REPEATS=3
export LOCAL_RATIO_PCT=70 WORKSET_MB=16384 REDIS_WORKSET_MB=16384
export BYPASS_SWAPCACHE=Y LAZY_POLL=N RSWAP_REQUIRED_BACKEND=rdma
# Preserve the previously measured default reclaim policy explicitly.
export RECLAIM_MODE=0 RECLAIM_HEADROOM_PAGES=2048 STHD_CNT=16
export RESTORE_THP=1 BENCH_SOCKET=0 BENCH_NUMA_NODE=0 BENCH_NUMACTL=1
export MEMCACHED_BIN="$HOME/memcached/memcached" MUTILATE_BIN="$HOME/mutilate/mutilate"
export REDIS_SERVER_BIN="$HOME/redis/src/redis-server" YCSB_BIN="$HOME/ycsb-0.17.0/bin/ycsb"
export XGB_DATA_FILE="${XGB_DATA_FILE:-$SOURCE_ROOT/tools/rdma/data/HIGGS.csv/HIGGS.csv}" XGB_DATA_FORMAT=csv
export XGB_NTHREAD=4 XGB_ROUNDS=30 XGB_EXPECTED_METRIC_MIN=0.80 XGB_EXPECTED_METRIC_MAX=0.85
export CGROUP_NAME=hermit-baseline-mc RECORDS=32000000 MEMCACHED_MEM_MB=16384
export MEMCACHED_CORES=0-7 MUTILATE_CORES=8-15 MEMCACHED_THREADS=8 MUTILATE_THREADS=8
export LOADS='100000 250000 500000' DURATION=40 PORT=11219
export REDIS_PORT=6399 YCSB_RECORDCOUNT=8192 YCSB_OPERATIONCOUNT=8192
if [ "$PROFILE" = smoke ]; then
  export PAGE_SIZES_KB='4 64 2048' BENCH_REPEATS=1 WORKSET_MB=1024
fi
{
  printf 'profile=%s\n' "$PROFILE"
  export -p | grep -E ' (PAGE_SIZES_KB|BENCH_REPEATS|LOCAL_RATIO_PCT|WORKSET_MB|REDIS_WORKSET_MB|RECLAIM_MODE|RECLAIM_HEADROOM_PAGES|STHD_CNT|LOADS|DURATION|XGB_NTHREAD|XGB_ROUNDS|XGB_DATA_FILE|RECORDS|REDIS_PORT|PORT)='
} > "$BASELINE_ROOT/plan.txt"
(cd "$BASELINE_ROOT/source" && sha256sum * > "$BASELINE_ROOT/source-sha256.txt")
printf 'suite\tstart\tend\texit_code\n' > "$BASELINE_ROOT/stages.tsv"
run_suite() {
  local name=$1 script=$2 start rc=0
  shift 2
  start=$(date -Is)
  printf '%s START %s\n' "$start" "$name" | tee -a "$BASELINE_ROOT/progress.log"
  env RESULT_DIR="$RESULT_ROOT/$name" RUN_ID="$name" "$@" \
    timeout --signal=TERM --kill-after=120s 3h bash "$BASELINE_ROOT/source/$script" \
    > "$BASELINE_ROOT/$name.log" 2>&1 || rc=$?
  printf '%s\t%s\t%s\t%s\n' "$name" "$start" "$(date -Is)" "$rc" >> "$BASELINE_ROOT/stages.tsv"
  printf '%s END %s rc=%s\n' "$(date -Is)" "$name" "$rc" | tee -a "$BASELINE_ROOT/progress.log"
  [ "$rc" = 0 ] || return "$rc"
}
run_suite anon-1t run_anon_swapout_sweep.sh BENCH_THREADS=1 BENCH_CPUS=0 \
  SWAPOUT_TRIGGER=parallel-fault ACCESS_RATIOS='100 chunk64k' ACCESS_ORDERS=sequential ACCESS_LOCALITIES=high
[ "$PROFILE" != smoke ] || exit 0
run_suite anon-8t run_anon_swapout_sweep.sh BENCH_THREADS=8 BENCH_CPUS=0-7 \
  SWAPOUT_TRIGGER=parallel-fault ACCESS_RATIOS='100 chunk64k' ACCESS_ORDERS=sequential ACCESS_LOCALITIES=high
run_suite redis-chunk64k run_redis_page_sweep.sh REDIS_VALUE_SIZE=2097152 REDIS_SCAN_CHUNK=65536 \
  REDIS_ACTIVE_RATIOS=100 REDIS_ACCESS_ORDER=sequential REDIS_CHECKSUM=Y REDIS_CLIENTS=1 REDIS_INSTANCES=1 \
  BENCH_CPUS=8-15 REDIS_SERVER_CPUS=0-7
run_suite ycsb-redis-full run_ycsb_page_sweep.sh BINDING=redis YCSB_FIELDLENGTH=2097152 \
  YCSB_FIELDCOUNT=1 YCSB_READALLFIELDS=true YCSB_REQUESTDISTRIBUTION=uniform BENCH_CPUS=8-15 SERVER_CPUS=0-7
run_suite xgboost-higgs run_xgboost_page_sweep.sh BENCH_CPU=0-3
run_suite memcached run_memcached_page_sweep.sh MODES=hermit-cgroup \
  BASE_RUN_ID=memcached SWEEP_DIR="$RESULT_ROOT/memcached"
