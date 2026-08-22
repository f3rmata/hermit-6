#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
  cat <<'USAGE'
usage:
  RSWAP_SERVER_IP=172.16.0.58 ./run_memcached_native_hermit_sweep.sh

Run a strict two-phase page-size comparison:
  1. native local and local-cgroup with rswap_client unloaded;
  2. hermit-cgroup after manage_rswap_client.sh install.

The native phase covers every requested page size before the client is loaded.
This avoids unsafe and expensive swapoff/rmmod/insmod transitions for every
page size. The two phase directories retain identical page-size labels and can
be compared through their page-sweep-summary.csv files.

Required environment:
  RSWAP_SERVER_IP       RDMA memory server address, for example 172.16.0.58

Optional environment:
  RSWAP_SERVER_PORT=9400
  RSWAP_SWAP_DEV=/dev/sdb6       (default; block device required for order>0)
  RSWAP_SWAP_DEV=                (explicit empty value selects swapfile mode)
  RSWAP_SWAP_FILE=$HOME/swapfile (only used with an explicit loop device)
  RSWAP_SWAP_PRIORITY=10
  RSWAP_MEM_GB=48
  PAGE_SIZES_KB="4 16 32 64 128 256 512 1024 2048"
  BASE_RUN_ID=<timestamp>-native-hermit
  FINAL_ACTION=leave-hermit|unload   (default leave-hermit)
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

: "${RSWAP_SERVER_IP:?set RSWAP_SERVER_IP to the dnet-58 RDMA address}"
RSWAP_SERVER_PORT=${RSWAP_SERVER_PORT:-9400}
RSWAP_SWAP_FILE=${RSWAP_SWAP_FILE:-"$HOME/swapfile"}
# A block device is required for Hermit to receive order>0 (larger than 4 KiB)
# folios; a swap file is always split to 4 KiB by the swap allocator.
RSWAP_SWAP_DEV=${RSWAP_SWAP_DEV-/dev/sdb6}
SWAP_TARGET=${RSWAP_SWAP_DEV:-$RSWAP_SWAP_FILE}
RSWAP_MEM_GB=${RSWAP_MEM_GB:-48}
RSWAP_SWAP_PRIORITY=${RSWAP_SWAP_PRIORITY:-10}
FINAL_ACTION=${FINAL_ACTION:-leave-hermit}
BASE_RUN_ID=${BASE_RUN_ID:-"$(date +%Y%m%d-%H%M%S)-${KERNEL_TAG}-native-hermit"}
NATIVE_SWEEP_DIR="$RESULT_ROOT/$BASE_RUN_ID/native"
HERMIT_SWEEP_DIR="$RESULT_ROOT/$BASE_RUN_ID/hermit"
CLIENT_DIR="$HERMIT6_ROOT/remoteswap/client"
MANAGE_SCRIPT="$CLIENT_DIR/manage_rswap_client.sh"

case "$FINAL_ACTION" in
  leave-hermit|unload) ;;
  *) rdma_die "FINAL_ACTION must be leave-hermit or unload" ;;
esac

test -x "$MANAGE_SCRIPT" || rdma_die "missing executable $MANAGE_SCRIPT"
sudo -v

client_loaded() {
  [ -d /sys/module/rswap_client ]
}

ensure_loop_bound() {
  # Make the loop swap self-healing after a reboot or cleanup:
  #  - a loop bound to a deleted backing file is detached and re-bound;
  #  - a missing backing file is recreated (fallocate + mkswap) so the
  #    native phase's swapon works without manual setup.
  local name backing
  case "$RSWAP_SWAP_DEV" in
    /dev/loop*) ;;
    *) return 0 ;;
  esac
  name=${RSWAP_SWAP_DEV#/dev/}
  backing=$(sudo_cat "/sys/block/$name/loop/backing_file" 2>/dev/null || true)
  if [ -n "$backing" ]; then
    case "$backing" in
      *" (deleted)")
        rdma_log "loop device $RSWAP_SWAP_DEV bound to deleted file; re-binding"
        sudo losetup -d "$RSWAP_SWAP_DEV" || \
          rdma_die "losetup -d $RSWAP_SWAP_DEV failed"
        ;;
      *)
        rdma_log "loop device $RSWAP_SWAP_DEV already bound to $backing"
        return 0
        ;;
    esac
  fi
  [ -n "$RSWAP_SWAP_FILE" ] || \
    rdma_die "RSWAP_SWAP_FILE is required to bind $RSWAP_SWAP_DEV"
  if [ ! -f "$RSWAP_SWAP_FILE" ]; then
    [ -n "$RSWAP_MEM_GB" ] || \
      rdma_die "RSWAP_MEM_GB required to recreate $RSWAP_SWAP_FILE"
    rdma_log "creating swap backing file $RSWAP_SWAP_FILE ($RSWAP_MEM_GB GiB)"
    sudo fallocate -l "${RSWAP_MEM_GB}G" "$RSWAP_SWAP_FILE" || \
      rdma_die "fallocate $RSWAP_SWAP_FILE failed"
    sudo chmod 600 "$RSWAP_SWAP_FILE"
    sudo mkswap "$RSWAP_SWAP_FILE" >/dev/null 2>&1 || \
      rdma_die "mkswap $RSWAP_SWAP_FILE failed"
  fi
  rdma_log "binding $RSWAP_SWAP_DEV to $RSWAP_SWAP_FILE"
  sudo losetup "$RSWAP_SWAP_DEV" "$RSWAP_SWAP_FILE" || \
    rdma_die "losetup $RSWAP_SWAP_DEV $RSWAP_SWAP_FILE failed"
  sudo_test -b "$RSWAP_SWAP_DEV" || \
    rdma_die "loop device not usable after bind: $RSWAP_SWAP_DEV"
}

