# Hermit Linux 6.15 第一阶段适配说明

## 1. 文档目的

这份文档只记录 **第一阶段已经实际完成的 6.15 适配内容**，不展开完整的
Hermit 功能恢复方案。

这里的“第一阶段”定义为：

- 先把 `hermit/linux-6.15` 中已经移入的 Hermit 基础代码接到 6.15 当前
  的 MM / memcg / folio 接口上；
- 优先解决 **编译断点、接口签名变化、基础统计和 memcg 异步回收入口**；
- 暂时 **不** 恢复 5.14 Hermit 的完整 swap-in / frontswap / remoteswap
  快路径语义。

换句话说，这一阶段的目标是：

- 让 6.15 版本的 Hermit 基础骨架更接近“可继续开发”的状态；
- 给第二阶段的真正功能迁移留出稳定入口；
- 避免在 6.15 上直接强行复刻 5.14 的老接口。

## 2. 第一阶段边界

### 已覆盖

- Hermit 基础头文件与 MM 文件在 6.15 上的编译兼容
- `memcg` 侧的初始化、清理、充电统计、异步回收触发点
- `try_to_free_mem_cgroup_pages()` 旧 Hermit 入口到 6.15 原生 reclaim 的包装
- `page` 风格接口向 `folio` 风格接口的最小替换
- `swap_stats` 基础统计代码的编译修复

### 明确未覆盖

- `frontswap` 语义恢复
- `remoteswap/client` 到 6.15 的后端重构
- `do_swap_page()` / `read_swap_cache_async()` 路径上的 Hermit swap-in 优化
- 5.14 中更深的 profiling / batching / speculative I/O 语义恢复
- large folio / THP / MGLRU 的专门适配

## 3. 已完成的修改

## 3.1 `mm/swap_stats.c`

### 修改内容

- 删除了重复定义的：
  - `struct adc_time_stat adc_time_stats[NUM_ADC_TIME_STAT_TYPE];`

### 原因

移植过程中该全局数组被定义了两次，会直接导致重复符号或编译失败。

### 第一阶段效果

- 保留 Hermit/ADC 统计结构；
- 消除最直接的链接/编译错误。

## 3.2 `mm/memcontrol.c`

这是第一阶段最关键的适配点之一，因为 5.14 Hermit 很多逻辑都是从
`memcg charge -> reclaim -> async reclaim` 这条线上接入的，而 6.15 这条线
已经发生了明显变化。

### 3.2.1 Hermit 头文件按 `CONFIG_HERMIT` 包裹

新增了条件包含：

- `#include <linux/hermit_utils.h>`
- `#include <linux/hermit.h>`
- `#include <linux/swap_stats.h>`

### 原因

避免在 `CONFIG_HERMIT=n` 时把 Hermit 私有接口硬塞进普通 6.15 构建路径。

### 3.2.2 `reclaim_high()` 改回 6.15 原生 memcg reclaim 调用

旧的半迁移代码还在调用一个已经不匹配 6.15 的 Hermit 风格参数列表，里面引用了：

- `mem_over_limit`
- `may_swap`
- `adc_pf_bits`
- `pf_breakdown`

这些变量/参数在 6.15 当前版本里并不成立。

现在改为直接走：

```c
try_to_free_mem_cgroup_pages(memcg, nr_pages, gfp_mask,
			     MEMCG_RECLAIM_MAY_SWAP, NULL);
```

### 原因

`reclaim_high()` 本身是 6.15 原生 memcg 逻辑的一部分。第一阶段不应在这里强塞
5.14 Hermit 的 profiling 版 reclaim 签名，而应先确保原生 reclaim 语义正确。

### 3.2.3 在 `try_charge_memcg()` 成功路径恢复 Hermit 统计与异步回收触发

在以下成功路径中，增加了 `CONFIG_HERMIT` 下的逻辑：

- `consume_stock(...)` 成功后
- `force:` 路径
- `done_restock:` 路径

新增行为：

- `atomic64_add(nr_pages, &memcg->total_pg_charge);`
- `hmt_async_reclaim(current->mm, memcg);`

