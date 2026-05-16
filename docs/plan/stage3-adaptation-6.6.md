# Hermit Linux 6.6 第三阶段适配说明

## 1. 文档目的

这份文档记录第三阶段已经在 `hermit/linux-stable` 上完成的修改。

从这一阶段开始，后续实现目标切换到：

- 目标内核树：`hermit/linux-stable`
- 内核版本基线：Linux 6.6 系列
- 主要迁移对象：`do_swap_page()` 的 Hermit swap-in 路径、backend 插点、
  以及 swap fault profiling

前两份 `stage1-adaptation-6.15.md` / `stage2-adaptation-6.15.md` 仍然可以作为
迁移思路参考，但本阶段实际修改不再发生在 `linux-6.15` 中。

第三阶段的目标不是一次性恢复 5.14 Hermit 的全部功能，而是先把
**demand swap-in 读路径** 做成可继续扩展的形态：

- `HMT_BPS_SCACHE` 分支具备调用 Hermit backend 的能力；
- backend 不存在或失败时能回退到 6.6 原生 `swap_readpage()`；
- `do_swap_page()` 内部恢复关键 profiling breakdown；
- 不恢复主线 6.6 已经删除的 `frontswap` 接口；
- 给后续 DRAM/RDMA backend 模块提供稳定注册接口。

## 2. 第三阶段范围

### 已完成

- 在 6.6 `do_swap_page()` 中接入 Hermit backend read path。
- 新增 Hermit backend 注册/调用抽象。
- 更新 `hermit_issue_read()` / `hermit_poll_read()`，不再依赖旧版 `frontswap`
  或错误的 `swap_iocb` 外部变量。
- 在 swap fault 主路径中恢复局部 profiling：
  - swap fault 类型标记；
  - major/minor swap fault 分类；
  - Hermit backend fault 标记；
  - swapcache lookup、page I/O、memcg charge、metadata、set PTE 等阶段耗时。
- 保持 `CONFIG_HERMIT=n` 构建路径不受影响。

### 明确未完成

- `remoteswap/client` 到新 backend API 的完整改造。
- swapout/store 路径接入 Hermit backend。
- backend invalidate / swapoff / writeback 完整语义。
- 5.14 `hermit_vma_prefetch()` 的完整迁移。
- 架构入口层的完整 page fault profiling：
  - `ADC_TRAP_TO_KERNEL`
  - `ADC_RET_TO_USER`
  - 从异常入口到返回用户态的完整 `ADC_TOTAL_PF`

当前 `ADC_TOTAL_PF` 只覆盖 `do_swap_page()` 内部的 swap fault 处理时长，
不是 5.14 中从 x86 page fault 入口开始统计的完整时长。

## 3. 迁移前后的路径变化

### 3.1 第三阶段前的 6.6 路径

第三阶段前，`linux-stable` 已经有一个初步的 Hermit bypass 分支，但实际 I/O
仍然走原生 `swap_readpage()`：

```text
page fault
  -> do_swap_page()
  -> swap_cache_get_folio(entry)
  -> miss
  -> sync_direct || HMT_BPS_SCACHE
  -> allocate folio
  -> mem_cgroup_swapin_charge_folio()
  -> swapcache_prepare(entry)
  -> folio->swap = entry
  -> swap_readpage(page, true, NULL)
  -> folio_lock_or_retry()
  -> PTE metadata / rmap / set_pte
```

这个版本只能说明“绕过 swapcache”已经接到了 6.6 原生 direct swapin 框架里，
但它还不是 5.14 Hermit 的 `issue_read/poll_read` 路径。

### 3.2 第三阶段后的 6.6 路径

第三阶段后，`HMT_BPS_SCACHE` 分支优先尝试 Hermit backend：

```text
page fault
  -> do_swap_page()
  -> swap_cache_get_folio(entry)
  -> miss
  -> HMT_BPS_SCACHE && __swap_count(entry) == 1
  -> hermit_alloc_swap_folio()
  -> memcg swapin charge
  -> swapcache_prepare(entry)
  -> hermit_direct_swap_readpage()
       |
       +-- backend ready:
       |     -> hermit_issue_read()
       |     -> hermit_backend_load()
       |     -> hermit_poll_read()
       |
       +-- backend missing / backend failed:
             -> native swap_readpage(page, true, NULL)
  -> folio uptodate check
  -> arch_swap_restore()
  -> swap_free()
  -> rmap / set_pte
```