ensure_loop_bound
if [ -n "$RSWAP_SWAP_DEV" ]; then
  test -b "$SWAP_TARGET" || rdma_die "block device does not exist: $SWAP_TARGET"
else
  test -f "$SWAP_TARGET" || rdma_die "swap file does not exist: $SWAP_TARGET"
fi

swap_is_active() {
  # /proc/swaps and swapon -s escape spaces as \040; decode for a literal match.
  swapon -s | awk -v target="$SWAP_TARGET" \
    '{ path = $1; gsub(/\\040/, " ", path);
       if (path == target) { found = 1 } } END { exit !found }'
}

deactivate_other_swaps() {
  local active_swaps=() active

  mapfile -t active_swaps < <(awk -v target="$SWAP_TARGET" \
    'NR > 1 { path = $1; gsub(/\\040/, " ", path);
       if (path != target) print path }' /proc/swaps)
  for active in "${active_swaps[@]}"; do
    rdma_log "disabling non-target swap device $active"
    sudo swapoff "$active" || rdma_die "swapoff $active failed"
  done
}

activate_native_swap() {
  if swap_is_active; then
    sudo swapoff "$SWAP_TARGET"
  fi
  if client_loaded; then
    sudo rmmod rswap_client
  fi
  deactivate_other_swaps
  sudo swapon -p "$RSWAP_SWAP_PRIORITY" "$SWAP_TARGET"
}

install_hermit_client() {
  (
    cd "$CLIENT_DIR"
    RSWAP_SERVER_IP="$RSWAP_SERVER_IP" \
      RSWAP_SERVER_PORT="$RSWAP_SERVER_PORT" \
      RSWAP_SWAP_DEV="$RSWAP_SWAP_DEV" \
      RSWAP_SWAP_FILE="$RSWAP_SWAP_FILE" \
      RSWAP_MEM_GB="$RSWAP_MEM_GB" \
      RSWAP_SWAP_PRIORITY="$RSWAP_SWAP_PRIORITY" \
      ./manage_rswap_client.sh install
  )
  client_loaded || rdma_die "rswap_client did not load"
  sudo_test -r /sys/kernel/debug/hermit/remote_order_mask ||
    rdma_die "Hermit debugfs remote_order_mask is unavailable after install"
}

unload_hermit_client() {
  if swap_is_active; then
    sudo swapoff "$SWAP_TARGET"
  fi
  if client_loaded; then
    sudo rmmod rswap_client
  fi
  deactivate_other_swaps
  sudo swapon -p "$RSWAP_SWAP_PRIORITY" "$SWAP_TARGET"
}

rdma_log "phase=native: unloading rswap_client and enabling local swap"
activate_native_swap
MODES='local local-cgroup' SWEEP_DIR="$NATIVE_SWEEP_DIR" \
  "$SCRIPT_DIR/run_memcached_page_sweep.sh"

rdma_log "phase=hermit: installing rswap_client for $RSWAP_SERVER_IP:$RSWAP_SERVER_PORT"
install_hermit_client
MODES='hermit-cgroup' SWEEP_DIR="$HERMIT_SWEEP_DIR" \
  "$SCRIPT_DIR/run_memcached_page_sweep.sh"

if [ "$FINAL_ACTION" = "unload" ]; then
  rdma_log "final action: unloading rswap_client and restoring local swap"
  unload_hermit_client
else
  rdma_log "final action: leaving Hermit client and $SWAP_TARGET active"
fi

rdma_log "native results: $NATIVE_SWEEP_DIR/page-sweep-summary.csv"
rdma_log "Hermit results: $HERMIT_SWEEP_DIR/page-sweep-summary.csv"