### 原因

Hermit 的异步回收线程是挂在 memcg 上运行的。第一阶段不重做全部 pageout 路径，
但至少要在 **稳定的 charge 成功点** 上恢复：

- 充电统计；
- 异步回收线程的启动入口。

这样第二阶段继续恢复 Hermit reclaim 策略时，不需要再回头改 charge 主路径。

### 3.2.4 在 memcg 生命周期里补上 Hermit 初始化/清理

在 `mem_cgroup_alloc()` 中加入：

- `hermit_init_memcg(memcg);`

在 `mem_cgroup_free()` 中加入：

- `hermit_cleanup_memcg(memcg);`

都放在 `CONFIG_HERMIT` 条件下。

### 原因

6.15 的 `struct mem_cgroup` 已经被扩展出 Hermit 私有状态：

- `total_pg_charge`
- `total_pg_uncharge`
- `hmt_sc`
- `sthds`

如果不在 alloc/free 时显式初始化与清理，后续异步 reclaim 线程和统计字段会失配。

### 3.2.5 恢复 `hermit_mem_cgroup_swapin_charge_page()`，但改成兼容包装层

5.14 版本使用的是：

- `__mem_cgroup_charge_profiling(page, memcg, gfp, ...)`

这个 helper 在 6.15 中已经不存在。

第一阶段现在把它改成：

- 用 `page_folio(page)` 取得 `folio`
- 通过 `get_mem_cgroup_from_mm(mm)` 找到目标 memcg
- 调用 6.15 当前文件内已有的 `charge_memcg(folio, memcg, gfp)`

同时保留：

- `ADC_CGROUP_ACCOUNT` 的时间统计

但不再尝试恢复 5.14 的 page-fault profiling 位图更新。

### 原因

这个函数在当前 6.15 树中几乎没有实际调用点，但只要 `CONFIG_HERMIT=y`，
函数体就必须能编过。

第一阶段把它保留为“兼容壳”最合适：

- 语义上仍是“给 swapin 新页做 memcg charge”；
- 但内部实现用 6.15 原生 folio charge 路径；
- 把更细的 fault profiling 恢复推迟到下一阶段。

### 3.2.6 修正 `hermit_mem_cgroup_swapout()` 里的统计接口

5.14 中这里会调用：

- `mem_cgroup_charge_statistics(...)`

该 helper 在 6.15 中不再保留同样的接口。

第一阶段改成：

```c
__count_memcg_events(memcg, PGPGOUT, 1);
```

### 原因

`hermit_mem_cgroup_swapout()` 的第一阶段目标不是完整恢复 5.14 的 memcg 记账细节，
而是先把最核心的 swapout 事件统计保留下来，并且保证函数可编译。

### 3.2.7 Hermit 私有函数按 `CONFIG_HERMIT` 包裹

已经把以下函数限制在 Hermit 打开时编译：

- `hermit_mem_cgroup_swapin_charge_page(...)`
- `hermit_mem_cgroup_swapout(...)`

### 原因

避免在普通 6.15 配置里暴露不必要的私有符号。

## 3.3 `include/linux/memcontrol.h`

### 修改内容

补充了 `CONFIG_HERMIT` 下的兼容声明：

- `hermit_mem_cgroup_swapin_charge_page(...)`

### 原因

6.15 打开 `WERROR` 时，全局函数若没有前置声明会直接报
`missing-prototypes`。

第一阶段既然保留这个函数，就应当给出显式原型。

## 3.4 `include/linux/swap.h`

### 修改内容

增加了 `CONFIG_HERMIT` 下的声明：

- `hermit_try_to_free_mem_cgroup_pages(...)`
- `hermit_mem_cgroup_swapout(...)`

并在 `!CONFIG_MEMCG || !CONFIG_SWAP` 的分支里补了
`hermit_mem_cgroup_swapout()` 的空实现。

### 原因

5.14 Hermit 在 reclaim 和 batched swapout 上有自己的外部入口；
6.15 当前树里保留了调用点，但没有对应声明，会导致编译失败。

## 3.5 `mm/vmscan.c`