也就是说，现在 `do_swap_page()` 已经有了真正的 Hermit backend 插点。
没有 backend 时，行为仍然等价于原生 6.6 direct swapin。

### 3.3 Lazy poll 路径

`HMT_LAZY_POLL` 开启时，Hermit 读请求可以先提交，然后延迟到 PTE 更新前再 poll：

```text
hermit_issue_read()
  -> backend load submitted
  -> do_swap_page() 继续处理部分 metadata
  -> pte_offset_map_lock()
  -> hermit_poll_pending_read()
  -> folio_test_uptodate()
  -> set_pte_at()
```

这保留了 5.14 Hermit 想要的核心思想：

- 把 I/O 提交提前；
- 把等待尽量推迟；
- 在真正需要页面内容前完成 poll。

`HMT_LAZY_POLL` 关闭时，提交后会立即 poll，然后重新进入 6.6 原生锁页和
metadata 路径。

## 4. 已完成的代码修改

## 4.1 新增 Hermit backend 抽象

文件：

- `linux-stable/include/linux/hermit_backend.h`
- `linux-stable/mm/hermit_backend.c`
- `linux-stable/mm/Makefile`

### 4.1.1 新增 backend ops

新增结构：

```c
struct hermit_backend_ops {
	int (*load)(swp_entry_t entry, struct page *page, int cpu, bool async);
	int (*store)(swp_entry_t entry, struct page *page, int cpu, bool async);
	int (*poll_load)(int cpu);
	int (*peek_load)(int cpu);
};
```

当前第三阶段只使用了：

- `load`
- `poll_load`

`store` / `peek_load` 是给后续 swapout 和 prefetch 策略预留的接口。

### 4.1.2 新增注册接口

新增接口：

```c
int hermit_register_backend(const struct hermit_backend_ops *ops);
int hermit_regsiter_backend(const struct hermit_backend_ops *ops);
void hermit_unregister_backend(const struct hermit_backend_ops *ops);
bool hermit_backend_ready(void);
```

说明：

- `hermit_register_backend()` 是正确拼写的新接口；
- `hermit_regsiter_backend()` 保留为兼容拼写错误的旧声明，内部直接调用
  `hermit_register_backend()`；
- 当前只允许注册一个 backend；
- backend 指针使用 RCU 读取，注册/注销使用 mutex 保护；
- 注销后调用 `synchronize_rcu()`，避免并发 fault 路径仍持有旧 ops。

### 4.1.3 新增调用接口

新增接口：

```c
int hermit_backend_load(swp_entry_t entry, struct page *page, int cpu,
			bool async);
int hermit_backend_store(swp_entry_t entry, struct page *page, int cpu,
			 bool async);
int hermit_backend_poll_load(int cpu);
int hermit_backend_peek_load(int cpu);
```

当没有 backend 或 backend 未实现对应函数时，返回 `-EOPNOTSUPP`。

### 4.1.4 Makefile 接入

`mm/Makefile` 中：

```make
obj-$(CONFIG_HERMIT) += hermit.o hermit_backend.o hermit_utils.o swap_stats.o
```

这样 `CONFIG_HERMIT=y` 时，backend 注册层会和 Hermit 基础模块一起编译进内核。

## 4.2 更新 `hermit_issue_read()` / `hermit_poll_read()`

文件：

- `linux-stable/mm/page_io.c`
- `linux-stable/mm/swap.h`

### 4.2.1 不再使用旧版 `swap_iocb`

6.6 中 `struct swap_iocb` 是 `mm/page_io.c` 内部给 swapfile batched I/O 使用的
plug 结构，不应该由 Hermit 自己创建一个外部 `swap_iocb` 变量传入。

因此本阶段没有添加所谓“新版 `swap_iocb`”，而是把 Hermit read helper 改为：

```c
folio->swap = entry;
ret = hermit_backend_load(entry, page, cpu, false);
folio->private = NULL;
```

原生 fallback 仍然使用：

```c
folio->swap = entry;
swap_readpage(page, true, NULL);
folio->private = NULL;
```

### 4.2.2 `hermit_issue_read()` 的新语义

当前 `hermit_issue_read()`：

- 检查 `hermit_backend_ready()`；
- 保证 folio locked / swapbacked；
- 使用 `get_cpu()` 固定提交 CPU；
- 调用 `hermit_backend_load(entry, page, cpu, false)`；
- 成功时返回 CPU id；
- 失败时返回负错误码。

返回 CPU id 的原因是后续 `hermit_poll_read()` 需要知道应该 poll 哪个 backend
queue。

