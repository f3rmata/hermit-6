#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(realpath "$(dirname "$0")")
HERMIT_DIR=$(realpath "$SCRIPT_DIR/../..")
KERNEL_TREE=$(realpath "$HERMIT_DIR/linux-stable")
REMOTESWAP_CLIENT_DIR=$(realpath "$HERMIT_DIR/remoteswap-6.6/client")
WORK_DIR=${WORK_DIR:-"$SCRIPT_DIR/_work"}
INITRAMFS_DIR="$WORK_DIR/initramfs"
LOG_FILE="$WORK_DIR/qemu.log"
SERIAL_LOG="$WORK_DIR/guest-serial.log"
QEMU_BIN=${QEMU_BIN:-qemu-system-x86_64}
QEMU_ACCEL=${QEMU_ACCEL:-kvm}
QEMU_CPU=${QEMU_CPU:-host}
TIMEOUT_SEC=${TIMEOUT_SEC:-240}
SMP=${SMP:-4}
GUEST_RAM_MB=${GUEST_RAM_MB:-4096}
RSWAP_MEM_GB=${RSWAP_MEM_GB:-1}
SWAP_MB=${SWAP_MB:-1024}
MEMHOG_MB=${MEMHOG_MB:-1800}
TMPFS_FILL_MB=${TMPFS_FILL_MB:-768}
SKIP_BUILD=${SKIP_BUILD:-0}
BYPASS_SWAPCACHE=${BYPASS_SWAPCACHE:-Y}
LAZY_POLL=${LAZY_POLL:-N}
MEMHOG_READY_TIMEOUT_SEC=${MEMHOG_READY_TIMEOUT_SEC:-180}

default_sthd_cnt=$SMP
if [ "$default_sthd_cnt" -gt 4 ]; then
    default_sthd_cnt=4
fi
STHD_CNT=${STHD_CNT:-$default_sthd_cnt}

KERNEL_IMAGE="$KERNEL_TREE/arch/x86/boot/bzImage"
BRD_MODULE="$KERNEL_TREE/drivers/block/brd.ko"
RSWAP_CLIENT_MODULE="$REMOTESWAP_CLIENT_DIR/rswap-client.ko"
MEMHOG_BIN="$WORK_DIR/memhog"
SWAP_STATS_BIN="$WORK_DIR/hermit_swap_stats"
INITRAMFS_IMAGE="$WORK_DIR/initramfs.cpio.gz"

require_file() {
    local path=$1

    if [ ! -e "$path" ]; then
        echo "missing required file: $path" >&2
        exit 1
    fi
}

check_numeric_constraints() {
    if [ "$SWAP_MB" -gt $((RSWAP_MEM_GB * 1024)) ]; then
        echo "SWAP_MB ($SWAP_MB) must not exceed RSWAP_MEM_GB * 1024 ($((RSWAP_MEM_GB * 1024)))" >&2
        exit 1
    fi
}

copy_busybox_deps() {
    local target_root=$1
    local dep

    cp /usr/bin/busybox "$target_root/bin/busybox"
    while read -r dep; do
        [ -n "$dep" ] || continue
        mkdir -p "$target_root$(dirname "$dep")"
        cp "$dep" "$target_root$dep"
    done < <(ldd /usr/bin/busybox | awk '{print $3}' | grep '^/' ; echo /lib64/ld-linux-x86-64.so.2)
}

link_busybox_applets() {
    local target_root=$1
    local applet

    for applet in sh mount mkdir cat grep awk mdev insmod mkswap swapon free dmesg poweroff sleep ls dd kill rm sync; do
        ln -sf /bin/busybox "$target_root/bin/$applet"
    done
}

check_artifacts() {
    require_file "$KERNEL_IMAGE"
    require_file "$BRD_MODULE"
    require_file "$RSWAP_CLIENT_MODULE"
    require_file "$MEMHOG_BIN"
    require_file "$SWAP_STATS_BIN"
}

