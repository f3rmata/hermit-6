#!/usr/bin/env bash
# Read-only verification after both baseline drivers have exited.
set -euo pipefail
ROOT=${BASELINE_ROOT:?}
rc=0
while IFS=$'\t' read -r path expected; do
  actual=$(sudo -n cat "$path")
  printf 'control\t%s\texpected=%s\tactual=%s\n' "$path" "$expected" "$actual"
  [ "$actual" = "$expected" ] || rc=1
done < "$ROOT/state/controls.tsv"
while IFS=$'\t' read -r path expected; do
  actual=$(sed -n 's/.*\[\([^]]*\)\].*/\1/p' "$path")
  printf 'thp\t%s\texpected=%s\tactual=%s\n' "$path" "$expected" "$actual"
  [ "$actual" = "$expected" ] || rc=1
done < "$ROOT/state/thp.tsv"
uname -a
git -C "$HOME/hermit-6/linux-stable" rev-parse HEAD
sha256sum "/boot/vmlinuz-$(uname -r)" "$HOME/hermit-6/remoteswap/client/rswap-client.ko"
printf 'loaded_module_srcversion='; cat /sys/module/rswap_client/srcversion
cat /proc/swaps
for dir in /sys/kernel/debug/rswap_rdma /sys/kernel/debug/rswap_dram; do
  for key in loads stores load_misses post_errors wc_errors errors; do
    if sudo -n test -r "$dir/$key"; then
      printf 'backend_counter\t%s\t' "$dir/$key"
      sudo -n cat "$dir/$key"
    else
      printf 'backend_counter_unavailable\t%s\n' "$dir/$key"
    fi
  done
done
for process in memcached mutilate redis-server; do
  if pgrep -x "$process"; then printf 'remaining_process=%s\n' "$process"; rc=1; fi
done
for cg in hermit-anon-swapout hermit-redis hermit-xgboost hermit-ycsb hermit-baseline-mc; do
  if [ -e "/sys/fs/cgroup/$cg/cgroup.procs" ]; then
    procs=$(cat "/sys/fs/cgroup/$cg/cgroup.procs")
    printf 'cgroup=%s processes=%s\n' "$cg" "$procs"
    [ -z "$procs" ] || rc=1
  fi
done
"$HOME/redis/src/redis-server" --version
"$HOME/memcached/memcached" -V
python3 -c 'import importlib.metadata as m; print("xgboost=" + m.version("xgboost"))'
sha256sum "$HOME/redis/src/redis-server" "$HOME/memcached/memcached" "$HOME/mutilate/mutilate" "$ROOT/hermit_swap_stats"
(cd "$ROOT/source" && sha256sum -c "$ROOT/source-sha256.txt")
printf 'restore_verification_exit_code=%s\n' "$rc"
exit "$rc"