### 4.2.3 `hermit_poll_read()` 的新语义

当前 `hermit_poll_read()`：

- 记录 `ADC_POLL_LOAD`；
- 调用 `hermit_backend_poll_load(cpu)`；
- 如果调用方要求 unlock，并且 page 仍处于 locked 状态，则 `unlock_page(page)`；
- 如果 backend callback 已经提前 unlock，不会二次 unlock。

这个处理兼容两种 backend 行为：

- callback 自己 mark uptodate + unlock；
- poll 阶段完成 mark uptodate，调用方统一 unlock。

### 4.2.4 `mm/swap.h` 中声明 Hermit read helper

为了让 `mm/memory.c` 能调用 `hermit_issue_read()` 和 `hermit_poll_read()`，
在 `mm/swap.h` 中增加了 `CONFIG_HERMIT` 条件声明。

这样这些 helper 只存在于 Hermit 构建路径，不污染普通内核构建。

## 4.3 修改 `do_swap_page()` 主路径

文件：

- `linux-stable/mm/memory.c`

### 4.3.1 新增 profiling helper

新增 helper：

```c
hermit_pf_section_start()
hermit_pf_section_end()
hermit_finish_swap_fault_profile()
```

设计目的：

- 避免在 `do_swap_page()` 的多个 exit label 上重复写计时逻辑；
- 每个阶段用一个 `bool active` 防止重复 end；
- `pf_breakdown == NULL` 时自动成为空操作；
- `ADC_PROFILE_PF_BREAKDOWN` 未开启时不分配 stack array。

### 4.3.2 memcg charge 统计包装

新增：

```c
hermit_mem_cgroup_swapin_charge_folio()
```

它只是在原生：

```c
mem_cgroup_swapin_charge_folio()
```

外面包了一层 `ADC_CGROUP_ACCOUNT` 计时。

这样不会改变 memcg charge 语义，只恢复 profiling 视角。

### 4.3.3 allocation 统计

`hermit_alloc_swap_folio()` 改为接收 `pf_breakdown`：

```c
static struct folio *hermit_alloc_swap_folio(struct vm_fault *vmf,
					     uint64_t *pf_breakdown)
```

它现在统计：

- `ADC_ALLOC_PAGE`
- `ADC_CGROUP_ACCOUNT`

这个 helper 主要用于 Hermit bypass 分支中已经提前 charge 的 folio。

### 4.3.4 新增 `hermit_direct_swap_readpage()`

新增 helper：

```c
hermit_direct_swap_readpage(page, entry, hermit_bypass,
			    &hermit_read_cpu, &adc_pf_bits,
			    pf_breakdown);
```

行为：

1. 如果 `hermit_bypass == true` 且 backend ready：
   - 调用 `hermit_issue_read()`；
   - 成功则设置 `ADC_PF_HERMIT_BIT`；
   - 如果未开启 `HMT_LAZY_POLL`，立即 `hermit_poll_read()`；
   - 如果开启 `HMT_LAZY_POLL`，保留 `hermit_read_cpu`，后续再 poll。
2. 如果 backend 不存在或提交失败：
   - 增加 `ADC_OPTIM_FAILED`；
   - 回退到原生 `swap_readpage(page, true, NULL)`。

因此当前代码不会因为 backend 尚未实现而破坏 swap-in。

### 4.3.5 新增 `hermit_poll_pending_read()`

新增 helper：

```c
hermit_poll_pending_read(&hermit_read_cpu, page, unlock, pf_breakdown);
```

它用于所有需要收尾 pending read 的位置：

- 正常 PTE 更新前；
- `out:`；
- `out_page:`；
- `out_release:`。

这样可以减少出错路径遗留未完成 I/O 的风险。

### 4.3.6 swap fault 类型标记

在 `get_swap_device(entry)` 成功后设置：

```c
set_adc_pf_bits(&adc_pf_bits, ADC_PF_SWAP_BIT);
```

在需要真实读入 swap page 的 major fault 路径中设置：

```c
set_adc_pf_bits(&adc_pf_bits, ADC_PF_MAJOR_BIT);
```

在 Hermit backend 成功接管 read 时设置：

```c
set_adc_pf_bits(..., ADC_PF_HERMIT_BIT);
```

这里实际调用发生在 `hermit_direct_swap_readpage()` 内部，传入的是
`do_swap_page()` 中 `adc_pf_bits` 的地址。

最终在 exit path 调用：