build_initramfs() {
    rm -rf "$INITRAMFS_DIR"
    mkdir -p "$INITRAMFS_DIR"/{bin,dev,etc,lib64,mnt,proc,sys,tmp}

    copy_busybox_deps "$INITRAMFS_DIR"
    link_busybox_applets "$INITRAMFS_DIR"

    cp "$MEMHOG_BIN" "$INITRAMFS_DIR/bin/memhog"
    cp "$SWAP_STATS_BIN" "$INITRAMFS_DIR/bin/hermit_swap_stats"
    cp "$BRD_MODULE" "$INITRAMFS_DIR/brd.ko"
    cp "$RSWAP_CLIENT_MODULE" "$INITRAMFS_DIR/rswap-client.ko"

    cat > "$INITRAMFS_DIR/init" <<EOF
#!/bin/sh
set -eu

export PATH=/bin

now_ms() {
    awk '{ printf "%d\n", \$1 * 1000 }' /proc/uptime
}

log_timing() {
    echo "TIMING: step=\$1 start_ms=\$2 end_ms=\$3 delta_ms=\$((\$3 - \$2)) \$4"
}

run_step() {
    step_name=\$1
    shift
    step_start=\$(now_ms)
    if "\$@"; then
        step_rc=0
    else
        step_rc=\$?
    fi
    step_end=\$(now_ms)
    log_timing "\$step_name" "\$step_start" "\$step_end" "rc=\$step_rc"
    return "\$step_rc"
}

capture_dmesg() {
    dmesg > "\$1"
}

emit_new_hermit_dmesg() {
    label=\$1
    before_log=\$2
    after_log=\$3
    before_lines=\$(awk 'END { print NR + 0 }' "\$before_log")

    awk -v start="\$before_lines" 'NR > start { print }' "\$after_log" \
        | grep -E 'major swap duration|minor swap duration|swap-out   duration|non-swap   duration|RDMA read  latency|RDMA write latency|check references|reverse mapping|hermit check refs|hermit rmapping|TLB flush dirty|TLB flush          |Poll wait|Poll All|Demand|Prefetch|HitOnCache|TotalSwapOut|TotalReclaim|BatchReclaim|HermitSwapOut|HermitIsoVpages|HermitIsoVaddrs|HermitReclaim|Optimisic Faild|Major   SPF|Minor   SPF|Swapout SPF|Hmt out SPF' \
        | while IFS= read -r line; do
            [ -n "\$line" ] || continue
            echo "HERMIT_DMESG: label=\$label line=\$line"
        done || true
}

read_rswap_dram_counter() {
    counter_name=\$1
    counter_path="/sys/kernel/debug/rswap_dram/\$counter_name"

    if [ -r "\$counter_path" ]; then
        cat "\$counter_path"
    else
        echo 0
    fi
}

dump_rswap_dram_stats() {
    label=\$1
    stores=\$(read_rswap_dram_counter stores)
    loads=\$(read_rswap_dram_counter loads)
    load_misses=\$(read_rswap_dram_counter load_misses)
    errors=\$(read_rswap_dram_counter errors)

    echo "RSWAP_DRAM_STATS: label=\$label stores=\$stores loads=\$loads load_misses=\$load_misses errors=\$errors"
}

read_vmstat_counter() {
    counter_name=\$1
    awk -v key="\$counter_name" '\$1 == key { print \$2; found = 1 } END { if (!found) print 0 }' /proc/vmstat
}

dump_swap_vmstat() {
    label=\$1
    pswpin=\$(read_vmstat_counter pswpin)
    pswpout=\$(read_vmstat_counter pswpout)

    echo "SWAP_VMSTAT: label=\$label pswpin=\$pswpin pswpout=\$pswpout"
}

wait_for_memhog_ready() {
    memhog_pid=\$1
    ready_file=\$2
    timeout_sec=\$3
    start_ms=\$(now_ms)
    timeout_ms=\$((timeout_sec * 1000))

    while [ ! -e "\$ready_file" ]; do
        if ! kill -0 "\$memhog_pid" 2>/dev/null; then
            echo "MEMHOG_STATE: wait_failed pid=\$memhog_pid reason=exited_before_ready"
            return 1
        fi

        now=\$(now_ms)
        if [ \$((now - start_ms)) -ge "\$timeout_ms" ]; then
            echo "MEMHOG_STATE: wait_failed pid=\$memhog_pid reason=timeout timeout_sec=\$timeout_sec"
            return 1
        fi

        sleep 1
    done

    echo "MEMHOG_STATE: ready pid=\$memhog_pid file=\$ready_file"
    return 0
}

set_hermit_flag() {
    key=\$1
    value=\$2
    path="/sys/kernel/debug/hermit/\$key"

    if [ -w "\$path" ]; then
        if printf '%s' "\$value" > "\$path"; then
            echo "HERMIT_CONFIG: key=\$key value=\$value status=ok"
        else
            echo "HERMIT_CONFIG: key=\$key value=\$value status=write_failed"
        fi
    else
        echo "HERMIT_CONFIG: key=\$key value=\$value status=unavailable"
    fi
}

configure_hermit() {
    set_hermit_flag vaddr_swapout Y
    set_hermit_flag batch_swapout Y
    set_hermit_flag batch_io Y
    set_hermit_flag bypass_swapcache $BYPASS_SWAPCACHE
    set_hermit_flag speculative_io Y
    set_hermit_flag speculative_lock Y
    set_hermit_flag lazy_poll $LAZY_POLL
    set_hermit_flag apt_reclaim Y
    set_hermit_flag sthd_cnt $STHD_CNT
}

ln -sf /proc/mounts /etc/mtab

mount -t proc none /proc
mount -t sysfs none /sys
mount -t devtmpfs none /dev
if [ -w /proc/sys/kernel/hotplug ]; then
    echo /bin/mdev > /proc/sys/kernel/hotplug
else
    echo "INITRAMFS: kernel hotplug helper unavailable; using mdev -s only"
fi
mdev -s
mount -t debugfs none /sys/kernel/debug

echo 100 > /proc/sys/vm/swappiness

boot_start_ms=\$(now_ms)

run_step load_brd insmod /brd.ko rd_nr=1 rd_size=$((SWAP_MB * 1024))
run_step wait_brd sleep 1
mdev -s
run_step mkswap_ramdisk mkswap /dev/ram0
run_step swapon_ramdisk swapon /dev/ram0

run_step load_rswap_client insmod /rswap-client.ko rmsize=$RSWAP_MEM_GB
run_step configure_hermit configure_hermit
run_step reset_hermit_stats /bin/hermit_swap_stats reset boot
dump_swap_vmstat boot
PSWPIN_BOOT=\$(read_vmstat_counter pswpin)
PSWPOUT_BOOT=\$(read_vmstat_counter pswpout)

mkdir -p /mnt
run_step mount_tmpfs mount -t tmpfs -o size=${TMPFS_FILL_MB}M tmpfs /mnt

memhog_spawn_start=\$(now_ms)
MEMHOG_READY_FILE=/tmp/memhog.ready /bin/memhog $MEMHOG_MB reload-on-signal &
MEMHOG_PID=\$!
memhog_spawn_end=\$(now_ms)
log_timing memhog_spawn "\$memhog_spawn_start" "\$memhog_spawn_end" "pid=\$MEMHOG_PID"

if ! run_step wait_memhog_ready wait_for_memhog_ready "\$MEMHOG_PID" /tmp/memhog.ready $MEMHOG_READY_TIMEOUT_SEC; then
    echo "VALIDATION: FAIL"
    poweroff -f
fi

dd_start=\$(now_ms)
dd if=/dev/zero of=/mnt/fill bs=1M count=$TMPFS_FILL_MB || true
sync
dd_end=\$(now_ms)
log_timing tmpfs_fill "\$dd_start" "\$dd_end" "count_mb=$TMPFS_FILL_MB"

run_step drop_tmpfs_fill rm -f /mnt/fill
run_step settle_after_pressure sleep 2

capture_dmesg /tmp/dmesg.before.before_reload
dump_rswap_dram_stats before_reload
dump_swap_vmstat before_reload
/bin/hermit_swap_stats stats before_reload
capture_dmesg /tmp/dmesg.after.before_reload
emit_new_hermit_dmesg before_reload /tmp/dmesg.before.before_reload /tmp/dmesg.after.before_reload

RSWAP_STORES_BEFORE=\$(read_rswap_dram_counter stores)
RSWAP_LOADS_BEFORE=\$(read_rswap_dram_counter loads)

run_step reset_hermit_stats_reload /bin/hermit_swap_stats reset after_pressure

reload_signal_start=\$(now_ms)
kill -USR1 \$MEMHOG_PID || true
reload_signal_end=\$(now_ms)
log_timing memhog_reload_signal "\$reload_signal_start" "\$reload_signal_end" "pid=\$MEMHOG_PID"

memhog_wait_start=\$(now_ms)
if wait \$MEMHOG_PID; then
    MEMHOG_RC=0
else
    MEMHOG_RC=\$?
fi
memhog_wait_end=\$(now_ms)
log_timing memhog_wait "\$memhog_wait_start" "\$memhog_wait_end" "pid=\$MEMHOG_PID rc=\$MEMHOG_RC"

capture_dmesg /tmp/dmesg.before.after_reload
dump_rswap_dram_stats after_reload
dump_swap_vmstat after_reload
/bin/hermit_swap_stats stats after_reload
capture_dmesg /tmp/dmesg.after.after_reload
emit_new_hermit_dmesg after_reload /tmp/dmesg.before.after_reload /tmp/dmesg.after.after_reload

RSWAP_LOADS_AFTER=\$(read_rswap_dram_counter loads)
RSWAP_ERRORS_AFTER=\$(read_rswap_dram_counter errors)
PSWPIN_AFTER=\$(read_vmstat_counter pswpin)
PSWPOUT_AFTER=\$(read_vmstat_counter pswpout)

boot_end_ms=\$(now_ms)
log_timing end_to_end_guest "\$boot_start_ms" "\$boot_end_ms" "stores=\$RSWAP_STORES_BEFORE loads_after=\$RSWAP_LOADS_AFTER errors=\$RSWAP_ERRORS_AFTER pswpin_delta=\$((PSWPIN_AFTER - PSWPIN_BOOT)) pswpout_delta=\$((PSWPOUT_AFTER - PSWPOUT_BOOT))"

if [ "\$RSWAP_STORES_BEFORE" -gt 0 ] &&
   [ "\$RSWAP_LOADS_AFTER" -gt "\$RSWAP_LOADS_BEFORE" ] &&
   [ "\$RSWAP_ERRORS_AFTER" -eq 0 ] &&
   [ "\$PSWPIN_AFTER" -gt "\$PSWPIN_BOOT" ] &&
   [ "\$PSWPOUT_AFTER" -gt "\$PSWPOUT_BOOT" ] &&
   [ "\$MEMHOG_RC" -eq 0 ]; then
    echo "VALIDATION: PASS"
    poweroff -f
fi

echo "VALIDATION: FAIL"
poweroff -f
EOF
    chmod +x "$INITRAMFS_DIR/init"

    (cd "$INITRAMFS_DIR" && find . -print0 | cpio --null -ov --format=newc | gzip -9 > "$INITRAMFS_IMAGE") >/dev/null
}