### 修改内容

新增了第一阶段兼容包装：

- `hermit_try_to_free_mem_cgroup_pages(...)`

其实现策略是：

1. 保留 5.14 Hermit 的旧函数名和旧参数表；
2. 把 `bool may_swap` 转成 6.15 的 `MEMCG_RECLAIM_MAY_SWAP`；
3. 直接调用 6.15 原生：

```c
try_to_free_mem_cgroup_pages(memcg, nr_pages, gfp_mask,
			     reclaim_options, NULL);
```

4. 用 `adc_pf_breakdown_end(..., ADC_PAGE_RECLAIM, ...)` 记录这一层的
   reclaim 时长。

### 原因

5.14 版本的 Hermit reclaim 入口带有：

- 线程上下文 `cthd`
- `adc_pf_bits`
- `pf_breakdown`

而 6.15 原生 memcg reclaim 已经改成：

- `unsigned int reclaim_options`
- `int *swappiness`

第一阶段不重写 6.15 的整个 reclaim 实现，而是先做一个 **旧入口 -> 新入口**
的翻译层。这样：

- `mm/hermit_utils.c` 里旧调用点不用立刻重写；
- reclaim 真正执行时仍然走 6.15 upstream 的主逻辑；
- 后续若要恢复更细粒度 profiling，再在这个包装层和 pageout 细节里继续加。

## 3.6 `mm/hermit.c`

这个文件的主要问题是：5.14 代码仍以 `struct page` 和旧 rmap/page-idle API
为中心，而 6.15 已经全面转向 `folio` 风格接口。

### 3.6.1 `hmt_update_rft_dist()` 迁移到 `folio_memcg()`

旧代码：

- `page_memcg(page)`

新代码：

- `folio_memcg(page_folio(page))`

### 原因

`page_memcg()` 已不再是 6.15 推荐接口。

### 3.6.2 `hermit_page_referenced()` 迁移到 folio 系列 helper

替换内容包括：

- `total_mapcount(page)` -> `folio_mapcount(page_folio(page))`
- `page_referenced(page, ...)` -> `folio_referenced(page_folio(page), ...)`
- `clear_page_idle(page)` -> `folio_clear_idle(page_folio(page))`
- `test_and_clear_page_young(page)` -> `folio_test_clear_young(page_folio(page))`

同时把两个旧时代判断一并改掉：

- `page_rmapping(page)` -> `folio_raw_mapping(page_folio(page))`
- `PageKsm(page)` -> `folio_test_ksm(page_folio(page))`

### 原因

这是 5.14 -> 6.15 MM 迁移里最典型的一类改动。

第一阶段的目标不是重写 Hermit 的 vaddr reclaim 算法，而是让这一层 helper
能够继续在 6.15 上编译并保持大体语义。

### 3.6.3 `hermit_init()` 改成 `static`

### 原因

它只是当前文件内的 `__initcall()` 入口，不需要暴露为全局符号。
在 6.15 默认 `WERROR` 下，这能消除 `missing-prototypes` 编译错误。

## 3.7 `mm/hermit_utils.c`

### 修改内容

增加：

- `#include <linux/swap_stats.h>`

### 原因

`hermit_reclaim_high()` 中直接使用了：

- `NUM_ADC_PF_BREAKDOWN_TYPE`
- `ADC_TOTAL_PF`
- `ADC_HMT_OUT_SPF`
- `adc_pf_breakdown_end(...)`
- `accum_adc_pf_breakdown(...)`

如果不显式包含 `swap_stats.h`，在 6.15 + `WERROR` 环境下会直接因隐式声明和
未定义枚举而失败。

## 4. 第一阶段后的内存路径形态

这一阶段完成后，**真正接通的主要是 memcg charge -> Hermit async reclaim ->
native reclaim** 这条链路。

示意如下：

```text
匿名页分配 / memcg charge 成功
        |
        v
try_charge_memcg()
        |
        +--> total_pg_charge++
        |
        +--> hmt_async_reclaim(current->mm, memcg)
                 |
                 v
          hermit_reclaim_high()
                 |
                 v
          hermit_try_to_free_mem_cgroup_pages(...)
                 |
                 v
          6.15 native try_to_free_mem_cgroup_pages(...)
                 |
                 v
          shrink_lruvec() / shrink_folio_list()
```

