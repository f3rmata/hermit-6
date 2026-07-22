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

Linux 6.18 folio APIs are used throughout. Backend I/O is limited to order-0
folios; larger folios stay on the native swap path. The memcg reclaim call uses
the 6.18 five-argument `try_to_free_mem_cgroup_pages()` interface.

## Authoritative copy protocol

Each swap entry has an XArray marker only after a backend store succeeds. The
state transitions are:

```text
new writeout -> clear old remote marker
remote store succeeds -> install marker -> skip local swap BIO
remote store fails -> no marker -> native swap write
slot freed/reused -> erase marker
```

Zeromap, zswap, and native local writes start a new generation with the remote
marker cleared. A remote-marked load failure is reported as an I/O failure;
the kernel must not fall back to a local copy that was deliberately never
written.

## Swapin

The normal swapcache path calls the backend synchronously for remote entries.
With `bypass_swapcache=Y`, an entry is eligible for direct swapin only when it
is remote-marked, has a single swap reference, and can be represented by an
order-0 folio.

With `speculative_io=Y`, the load is issued before fault metadata work. The
saved CPU identifies the RDMA queue used for the later poll. With
`lazy_poll=Y`, `peek_load()` first processes completions until the request is
ready, then `poll_load()` drains the queue and returns any completion error.

## Memcg reclaim

When `apt_reclaim=Y`, a non-root memcg with a finite `memory.max` schedules
asynchronous reclaim when its margin falls below 2048 pages. `reclaim_mode=1`
uses up to `sthd_cnt` work items; other modes use one. Work is cancelled before
the memcg is freed.

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
| RDMA client | matching OFED tree | modpost has no unresolved/CRC errors |
| RDMA end to end | NIC + memory server | connect, pressure, reload checksum pass |

The last two checks require an external OFED installation and, for end-to-end
testing, actual RDMA hardware and a running server. They must not be inferred
from the DRAM QEMU result.

## Known constraints

- Backend calls currently use a regular RCU read-side critical section. The
  present DRAM memcpy and RDMA busy-poll callbacks do not sleep.
- An async issue and its later poll are separate backend calls. Do not unload
  the backend while swap is active or requests are outstanding.
- The DRAM backend consumes guest RAM and is therefore a functional mock, not
  a capacity or latency model of disaggregated memory.
