# Remoteswap on Hermit 6.18

Remoteswap provides the DRAM validation backend and the RDMA backend used by
the Hermit-enabled Linux `v6.18.38` tree in this repository.

## Backends

The local DRAM backend is intended for QEMU and functional testing. It has no
OFED dependency:

```bash
make -C client KDIR="$PWD/../linux-stable" BACKEND=DRAM
```

The RDMA backend uses an external Mellanox OFED build for the exact target
kernel. `OFA_DIR` must contain both `include/` and `Module.symvers`:

```bash
make -C client \
  KDIR="$PWD/../linux-stable" \
  BACKEND=RDMA \
  OFA_DIR="/usr/src/ofa_kernel-dkms/x86_64/$(make -sC ../linux-stable kernelrelease)"
```

The RDMA build does not use the in-tree Linux RDMA core. Build or install OFED
against the Hermit `v6.18.38` kernel first. Do not mix headers or symbol CRCs
from another kernel. `/usr/src/ofa_kernel/default` is usable only when its
resolved target contains the `Module.symvers` generated for this exact kernel.

## Hardware

The original deployment targets include Mellanox ConnectX-3/4/5/6 adapters
over InfiniBand or RoCE. Driver, firmware, link mode, MTU, addressing, and the
memory server must be configured consistently on both machines.

## Server

Build and start the memory server before loading the client:

```bash
OFA_DIR="/usr/src/ofa_kernel-dkms/x86_64/$(uname -r)"
make -C server OFA_DIR="$OFA_DIR"
./server/rswap-server <server-ip> <port> <pool-size-gib> <client-cpu-count>
```

The client CPU count determines the number of RDMA queues. A mismatch can
exceed the server's available queue slots.

## Client

The remote pool size, swap size, server address, and port must agree. The
existing management script exposes those settings, or the module can be loaded
directly:

```bash
sudo insmod client/rswap-client.ko \
  sip=10.0.0.2 sport=9400 rmsize=48
```

After the RDMA session is connected, successful Hermit registration emits:

```text
rswap: Hermit RDMA backend registered
```

Hermit stores folios from order 0 through `PMD_ORDER`. A successful remote
store is authoritative and skips the local swap BIO; a failed store falls back
to the native swap path. Direct asynchronous swapin and lazy polling are
controlled through `/sys/kernel/debug/hermit/`.

### Swap device

The kernel swap slot allocator only hands out order>0 (larger than 4 KiB) slots
when the swap device sets `SWP_BLKDEV`, which happens only for block devices
(raw partitions or loop devices). A swap *file* is always broken down to 4 KiB
by the swap subsystem, so `remote_order_mask` bits above bit 0 never take effect
and every remote transfer is a 4 KiB store/load.

`manage_rswap_client.sh` accepts either kind of swap target:

```bash
# Swap file (default, order-0 only).
RSWAP_SWAP_FILE="$HOME/swapfile" RSWAP_MEM_GB=48 ./manage_rswap_client.sh install

# Raw partition or existing loop device (order>0 supported).
RSWAP_SWAP_DEV=/dev/nvme0n1p5 RSWAP_MEM_GB=48 ./manage_rswap_client.sh install

# Bind a swap file to a loop device, then install against it.
RSWAP_SWAP_FILE="$HOME/swapfile" RSWAP_MEM_GB=48 ./manage_rswap_client.sh loop
# -> prints RSWAP_SWAP_DEV=/dev/loopN; then:
RSWAP_SWAP_DEV=/dev/loopN ./manage_rswap_client.sh install
```

For a block device, `rmsize` is taken from `RSWAP_MEM_GB` when set, otherwise
derived from `blockdev --getsize64` (GiB). `uninstall` detaches a `/dev/loop*`
target after swapping it off. Keep `RSWAP_MEM_GB` consistent with the actual
device size so the remote pool and the swap slots line up. Note that
`bypass_swapcache` direct swap-in is only legal on a synchronous swap device.

The transfer size is selected with `remote_order_mask`. Bit 0 is 4 KiB, bit 2
is 16 KiB, and bit 9 is 2 MiB; bit 1 is not valid. The default is `0x1`, and
bit 0 is mandatory so every configuration has a base-page path. For example:

```bash
# Enable 4 KiB, 64 KiB and 2 MiB transfers.
echo 0x211 | sudo tee /sys/kernel/debug/hermit/remote_order_mask
cat /sys/kernel/debug/hermit/effective_order_mask
cat /sys/kernel/debug/hermit/order_stats
```

When a large order is enabled, the RDMA backend first submits one variable-size
WR for the physically contiguous folio. A DMA-map, post, or completion failure
causes the request to be retried as one 4 KiB WR per base page. If the order is
disabled, the backend uses those 4 KiB WRs directly. `max_order` limits the
largest single WR supported by the RDMA client, and `effective_order_mask` is
the intersection of that capability with the runtime mask. `max_order` is a
read-only module parameter; change it by unloading and reloading the module
after `swapoff`, for example `max_order=4` for at most 64 KiB per WR.

All client CQs use `IB_POLL_DIRECT`, and polling a queue is serialized by that
queue's CQ lock. Async load contexts come from a reserved mempool so the swap-in
hot path is not dependent on a fresh `GFP_ATOMIC` slab allocation under memory
pressure. Completion publication uses release/acquire ordering between the
request status and pending count. Partial base-page submission is retained as
one transaction and retried only after its already-posted WRs complete.

The DRAM validation backend intentionally advertises only order 0 because it
always copies a folio as base pages. Large-folio DRAM tests therefore increment
the target order's fallback counter; they validate Hermit's folio accounting
and fallback path, not a variable-size RDMA WR.

The mask changes Hermit's transport only. THP/mTHP allocation remains under
Linux's `/sys/kernel/mm/transparent_hugepage/` controls.

Before unloading the client, disable the associated swap device and ensure no
remote entries or outstanding RDMA requests remain:

```bash
sudo swapoff <swap-device-or-file>
sudo rmmod rswap_client
```

Unregister first rejects new RDMA transactions, lets already-issued async
transactions finish through their normal poll callback, and then waits for all
posted WR counters before CQ/QP/cache destruction. This protects resource
lifetime but does not make remote-only swap entries recoverable after a live
unload; a successful `swapoff` remains mandatory.

## Validation

`BACKEND=DRAM` plus `tools/qemu-dram/validate-qemu-dram.sh` validates data
checksums, runtime mask switching, per-order counters, swap vmstat, profiling
output, and the absence of local swap writes after successful remote stores.

Use `THP_SIZE_KB=64 REMOTE_ORDER_MASK=0x11 BYPASS_SWAPCACHE=Y` to cover mTHP
large-folio store and load. Use `THP_SIZE_KB=2048 REMOTE_ORDER_MASK=0x201` to
cover PMD-sized THP store. Linux 6.18's PTE swapin aggregation currently covers
mTHP orders below `PMD_ORDER`, so the 2 MiB case does not imply an order-9 load.

An RDMA build only proves API and symbol compatibility. End-to-end validation
also requires the matching OFED runtime, a supported NIC, a reachable memory
server, successful queue setup, and a swap pressure test that verifies data on
reload.

## Origins

The codebase is derived from Fastswap and Canvas and retains their server
protocol and queue layout.
