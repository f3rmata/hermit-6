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
  OFA_DIR=/usr/src/ofa_kernel/default
```

The RDMA build does not use the in-tree Linux RDMA core. Build or install OFED
against the Hermit `v6.18.38` kernel first. Do not mix headers or symbol CRCs
from another kernel.

## Hardware

The original deployment targets include Mellanox ConnectX-3/4/5/6 adapters
over InfiniBand or RoCE. Driver, firmware, link mode, MTU, addressing, and the
memory server must be configured consistently on both machines.

## Server

Build and start the memory server before loading the client:

```bash
make -C server OFA_DIR=/usr/src/ofa_kernel/default
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
the intersection of that capability with the runtime mask.

The mask changes Hermit's transport only. THP/mTHP allocation remains under
Linux's `/sys/kernel/mm/transparent_hugepage/` controls.

Before unloading the client, disable the associated swap device and ensure no
remote entries or outstanding RDMA requests remain:

```bash
sudo swapoff <swap-device-or-file>
sudo rmmod rswap_client
```

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