这里要特别注意：

- 第一阶段接通的是 **Hermit 的回收入口**；
- 还 **没有** 恢复 5.14 Hermit 的 frontswap/remoteswap page-in/page-out 快路径。

## 5. 第一阶段后仍然保留的缺口

下面这些内容属于第二阶段及之后的工作，不应误判为本阶段已经完成。

### 5.1 swap-in 快路径尚未迁移

包括但不限于：

- `mm/memory.c::do_swap_page()`
- `mm/swap_state.c::read_swap_cache_async()`
- `swapin_readahead()`
- bypass swapcache
- speculative prefetch

当前 6.15 仍然主要走 upstream 原生 swapin 流程。

### 5.2 frontswap 依赖尚未重构

5.14 Hermit 与 `remoteswap/client` 深度依赖 frontswap；
而 6.15 已经没有这套旧接口。

因此第二阶段需要做的是：

- 不再尝试简单“找回 frontswap”；
- 而是把 remoteswap client 改到新的 Hermit backend 抽象上。

### 5.3 reclaim 深层语义尚未恢复

虽然第一阶段已经恢复了 Hermit reclaim 的入口函数名，但以下能力仍未迁回：

- 5.14 的 profiling 版 `do_try_to_free_pages_profiling()`
- 更细的 `pf_breakdown` 子项统计
- batch swapout
- batch TLB
- 更完整的 vaddr-directed reclaim

### 5.4 large folio / THP / MGLRU 仍未专门处理

第一阶段的兼容修改主要假设：

- order-0 匿名页
- 普通 memcg reclaim 路径

而 6.15 中更复杂的：

- large folio
- THP
- MGLRU

目前仍主要依赖 upstream 原生行为，没有恢复 Hermit 的专门优化逻辑。

### 5.5 用户态验证脚本仍偏 5.14/frontswap

现有验证脚本和工具里，仍有一部分逻辑默认：

- 使用 `linux-5.14-rc5`
- 读取 frontswap/debugfs 统计

这些脚本需要在后续阶段再统一切到 6.15 的新路径。

## 6. 构建验证

本阶段完成后，已执行如下最小编译验证：

```bash
make -C hermit/linux-6.15 -j4 \
  mm/memcontrol.o mm/vmscan.o mm/hermit.o \
  mm/hermit_utils.o mm/swap_stats.o
```

结果：

- 以上对象文件已成功编译通过。

### 这次验证说明了什么

- 第一阶段新增/修改的兼容层，在对象级别上已经自洽；
- `CONFIG_HERMIT=y` 下至少关键 MM 相关文件可以通过 6.15 当前编译器检查；
- 头文件声明、folio API 替换、基础 memcg reclaim 包装已闭环。

### 这次验证还不能说明什么

- 还不能证明完整内核 `bzImage` / `modules` 已全部通过；
- 还不能证明 QEMU 启动和运行时语义正确；
- 还不能证明 Hermit 的远程内存功能已经恢复。

## 7. 对这一阶段的结论

第一阶段适配的本质，是把 5.14 Hermit 留下来的几个核心“接口断层”补齐：

- `page` 风格 MM helper -> `folio` 风格 helper
- 旧 Hermit reclaim 签名 -> 6.15 native memcg reclaim 签名
- 已删除的 5.14 memcg 统计 helper -> 6.15 仍存在的事件统计接口
- memcg 生命周期与 charge 成功点 -> Hermit 异步回收入口

完成这些之后，6.15 版 Hermit 已经具备继续做第二阶段迁移的基础：

- 可以继续改 `do_swap_page()` 和 `swap_state.c`
- 可以开始重新设计 6.15 的 Hermit backend
- 可以把 remoteswap/frontswap 依赖逐步拆掉

但此时它仍然只是一个 **编译与结构适配完成的第一阶段版本**，而不是功能完整的
6.15 Hermit。
