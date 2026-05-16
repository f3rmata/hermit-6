#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(realpath "$(dirname "$0")")

QEMU_ACCEL=${QEMU_ACCEL:-kvm}
QEMU_CPU=${QEMU_CPU:-host}
RSWAP_MEM_GB=${RSWAP_MEM_GB:-1}

"$SCRIPT_DIR/build-qemu-dram.sh"
SKIP_BUILD=1 \
QEMU_ACCEL="$QEMU_ACCEL" \
QEMU_CPU="$QEMU_CPU" \
RSWAP_MEM_GB="$RSWAP_MEM_GB" \
"$SCRIPT_DIR/validate-qemu-dram.sh"
