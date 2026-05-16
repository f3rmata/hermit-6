# Hermit Linux 6.6 第五阶段适配说明：5.14 profiling 点迁移

## 1. 文档目的

这份文档记录第五阶段对 `hermit/linux-stable` 的 profiling 迁移。

第四阶段已经让 DRAM backend 跑通 swapout / swapin 数据路径，但 profiling
仍然主要停留在 6.6 `do_swap_page()` 的局部统计上，很多 5.14 Hermit
点位会显示为 `0ns`。第五阶段的目标是恢复 5.14 的观测语义：

```text
page fault total latency
  -> swap major / minor / non-swap duration
  -> swapin page fault breakdown
  -> memcg charge / reclaim breakdown
  -> backend read/write latency
  -> swap readahead / prefetch breakdown
  -> reclaim / swapout breakdown
```

本阶段继续保留 5.14 的 counter 名称、enum 顺序和 report 输出格式。
即使当前 DRAM backend 不是 RDMA，也仍然写入 `ADC_RDMA_READ_LAT` 和
`ADC_RDMA_WRITE_LAT`，避免破坏现有 userspace 解析。

## 2. 统一 profiling context

新增文件：

- `linux-stable/include/linux/hermit_profile.h`

新增结构：

```c
struct hermit_pf_profile_ctx {
	int adc_pf_bits;
	uint64_t pf_breakdown[NUM_ADC_PF_BREAKDOWN_TYPE];
};
```

`task_struct` 中新增：

```c
struct hermit_pf_profile_ctx *hermit_pf_ctx;
```

设计原因：

- 6.6 的 `handle_mm_fault()`、`__handle_mm_fault()`、`handle_pte_fault()`
  是全内核共享接口，直接把 `adc_pf_bits` 和 `pf_breakdown` 参数一路传下去
  会扩大签名修改面。
- 第五阶段改用 per-task 当前 fault context：x86 page fault 入口 push，
  `do_swap_page()`、memcg charge、vmscan 和 page_io 通过 `current` 获取。
- `kernel/fork.c` 在 fork 后把子任务的 `hermit_pf_ctx` 清空，避免复制父任务
  正在使用的栈上 context 指针。

当前路径：

```text
exc_page_fault()
  -> hermit_pf_profile_ctx on stack
  -> current->hermit_pf_ctx = &ctx
  -> handle_page_fault()
  -> handle_mm_fault()
  -> do_swap_page() / memcg / vmscan / page_io
  -> current->hermit_pf_ctx = old_ctx
  -> record_adc_pf_time()
  -> parse_adc_pf_breakdown()
```

`do_swap_page()` 仍保留 fallback：如果不是从 x86 page fault 入口进入，
会创建本地 context 并按旧逻辑自行完成记录。

## 3. 已迁移点位

### 3.1 x86 page fault 外层

文件：

- `linux-stable/arch/x86/mm/fault.c`

迁移内容：

- `ADC_TRAP_TO_KERNEL`
- `ADC_RET_TO_USER`
- `ADC_TOTAL_PF`
- `ADC_SWAP_MAJOR_DUR`
- `ADC_SWAP_MINOR_DUR`
- `ADC_NON_SWAP_DUR`

说明：

- `ADC_LOCK_GET_PTE` 的开始时间在 x86 fault 入口建立。
- swap fault 会在 `do_swap_page()` 识别 swap entry 后结束
  `ADC_LOCK_GET_PTE`，并进入 `ADC_LOOKUP_SWAPCACHE`。
- 非 swap fault 目前只记录总耗时到 `ADC_NON_SWAP_DUR`，不进入
  `parse_adc_pf_breakdown()`。

### 3.2 swapin / do_swap_page

文件：

- `linux-stable/mm/memory.c`

迁移内容：

- `ADC_LOOKUP_SWAPCACHE`
- `ADC_PAGE_IO`
- `ADC_CGROUP_ACCOUNT`
- `ADC_UPD_METADATA`
- `ADC_SETPTE`
- `ADC_SET_PAGEMAP_UNLOCK`
- `ADC_ALLOC_PAGE`
- `ADC_POLL_LOAD`
- `ADC_ONDEMAND_SWAPIN`
- `ADC_OPTIM_FAILED`

路径：

