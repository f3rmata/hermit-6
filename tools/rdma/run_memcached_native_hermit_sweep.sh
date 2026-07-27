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
  RSWAP_SWAP_FILE=$HOME/swapfile
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
RSWAP_MEM_GB=${RSWAP_MEM_GB:-48}
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
test -f "$RSWAP_SWAP_FILE" || rdma_die "swap file does not exist: $RSWAP_SWAP_FILE"
sudo -v

client_loaded() {
  [ -d /sys/module/rswap_client ]
}

swap_is_active() {
  swapon --noheadings --raw --output NAME | grep -Fxq "$RSWAP_SWAP_FILE"
}

activate_native_swap() {
  if swap_is_active; then
    sudo swapoff "$RSWAP_SWAP_FILE"
  fi
  if client_loaded; then
    sudo rmmod rswap_client
  fi
  sudo swapon "$RSWAP_SWAP_FILE"
}

install_hermit_client() {
  (
    cd "$CLIENT_DIR"
    RSWAP_SERVER_IP="$RSWAP_SERVER_IP" \
      RSWAP_SERVER_PORT="$RSWAP_SERVER_PORT" \
      RSWAP_SWAP_FILE="$RSWAP_SWAP_FILE" \
      RSWAP_MEM_GB="$RSWAP_MEM_GB" \
      ./manage_rswap_client.sh install
  )
  client_loaded || rdma_die "rswap_client did not load"
  sudo_test -r /sys/kernel/debug/hermit/remote_order_mask ||
    rdma_die "Hermit debugfs remote_order_mask is unavailable after install"
}

unload_hermit_client() {
  if swap_is_active; then
    sudo swapoff "$RSWAP_SWAP_FILE"
  fi
  if client_loaded; then
    sudo rmmod rswap_client
  fi
  sudo swapon "$RSWAP_SWAP_FILE"
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
  rdma_log "final action: leaving Hermit client and its swapfile active"
fi

rdma_log "native results: $NATIVE_SWEEP_DIR/page-sweep-summary.csv"
rdma_log "Hermit results: $HERMIT_SWEEP_DIR/page-sweep-summary.csv"