run_qemu() {
    local qemu_rc=0

    rm -f "$LOG_FILE" "$SERIAL_LOG"
    timeout --foreground "$TIMEOUT_SEC" \
        "$QEMU_BIN" \
        -machine "accel=$QEMU_ACCEL" \
        -cpu "$QEMU_CPU" \
        -m "$GUEST_RAM_MB" \
        -smp "$SMP" \
        -display none \
        -serial "file:$SERIAL_LOG" \
        -monitor none \
        -no-reboot \
        -kernel "$KERNEL_IMAGE" \
        -initrd "$INITRAMFS_IMAGE" \
        -append "console=ttyS0,115200 earlyprintk=serial,ttyS0,115200 rdinit=/init loglevel=7 noxen nokaslr" > "$LOG_FILE" 2>&1 || qemu_rc=$?

    if [ "$qemu_rc" -ne 0 ] && [ "$qemu_rc" -ne 124 ]; then
        return "$qemu_rc"
    fi
}

extract_summary() {
    if [ -f "$SERIAL_LOG" ]; then
        grep -E '^(TIMING|MEMHOG_STATE|MEMHOG_TIMING|MEMHOG_CHECKSUM|HERMIT_SWAP_STATS|HERMIT_DMESG|RSWAP_DRAM_STATS|SWAP_VMSTAT|VALIDATION):' "$SERIAL_LOG" || true
    fi
}

check_numeric_constraints

if [ "$SKIP_BUILD" != "1" ]; then
    "$SCRIPT_DIR/build-qemu-dram.sh"
fi

check_artifacts
build_initramfs
run_qemu
extract_summary

if [ -f "$SERIAL_LOG" ] && tr -d '\r' < "$SERIAL_LOG" | grep -q '^VALIDATION: PASS$'; then
    echo "QEMU + Hermit DRAM validation succeeded. Serial log: $SERIAL_LOG"
    exit 0
fi

echo "QEMU + Hermit DRAM validation failed. Serial log: $SERIAL_LOG, QEMU log: $LOG_FILE" >&2
exit 1