```text
do_swap_page()
  -> swap_cache_get_folio()
  -> direct backend read or swapin_readahead()
  -> mem_cgroup_swapin_charge_folio()
  -> pte metadata update
  -> set_pte_at()
  -> optional hermit_poll_read()
```

关键变化：

- `do_swap_page()` 不再无条件使用本地 `pf_breakdown[]`。
  如果当前任务已有 `hermit_pf_ctx`，就复用 x86 外层 context。
- direct swapin 的 `hermit_issue_read()` 和 lazy poll 继续统计到同一份
  `ADC_PAGE_IO` / `ADC_POLL_LOAD` breakdown。
- 成功、fallback、OOM、SIGBUS、pte race 等退出路径都会闭合已经打开的
  `PAGE_IO`、`UPD_METADATA`、`SETPTE` 和 `SET_PAGEMAP_UNLOCK` section。

### 3.3 memcg charge / reclaim

文件：

- `linux-stable/mm/memcontrol.c`
- `linux-stable/mm/vmscan.c`

迁移内容：

- `ADC_CGROUP_ACCOUNT`
- `ADC_PAGE_RECLAIM`
- `ADC_PF_SWAPOUT_BIT`

路径：

```text
mem_cgroup_swapin_charge_folio()
  -> charge_memcg()
  -> try_charge_memcg()
       -> ADC_CGROUP_ACCOUNT end
       -> ADC_PAGE_RECLAIM start
       -> try_to_free_mem_cgroup_pages()
       -> ADC_PAGE_RECLAIM end
       -> ADC_CGROUP_ACCOUNT restart
```

`scan_control` 新增 Hermit context 指针：

```c
int *hermit_adc_pf_bits;
uint64_t *hermit_pf_breakdown;
```

这样 `try_to_free_mem_cgroup_pages()` 进入 `shrink_lruvec()` /
`shrink_folio_list()` 后仍然可以更新同一份 page fault breakdown。

`hermit_try_to_free_mem_cgroup_pages()` 也会把显式传入的 `pf_breakdown[]`
临时 push 到当前任务 context，供 Hermit swap thread / async reclaim 使用。

### 3.4 vmscan / reclaim / swapout

文件：

- `linux-stable/mm/vmscan.c`

迁移内容：

- `ADC_PG_CHECK_REF`
- `ADC_TRY_TO_UNMAP`
- `ADC_TLB_FLUSH_DIRTY`
- `ADC_BATCHING_OUT`
- `ADC_RLS_PG_RM_MAP`
- `ADC_UNMAP_TLB_FLUSH`
- `ADC_SHRNK_ACTV_LST`
- `ADC_SHRNK_SLAB`
- `ADC_RMAP1_LAT`
- `ADC_RMAP2_LAT`
- `ADC_TLB_FLUSH_DIR`
- `ADC_TLB_FLUSH_LAT`
- `ADC_RECLAIM`

6.6 folio 映射：

```text
5.14 shrink_page_list()
  -> 6.6 shrink_folio_list()

5.14 page_check_references()
  -> 6.6 folio_check_references()

5.14 try_to_unmap(page)
  -> 6.6 try_to_unmap(folio)

5.14 try_to_release_page() / __remove_mapping()
  -> 6.6 filemap_release_folio() / __remove_mapping()

5.14 shrink_active_list()
  -> 6.6 shrink_active_list()

5.14 shrink_slab()
  -> 6.6 shrink_slab()
```

差异说明：

- 6.6 默认可能启用 MGLRU。MGLRU 路径不完全经过传统
  `shrink_folio_list()`，因此部分 traditional reclaim breakdown 在启用
  MGLRU 时可能仍然偏低或为 0。
- `ADC_HERMIT_RMAP1_LAT` / `ADC_HERMIT_RMAP2_LAT` 尚未恢复，因为当前 6.6
  还没有完整恢复 5.14 的 Hermit vaddr/vpage reclaim 路径。本阶段 native
  folio reclaim 先写入 `ADC_RMAP1_LAT` / `ADC_RMAP2_LAT`。
- `ADC_POLL_STORE` 仍允许为 0。DRAM backend 是同步 store；等 RDMA async
  store / poll store 迁移后再恢复。

### 3.5 backend read/write latency

文件：

- `linux-stable/mm/page_io.c`

迁移内容：

