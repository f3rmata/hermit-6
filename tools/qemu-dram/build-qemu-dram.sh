#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(realpath "$(dirname "$0")")
HERMIT_DIR=$(realpath "$SCRIPT_DIR/../..")
KERNEL_TREE=$(realpath "$HERMIT_DIR/linux-stable")
REMOTESWAP_CLIENT_DIR=$(realpath "$HERMIT_DIR/remoteswap/client")
WORK_DIR=${WORK_DIR:-"$SCRIPT_DIR/_work"}
JOBS=${JOBS:-$(nproc)}
KERNEL_CONFIG=${KERNEL_CONFIG:-config}
MEMHOG_SRC="$SCRIPT_DIR/src/memhog.c"
MEMHOG_BIN="$WORK_DIR/memhog"
SWAP_STATS_SRC="$SCRIPT_DIR/src/hermit_swap_stats.c"
SWAP_STATS_BIN="$WORK_DIR/hermit_swap_stats"
KCFLAGS_EXTRA=${KCFLAGS_EXTRA:-}
HOSTCFLAGS_EXTRA=${HOSTCFLAGS_EXTRA:-}

pick_default_gcc() {
    local candidate

    for candidate in gcc-14 gcc-13 gcc-12 gcc-11 gcc-10 gcc-9 gcc; do
        if command -v "$candidate" >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done

    echo "gcc"
}

compiler_major_version() {
    local compiler=$1

    "$compiler" -dumpversion | awk -F. '{ print $1 }'
}

setup_toolchain() {
    local cc_default
    local hostcc_default
    local cc_major

    cc_default=$(pick_default_gcc)
    hostcc_default=$cc_default

    CC_BIN=${CC_BIN:-$cc_default}
    HOSTCC_BIN=${HOSTCC_BIN:-$hostcc_default}

    cc_major=$(compiler_major_version "$CC_BIN")
    if [ "$cc_major" -ge 15 ]; then
        if [ -z "$KCFLAGS_EXTRA" ]; then
            KCFLAGS_EXTRA="-std=gnu11"
        fi
        if [ -z "$HOSTCFLAGS_EXTRA" ]; then
            HOSTCFLAGS_EXTRA="-std=gnu11"
        fi
    fi
}

prepare_kernel_config() {
    if [ ! -f "$KERNEL_TREE/.config" ]; then
        if [ -f "$KERNEL_TREE/$KERNEL_CONFIG" ]; then
            cp "$KERNEL_TREE/$KERNEL_CONFIG" "$KERNEL_TREE/.config"
        else
            make -C "$KERNEL_TREE" \
                CC="$CC_BIN" \
                HOSTCC="$HOSTCC_BIN" \
                KCFLAGS="$KCFLAGS_EXTRA" \
                HOSTCFLAGS="$HOSTCFLAGS_EXTRA" \
                x86_64_defconfig
        fi
    fi

    if [ -x "$KERNEL_TREE/scripts/config" ]; then
        "$KERNEL_TREE/scripts/config" --file "$KERNEL_TREE/.config" \
            -e HERMIT \
            -e MEMCG \
            -e SWAP \
            -e DEBUG_FS \
            -e TRANSPARENT_HUGEPAGE \
            -d TRANSPARENT_HUGEPAGE_ALWAYS \
            -e TRANSPARENT_HUGEPAGE_MADVISE \
            -d TRANSPARENT_HUGEPAGE_NEVER \
            -m BLK_DEV_RAM \
            -d ZSWAP
    fi

    make -C "$KERNEL_TREE" \
        CC="$CC_BIN" \
        HOSTCC="$HOSTCC_BIN" \
        KCFLAGS="$KCFLAGS_EXTRA" \
        HOSTCFLAGS="$HOSTCFLAGS_EXTRA" \
        olddefconfig
}

build_helpers() {
    mkdir -p "$WORK_DIR"
    "$CC_BIN" -static -O2 "$MEMHOG_SRC" -o "$MEMHOG_BIN"
    "$CC_BIN" -static -O2 "$SWAP_STATS_SRC" -o "$SWAP_STATS_BIN"
}

build_kernel_and_modules() {
    make -C "$KERNEL_TREE" \
        CC="$CC_BIN" \
        HOSTCC="$HOSTCC_BIN" \
        KCFLAGS="$KCFLAGS_EXTRA" \
        HOSTCFLAGS="$HOSTCFLAGS_EXTRA" \
        -j"$JOBS" \
        bzImage modules_prepare drivers/block/brd.ko

    make -C "$REMOTESWAP_CLIENT_DIR" \
        CC="$CC_BIN" \
        HOSTCC="$HOSTCC_BIN" \
        BACKEND=DRAM \
        KDIR="$KERNEL_TREE" \
        KBUILD_MODPOST_WARN=1
}

setup_toolchain
echo "Using kernel compiler: $CC_BIN"
echo "Using host compiler: $HOSTCC_BIN"
if [ -n "$KCFLAGS_EXTRA" ]; then
    echo "Applying KCFLAGS compatibility flags: $KCFLAGS_EXTRA"
fi
if [ -n "$HOSTCFLAGS_EXTRA" ]; then
    echo "Applying HOSTCFLAGS compatibility flags: $HOSTCFLAGS_EXTRA"
fi

build_helpers
prepare_kernel_config
build_kernel_and_modules

echo "Hermit QEMU DRAM build completed. Artifacts are under $WORK_DIR"