```c
record_adc_pf_time(adc_pf_bits, pf_breakdown[ADC_TOTAL_PF]);
parse_adc_pf_breakdown(adc_pf_bits, pf_breakdown);
```

注意：当前只会对真正进入 swap entry 的 fault 记录 breakdown。migration entry、
device private entry、hwpoison marker 等非普通 swap entry 不会进入
`parse_adc_pf_breakdown()`。

### 4.3.7 `ADC_PAGE_IO` 的覆盖范围

当前 `ADC_PAGE_IO` 在 swapcache miss 后开始：

```text
swapcache miss
  -> start ADC_PAGE_IO
  -> native swapin_readahead 或 direct swapin
  -> native path: folio_lock_or_retry 后结束
  -> Hermit lazy path: pending read poll 完成后结束
```

这和 5.14 的语义接近，但不是完全相同：

- 5.14 里 page I/O 通常围绕 Hermit issue/poll 更细；
- 6.6 当前为了不大规模 fork 原生路径，把 native fallback 和 Hermit backend 都放在
  同一个 `ADC_PAGE_IO` 桶里。

后续如果需要更精细，可以再拆分：

- `ADC_READ_PAGE`
- `ADC_POLL_LOAD`
- `ADC_RD_CACHE_ASYNC`

### 4.3.8 `ADC_UPD_METADATA` 的覆盖范围

当前从 folio 已经可用后开始，覆盖：

- swapcache 一致性检查；
- KSM copy 检查；
- throttle；
- PTE lock；
- uptodate 检查；
- exclusive 判断；
- `arch_swap_restore()`；
- `swap_free()`；
- mm counter 更新；
- rmap 添加。

在真正 `set_pte_at()` 前结束。

### 4.3.9 `ADC_SETPTE` 的覆盖范围

当前覆盖：

- `set_pte_at()`
- `arch_do_swap_page()`
- `folio_unlock()`
- swapcache folio cleanup

这个范围比 5.14 略宽，因为 6.6 原生 folio 路径把一些收尾动作放在一起。
这样做的优先级是保持 6.6 原生路径结构稳定。

## 5. Fallback 与安全条件

### 5.1 不进入 Hermit bypass 的情况

`hermit_should_bypass_swapcache()` 仍然要求：

- `HMT_BPS_SCACHE` 开启；
- `CONFIG_ZSWAP` 未启用；
- VMA 没有 armed userfaultfd；
- `__swap_count(entry) == 1`。

不满足时直接走原生 `swapin_readahead()` 或原生 direct swapin。

### 5.2 backend 不存在

如果没有模块调用 `hermit_register_backend()`：

```text
hermit_backend_ready() == false
```

则 `hermit_direct_swap_readpage()` 直接回退到：

```c
swap_readpage(page, true, NULL);
```

### 5.3 backend 提交失败

如果 `hermit_issue_read()` 返回负错误码：

- 计数 `ADC_OPTIM_FAILED`；
- 回退到原生 `swap_readpage()`；
- 不把 fault 计为 Hermit backend fault。

### 5.4 `CONFIG_HERMIT=n`

`hermit_issue_read()` / `hermit_poll_read()` 在 `page_io.c` 中被
`#ifdef CONFIG_HERMIT` 包裹。

普通内核构建不会看到：

- `hermit_backend_ready()`
- `ADC_POLL_LOAD`
- `pf_cycles_start()`
- `pf_cycles_end()`

因此不会污染非 Hermit 构建。

## 6. backend 实现契约

后续 DRAM/RDMA backend 迁入时，需要实现并注册：

```c
static const struct hermit_backend_ops ops = {
	.load = ...,
	.store = ...,
	.poll_load = ...,
	.peek_load = ...,
};

hermit_register_backend(&ops);
```

### 6.1 `load()` 约定

`load(entry, page, cpu, async)` 应该：

- 根据 `entry` 定位远端或 DRAM backend 中的 swap 数据；
- 把数据读入 `page`；
- 成功返回 `0`；
- 失败返回负错误码；
- 完成后必须保证页面最终会变成 uptodate。

对 RDMA backend 来说，通常是：

```text
load()
  -> post RDMA read
  -> completion callback folio_mark_uptodate(page_folio(page))
```

对 DRAM mock backend 来说，可以同步 memcpy，然后直接
`folio_mark_uptodate(page_folio(page))`。

### 6.2 `poll_load()` 约定

`poll_load(cpu)` 应该 drain 对应 CPU 的 load queue，使之前提交到该 CPU queue 的
读请求完成。