- `ADC_RDMA_READ_LAT`
- `ADC_RDMA_WRITE_LAT`
- `ADC_READ_PAGE`
- `ADC_WRITE_PAGE`
- `ADC_SWAP_OUT_DUR`
- `ADC_SWAPOUT`
- `ADC_HERMIT_SWAPOUT`

路径：

```text
swapout:
  shrink_folio_list()
  -> pageout()
  -> swap_writepage()
  -> hermit_swap_writepage()
  -> hermit_backend_store()

swapin:
  do_swap_page()
  -> hermit_issue_read()
  -> hermit_backend_load()

native swapcache read:
  swap_readpage()
  -> hermit_swap_readpage()
  -> hermit_backend_load()
```

说明：

- backend store 成功后同时递增 `ADC_SWAPOUT` 和 `ADC_HERMIT_SWAPOUT`。
- store 成功后记录 `ADC_SWAP_OUT_DUR` 和 `ADC_RDMA_WRITE_LAT`。
- backend load 成功后记录 `ADC_RDMA_READ_LAT`。
- `ADC_READ_PAGE` / `ADC_WRITE_PAGE` 写入当前 fault/reclaim context；没有
  active context 的 kswapd/global reclaim 只会更新全局 counter/latency。

### 3.6 swap readahead / prefetch

文件：

- `linux-stable/mm/swap_state.c`

迁移内容：

- `ADC_ALLOC_PAGE`
- `ADC_DEDUP_SWAPIN`
- `ADC_RD_CACHE_ASYNC`
- `ADC_PREFETCH`
- `ADC_PREFETCH_SWAPIN`
- `ADC_HIT_ON_PREFETCH`

路径：

```text
swapin_readahead()
  -> swap_cluster_readahead() or swap_vma_readahead()
       -> __read_swap_cache_async()
            -> filemap_get_folio()
            -> vma_alloc_folio()
            -> mem_cgroup_swapin_charge_folio()
       -> swap_readpage()
```

说明：

- `ADC_PREFETCH_SWAPIN` 和 `ADC_HIT_ON_PREFETCH` 在第四阶段前已经有基础
  counter，本阶段补上 breakdown。
- 如果 `page_cluster=0` 或 readahead window 为 1，`ADC_PREFETCH` 可能非常小。
- direct bypass swapcache 路径不会经过 swap readahead，因此这些字段可以为 0。

## 4. 验证方式

局部编译：

```bash
make -C hermit/linux-stable -j$(nproc) \
  mm/page_io.o mm/memory.o mm/vmscan.o mm/memcontrol.o \
  mm/swap_state.o arch/x86/mm/fault.o kernel/fork.o
```

DRAM QEMU 验证：

```bash
hermit/tools/qemu-dram/validate-qemu-dram.sh
```

第五阶段同步更新了 QEMU DRAM 验证脚本：

- 默认 `MEMHOG_MB` 从 `2304` 调整为 `1800`。在 4 GiB guest、1 GiB
  DRAM backend pool、768 MiB tmpfs 压力下仍能稳定产生 swapout / swapin，
  但不会像旧默认值那样容易把 initramfs 推入全局 OOM。
- `memhog` 支持 `MEMHOG_READY_FILE` 环境变量。进入
  `reload-on-signal` 等待态后写入 ready 文件。
- initramfs 在填充 tmpfs 前等待 `MEMHOG_STATE: ready`，避免 TCG 慢速环境下
  `dd` 与 `memhog` 首轮 fault 同时竞争内存，导致测试目标进程被 OOM kill。
- 摘要输出新增 `MEMHOG_STATE`，便于判断失败是数据路径问题还是压测时序问题。

建议分别测试：

```bash
BYPASS_SWAPCACHE=Y LAZY_POLL=N hermit/tools/qemu-dram/validate-qemu-dram.sh
BYPASS_SWAPCACHE=Y LAZY_POLL=Y hermit/tools/qemu-dram/validate-qemu-dram.sh
BYPASS_SWAPCACHE=N LAZY_POLL=N hermit/tools/qemu-dram/validate-qemu-dram.sh
```

期望结果：

