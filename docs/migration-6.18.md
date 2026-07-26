# Hermit migration to Linux 6.18

## Baseline

- Kernel: Linux `v6.18.38`
- Hermit branch: `hermit-6.18`
- Previous migration snapshot: Linux `v6.6.138`
- RDMA provider: external Mellanox OFED built for the target kernel

The historical 6.6 plans remain under `docs/plan/`. This document records the
6.18 result rather than rewriting those earlier stages.

## Kernel integration

Hermit is selected by `CONFIG_HERMIT` in `mm/Kconfig`. The core implementation
is split into control (`mm/hermit.c`), backend dispatch and remote-entry
tracking (`mm/hermit_backend.c`), and stable statistics/ABI
(`mm/hermit_stats.c`).

Linux 6.18 folio APIs are used throughout. Backend I/O accepts order 0 through
`PMD_ORDER`; `CONFIG_HERMIT` selects `ARCH_WANTS_THP_SWAP` so THP swap support
can be enabled. The memcg reclaim call uses the 6.18 five-argument
`try_to_free_mem_cgroup_pages()` interface.

## Authoritative copy protocol

Each remote folio is represented by an XArray extent. All slots are reserved
before I/O and become committed markers only after the complete backend store
succeeds. The state transitions are:

```text
new writeout -> reserve every slot in the folio extent
remote store succeeds -> commit every marker -> skip local swap BIO
remote store fails -> erase reservations -> native swap write
slot freed/reused -> erase that marker (partial invalidation is supported)
```

Zeromap, zswap, and native local writes start a new generation with the remote
marker cleared. A remote-marked load failure is reported as an I/O failure;
the kernel must not fall back to a local copy that was deliberately never
written.

## Swapin

The normal swapcache path calls the backend synchronously for remote entries.
A large read is accepted only when the entire candidate range belongs to one
committed extent. With `bypass_swapcache=Y`, an entry is eligible for direct
swapin when it is remote-marked and has a single swap reference. Linux 6.18's
PTE fault allocator can reconstruct mTHP orders 2–8; PMD-order THP swapout is
supported, but the same PTE fault path deliberately excludes `PMD_ORDER`.

With `speculative_io=Y`, the load is issued before fault metadata work. A
request-scoped `hermit_io` identifies the folio, transfer order, queue CPU,
backend transaction and completion status. With `lazy_poll=Y`, non-blocking
poll returns `-EAGAIN` until that specific transaction completes; blocking
poll waits and returns its completion status.

## Transfer order and RDMA fallback

`/sys/kernel/debug/hermit/remote_order_mask` is writable at runtime. Bit 0 is
4 KiB, bits 2 through 9 are 16 KiB through 2 MiB, and the default is `0x1`.
Bit 0 is mandatory. `effective_order_mask` reports the intersection with the
registered backend's `supported_order_mask`; `order_stats` reports per-order
stores, loads, 4 KiB fallbacks and errors. This control affects transfer I/O
only and does not change Linux THP/mTHP allocation policy.

For an enabled large order the RDMA backend maps the contiguous folio and posts
one variable-length WR. DMA-map, post, and work-completion failures are retried
as one 4 KiB child WR per base page. Each child records its actual DMA length
and direction, and the parent transaction owns the pending count and first
error. Async polling therefore observes one logical request even when fallback
creates many WRs.

## Memcg reclaim

When `apt_reclaim=Y`, a non-root memcg with a finite `memory.max` schedules
asynchronous reclaim before a charge reaches the hard limit. The target margin
is controlled by `/sys/kernel/debug/hermit/reclaim_headroom_pages`; it defaults
to 65536 pages (256 MiB on x86-64), and writing zero disables proactive
reclaim without changing `apt_reclaim`.

`reclaim_mode=1` uses up to `sthd_cnt` work items on the unbound workqueue;
other modes use one. Concurrent workers divide the current margin deficit
instead of each reclaiming the entire deficit. A worker that made progress
requeues itself when the retry budget expires and the margin is still below
the target. Work is cancelled before the memcg is freed.

The RDMA benchmark defaults to the concurrent configuration below. Both values
can be overridden per run:

```bash
export RECLAIM_MODE=1
export RECLAIM_HEADROOM_PAGES=65536
export STHD_CNT=16
```

## Preserved ABI

- x86-64 syscall 471: `reset_swap_stats`
- x86-64 syscall 472: `get_swap_stats`
- `/sys/kernel/debug/hermit/*`
- Existing dmesg/profiling field names consumed by experiment scripts

## RDMA provider

The client Makefile keeps DRAM independent of OFED. `BACKEND=RDMA` always uses
`OFA_DIR/include` and `OFA_DIR/Module.symvers`. The OFED tree must have been
built for this exact 6.18 kernel; enabling the kernel tree's in-tree
`CONFIG_INFINIBAND` is not part of this deployment.

## Validation matrix

| Check | Environment | Status criterion |
| --- | --- | --- |
| Hermit/MM objects | local compiler | no warnings or errors |
| `bzImage` | local compiler | complete link succeeds |
| DRAM client | target kernel tree | `rswap-client.ko` links |
| QEMU normal poll | TCG/KVM | checksum and all validation checks pass |
| QEMU lazy poll | TCG/KVM | same checks with `LAZY_POLL=Y` |
| QEMU 64 KiB mTHP | TCG/KVM | order-4 store/load, checksum and mask switch pass |
| QEMU 2 MiB THP | TCG/KVM | order-9 store, checksum and no backend errors |
| RDMA source object | target kernel RDMA headers | `rswap_rdma_ops.o` compiles |
| RDMA client | matching OFED tree | modpost has no unresolved/CRC errors |
| RDMA end to end | NIC + memory server | connect, pressure, reload checksum pass |

OFED modpost and RDMA end-to-end checks require an external OFED installation;
end-to-end testing additionally needs actual RDMA hardware and a running
server. They must not be inferred from the DRAM QEMU or source-object result.

## Known constraints

- Backend calls currently use a regular RCU read-side critical section. The
  present DRAM memcpy and RDMA busy-poll callbacks do not sleep.
- An async issue and its later poll are separate backend calls. Do not unload
  the backend while swap is active or requests are outstanding.
- The DRAM backend consumes guest RAM and is therefore a functional mock, not
  a capacity or latency model of disaggregated memory.