如果 backend 使用 lazy poll：

- callback 可以只 mark uptodate，不 unlock；
- `do_swap_page()` 后续会正常 `folio_unlock(folio)`。

如果 backend 不使用 lazy poll：

- callback 可以 unlock；
- `hermit_poll_read(..., unlock=true)` 也会检查 PageLocked，避免重复 unlock。

### 6.3 `async` 参数当前含义

当前 demand swap-in 调用：

```c
hermit_backend_load(entry, page, cpu, false);
```

这里的 `false` 表示这是 demand swap-in 的主读请求，不是预取请求。
是否真正异步完成，由 backend 自己决定。

后续迁移 prefetch 时，可以用 `async=true` 表示 prefetch / background load。

## 7. 当前 profiling 能力和限制

### 已恢复

可以通过现有 `get_swap_stats` / `reset_swap_stats` 路径看到：

- `ADC_ONDEMAND_SWAPIN`
- `ADC_OPTIM_FAILED`
- major/minor swap fault breakdown
- Hermit backend fault 的 `ADC_PF_HERMIT_BIT` 分类基础
- `ADC_POLL_LOAD`

### 未完全恢复

当前没有恢复 x86 fault entry 层的完整 wrapper，因此以下字段不等价于 5.14：

- `ADC_TRAP_TO_KERNEL`
- `ADC_RET_TO_USER`
- 完整异常入口到返回用户态的 `ADC_TOTAL_PF`

当前 `ADC_TOTAL_PF` 更准确地说是：

```text
do_swap_page() 内部 swap fault 处理耗时
```

而不是完整 page fault 耗时。

如果后续要恢复 5.14 级别的完整 profiling，需要继续改：

- `arch/x86/mm/fault.c`
- `handle_mm_fault()`
- `__handle_mm_fault()`
- `handle_pte_fault()`

这个改动面明显更大，第三阶段暂时没有做。

## 8. 编译验证

本阶段做过两组构建验证。

### 8.1 `CONFIG_HERMIT=y`

使用 out-of-tree build 目录：

```bash
make O=/tmp/hermit-linux-stable-build x86_64_defconfig
scripts/config --file /tmp/hermit-linux-stable-build/.config \
	-e MEMCG -e SWAP -e HERMIT
make O=/tmp/hermit-linux-stable-build olddefconfig
```

验证对象：

```bash
make O=/tmp/hermit-linux-stable-build \
	mm/memory.o \
	mm/page_io.o \
	mm/hermit_backend.o \
	mm/hermit.o \
	mm/hermit_utils.o \
	mm/swap_stats.o
```

补充验证：

```bash
make O=/tmp/hermit-linux-stable-build \
	mm/memcontrol.o \
	mm/vmscan.o \
	mm/swap_state.o \
	mm/swap_stats.o \
	mm/hermit_utils.o \
	extended_syscalls/extended_syscalls.o
```

最终验证：

```bash
make O=/tmp/hermit-linux-stable-build mm/built-in.a
```

结果：通过。

### 8.2 `CONFIG_HERMIT=n`

使用普通 x86_64 defconfig：

```bash
make O=/tmp/hermit-linux-stable-nohermit-build x86_64_defconfig
make O=/tmp/hermit-linux-stable-nohermit-build mm/memory.o mm/page_io.o
```

结果：通过。

这说明新增的 `page_io.c` helper 已经正确包在 `CONFIG_HERMIT` 条件内，普通内核构建
不会因 Hermit 私有符号失败。

## 9. 当前状态总结

第三阶段完成后，6.6 Hermit 的 demand swap-in 路径已经进入以下状态：

```text
HMT_BPS_SCACHE on
  + backend registered
      -> do_swap_page() 使用 hermit_issue_read()/poll_read()

HMT_BPS_SCACHE on
  + backend missing / failed
      -> do_swap_page() 回退原生 swap_readpage()

HMT_BPS_SCACHE off
      -> 6.6 原生 swapin_readahead/direct swapin
```

这意味着当前代码已经具备继续迁移 backend 的基本条件。

下一阶段最合理的工作是：

1. 把 `remoteswap/client` 从旧 `frontswap_ops` 改成 `hermit_backend_ops`。
2. 先接 DRAM backend，验证 `load()` / `poll_load()` 和 `HMT_LAZY_POLL`。
3. 再接 RDMA backend。
4. 最后处理 swapout/store、invalidate、prefetch 和完整 fault-entry profiling。