- `VALIDATION: PASS`
- `MEMHOG_CHECKSUM: status=pass`
- `RSWAP_DRAM_STATS: stores > 0, loads > 0, errors = 0`
- `SWAP_VMSTAT: pswpin` 和 `pswpout` 相比 boot 增长
- `ADC_SWAP_MAJOR_DUR` / `ADC_SWAP_MINOR_DUR` 非 0
- `ADC_RDMA_READ_LAT` / `ADC_RDMA_WRITE_LAT` 非 0
- direct swapin 场景下 `ADC_POLL_LOAD` 非 0
- 触发 memcg reclaim 时 `ADC_PAGE_RECLAIM`、`ADC_PG_CHECK_REF`、
  `ADC_TRY_TO_UNMAP`、`ADC_WRITE_PAGE` 应该非 0

本地验证记录：

```bash
make -C hermit/linux-stable -j$(nproc) \
  mm/page_io.o mm/memory.o mm/vmscan.o mm/memcontrol.o \
  mm/swap_state.o arch/x86/mm/fault.o kernel/fork.o

make -C hermit/linux-stable -j$(nproc) \
  bzImage modules_prepare drivers/block/brd.ko

make -C hermit/remoteswap-6.6/client \
  BACKEND=DRAM KDIR=$PWD/hermit/linux-stable KBUILD_MODPOST_WARN=1
```

以上构建均已通过。

当前环境没有可用 KVM，因此 QEMU 回归使用 TCG：

```bash
env SKIP_BUILD=1 QEMU_ACCEL=tcg QEMU_CPU=max TIMEOUT_SEC=240 \
  GUEST_RAM_MB=4096 MEMHOG_MB=1800 TMPFS_FILL_MB=768 \
  SWAP_MB=1024 RSWAP_MEM_GB=1 BYPASS_SWAPCACHE=Y LAZY_POLL=N \
  hermit/tools/qemu-dram/validate-qemu-dram.sh
```

代表性结果：

```text
MEMHOG_CHECKSUM: status=pass
RSWAP_DRAM_STATS: before_reload stores=139159 loads=56 errors=0
RSWAP_DRAM_STATS: after_reload  stores=139159 loads=79400 errors=0
SWAP_VMSTAT: pswpin_delta=58 pswpout_delta=139159
HERMIT_DMESG: major swap duration != 0
HERMIT_DMESG: RDMA read latency != 0
HERMIT_DMESG: RDMA write latency != 0
HERMIT_DMESG: TotalSwapOut/HermitSwapOut != 0 before reload
VALIDATION: PASS
```

额外回归：

```text
BYPASS_SWAPCACHE=Y LAZY_POLL=Y: PASS
BYPASS_SWAPCACHE=N LAZY_POLL=N: PASS
```

其中 `BYPASS_SWAPCACHE=N` 场景下 reload 后 `pswpin_delta` 与 backend
`loads` 同步增长，说明关闭 direct swapin 后，native swapcache 路径也能
通过 `swap_readpage()` 从 Hermit backend 读回数据。

## 5. 仍可能为 0 的字段

以下字段为 0 不一定表示错误：

- `ADC_POLL_STORE`：DRAM backend 是同步 store，没有 RDMA async store poll。
- `ADC_HERMIT_RMAP1_LAT` / `ADC_HERMIT_RMAP2_LAT`：Hermit vaddr/vpage reclaim
  尚未完整恢复。
- `ADC_PAGE_RECLAIM`：测试只发生 swapin、没有在 fault charge 中触发 memcg
  reclaim 时会为 0。
- `ADC_SHRNK_ACTV_LST` / `ADC_SHRNK_SLAB`：没有 active-list aging 或 slab
  pressure 时会为 0。
- `ADC_PREFETCH` / `ADC_RD_CACHE_ASYNC`：关闭 readahead、direct bypass
  swapcache、或 readahead window 为 1 时可能为 0。
- `ADC_NON_SWAP_DUR`：如果验证程序只在 reset 后触发 swap fault，非 swap fault
  数量可能很少或没有。

## 6. 后续工作

后续如果要继续提高与 5.14 的可比性，需要做两件事：

- 恢复 5.14 的 Hermit vaddr/vpage directed reclaim，再启用
  `ADC_HERMIT_RMAP1_LAT`、`ADC_HERMIT_RMAP2_LAT`、`ADC_HERMIT_RECLAIM` 的完整语义。
- 迁移 RDMA backend 的 async store/load 和 poll store，再恢复
  `ADC_POLL_STORE`、`ADC_POLL_WAIT`、`ADC_POLL_ALL`、`ADC_IB_CALLBACK`。
