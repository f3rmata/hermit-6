# Hermit Linux 6.15 第二阶段适配说明

## 1. 文档目的

这份文档记录第二阶段已经完成的 6.15 适配内容。

第二阶段的目标，不再是单纯把 Hermit 代码“编过去”，而是开始把
**swap-in 主路径** 上的 Hermit 语义重新接到 6.15 原生 MM 流程里。

这一阶段的核心原则是：

- 尽量复用 6.15 现有 `folio` / `swap_read_folio()` / `swapin_readahead()`
  路径；
- 不恢复 5.14 已消失的 `frontswap` 接口；
- 先恢复 **用户可见的 swap-in 行为开关和基础统计**；
- 给下一阶段的 backend 重构保留稳定插点。

## 2. 第二阶段范围

### 已完成

- 让 `HMT_BPS_SCACHE` 在 6.15 上重新具备真实行为
- 把 swap-in / readahead / cache-hit 的 ADC 统计重新接回 6.15
- 保持 6.15 原生 `do_swap_page()` / `swapin_readahead()` / `swap_read_folio()`
  为主执行路径

### 明确未完成

- `frontswap_load_async()` / `frontswap_poll_load()` 级别的异步读
- `hermit_issue_read()` / `hermit_poll_read()` 那套旧读路径
- `remoteswap/client` 的 6.15 backend 重构
- 5.14 的 `PG_prefetch` 私有页标志恢复
- 旧版 `hermit_vma_prefetch()` 逻辑完整迁回

## 3. 为什么第二阶段要这么切

5.14 Hermit 的 swap-in 优化，核心依赖三类老机制：

1. `frontswap`
2. `PG_prefetch`
3. 自己的一套 `issue_read/poll_read` 异步读协议

而 6.15 当前树的事实是：

- swap-in 主流程已经 folio 化；
- `frontswap` 已不在主线 6.15 中；
- 原来的 `PagePrefetch` 私有标志没有随 6.15 Hermit 树一起迁入；
- 当前 6.15 已经天然提供：
  - `swap_cache_get_folio()`
  - `folio_set_readahead()`
  - `folio_test_clear_readahead()`
  - direct swapin path
  - `swap_read_folio()`

所以第二阶段不应该硬搬 5.14 的读路径，而应该先把 Hermit 的两个最重要元素
重新恢复：

- **bypass swapcache**
- **swapin / prefetch / cache-hit 统计**

## 4. 已完成的代码修改

## 4.1 `mm/swap_state.c` 接回 Hermit swap-in 统计

文件：
[mm/swap_state.c](/home/fermata/Development/repos/fastswap/hermit/linux-6.15/mm/swap_state.c:1)

### 4.1.1 增加 Hermit 统计头文件

新增条件包含：

- `#include <linux/hermit.h>`
- `#include <linux/swap_stats.h>`

### 原因

第二阶段开始在 native swap cache / readahead 路径中直接更新 Hermit 计数器，
因此需要能访问：

- `hmt_ctl_flag(...)`
- `adc_profile_counter_inc(...)`
- `ADC_*` 统计枚举

### 4.1.2 用 native `PageReadahead` 语义替代 5.14 的 `PG_prefetch`

在 5.14 里，Hermit 使用私有页标志：

- `set_page_prefetch(page)`
- `test_and_clear_page_prefetch(page)`

来区分“普通 swap cache 命中”和“命中的是曾经预取过的页”。

6.15 当前树里没有这一位，但 upstream 自带：

- `folio_set_readahead(folio)`
- `folio_test_clear_readahead(folio)`

因此第二阶段在：

- [swap_cache_get_folio()](/home/fermata/Development/repos/fastswap/hermit/linux-6.15/mm/swap_state.c)

中复用了 `folio_test_clear_readahead(folio)` 的结果。

当命中的 folio 带有 readahead 标记时，现在会额外累加：

- `ADC_HIT_ON_PREFETCH`

### 原因

这不是 5.14 语义的 100% 等价恢复，但它是 6.15 当前最自然、最安全的近似：

- upstream 本来就用这个标记表达“这页是 readahead 读入的”；
- Hermit 想统计的也是“fault 命中了一个预先读入的页”；
- 不需要新增 6.15 的 page flag 位。

### 4.1.3 在 native readahead 路径中恢复 `ADC_PREFETCH_SWAPIN`

在以下两个函数里：

- [swap_cluster_readahead()](/home/fermata/Development/repos/fastswap/hermit/linux-6.15/mm/swap_state.c:600)
- [swap_vma_readahead()](/home/fermata/Development/repos/fastswap/hermit/linux-6.15/mm/swap_state.c:746)

当 readahead 读入的不是 fault 目标页，而是周围扩展页时，若新分配并成功加入
swap cache，就会：

1. `folio_set_readahead(folio)`
2. `count_vm_event(SWAP_RA)`
3. `adc_profile_counter_inc(ADC_PREFETCH_SWAPIN)`

### 原因

5.14 Hermit 的 `ADC_PREFETCH_SWAPIN` 统计的是“通过预取路径提前读入的 swapin 页”。

在第二阶段里，我们把这个统计重新绑定到 6.15 native readahead 产生的非目标页上。

这样一来：

- 用户态 `get_swap_stats` 能重新看到 prefetched swapin 数量；
- 统计含义与当前内核真实行为一致；
- 不需要先恢复 5.14 的异步 prefetch 队列。

## 4.2 `mm/memory.c` 恢复 Hermit 的 bypass-swapcache 语义

文件：
[mm/memory.c](/home/fermata/Development/repos/fastswap/hermit/linux-6.15/mm/memory.c:4206)

### 4.2.1 增加 Hermit 相关头文件

新增条件包含：

- `#include <linux/hermit.h>`
- `#include <linux/swap_stats.h>`

### 4.2.2 新增 `hermit_should_bypass_swapcache()`

新增 helper：

- [hermit_should_bypass_swapcache()](/home/fermata/Development/repos/fastswap/hermit/linux-6.15/mm/memory.c:4228)

它会在以下条件同时满足时返回 true：

- `HMT_BPS_SCACHE` 打开
- `zswap` 未启用
- 不在 `userfaultfd` 特殊语义下
- `__swap_count(entry) == 1`

### 原因

5.14 Hermit 的 `bypass swapcache` 目标，是在足够确定没有共享复用价值时，
尽快把目标 swap entry 直接读到 fault 所需页里，减少中间层。

6.15 已经存在一个 upstream direct swapin 分支，但它默认只在：

- `SWP_SYNCHRONOUS_IO`
- `swap_count == 1`

时启用。

第二阶段做的事，就是把 Hermit 的 knob 映射到这个 native direct-swapin 思路上。

### 4.2.3 `HMT_BPS_SCACHE` 触发时走 order-0 direct swapin

在：

- [do_swap_page()](/home/fermata/Development/repos/fastswap/hermit/linux-6.15/mm/memory.c:4450)

中，当普通 swap cache lookup miss 后：

- 如果命中 6.15 原生 `SWP_SYNCHRONOUS_IO` 条件，仍走 upstream direct path；
- 如果命中 `HMT_BPS_SCACHE` 条件，则额外允许走 direct path；
- Hermit 这一路会使用 `__alloc_swap_folio(vmf)`，故意限制为 **order-0**。

### 原因

这一阶段不碰 large folio / THP 的额外复杂度。

Hermit 的第二阶段目标是：

- 先恢复 bypass 的基本语义；
- 同时把风险压到 order-0 fault；
- 避免在 6.15 direct path 上和 large folio / zswap / hybrid backend 混用。

### 4.2.4 Hermit bypass 失败时回退到 native `swapin_readahead()`

如果：

- `HMT_BPS_SCACHE` 条件满足，
- 但 direct path 需要的 order-0 folio 分配失败，

当前不会直接把 fault 打成 OOM，而是：

1. 记录一次 `ADC_OPTIM_FAILED`
2. 回退到原生 `swapin_readahead()`

对应位置：

- [do_swap_page()](/home/fermata/Development/repos/fastswap/hermit/linux-6.15/mm/memory.c:4578)

### 原因

这一步很重要。

如果只是“打开 Hermit knob 后强推 direct path”，那么第二阶段会把一个优化开关
变成稳定性风险点。

现在的策略是：

- 能直读就直读；
- 直读条件临时不满足，就回退 native path；
- 并把失败次数记到 `ADC_OPTIM_FAILED`。

这和第一阶段“先保住 6.15 主路径”的原则一致。

### 4.2.5 恢复 `ADC_ONDEMAND_SWAPIN`

在 major swap fault 成功取回 fault 目标 folio 后，现在会增加：

- `ADC_ONDEMAND_SWAPIN`

对应位置：

- [do_swap_page()](/home/fermata/Development/repos/fastswap/hermit/linux-6.15/mm/memory.c:4600)

### 原因

5.14 Hermit 用户态统计接口里最直接的三个数字就是：

- ondemand swapin
- prefetch swapin
- hit on prefetch

第一阶段只有 syscalls 和统计容器，没有真实生产者；
第二阶段把这三个计数的生产路径重新接回来了。

## 5. 第二阶段后的 swap-in 路径

## 5.1 6.15 原生 + 第二阶段 Hermit 兼容版

```text
swap fault
   |
   v
do_swap_page()
   |
   +--> swap_cache_get_folio()
   |       |
   |       +--> 命中普通页: native minor fault
   |       |
   |       +--> 命中 readahead 页:
   |              folio_test_clear_readahead()
   |              ADC_HIT_ON_PREFETCH++
   |
   +--> miss:
           |
           +--> 若 HMT_BPS_SCACHE && swap_count == 1 && zswap 关闭
           |       |
           |       v
           |   order-0 direct swapin
           |       |
           |       +--> 成功: ADC_ONDEMAND_SWAPIN++
           |       |
           |       +--> 分配失败: ADC_OPTIM_FAILED++
           |                      fallback 到 swapin_readahead()
           |
           +--> 否则:
                   |
                   v
              swapin_readahead()
                   |
                   +--> target folio: major fault, ADC_ONDEMAND_SWAPIN++
                   |
                   +--> neighbor folios:
                          folio_set_readahead()
                          ADC_PREFETCH_SWAPIN++
```

## 5.2 与 5.14 Hermit 原版的差异

### 这一阶段已经恢复的

- bypass swapcache 这个控制点
- ondemand/prefetch/hit-on-prefetch 统计三元组
- VMA/cluster readahead 触发的“预读页”区分

### 这一阶段还没有恢复的

- `hermit_issue_read()`
- `hermit_poll_read()`
- speculative read
- `hermit_vma_prefetch()` 的队列化异步逻辑
- `frontswap_load_async()` / `poll_load()`
- 后端直达 RDMA/DRAM client

所以目前的第二阶段版本还不是：

```text
Hermit 自定义读路径
```

而是：

```text
Hermit 控制与统计 + 6.15 native swapin 读路径
```

## 6. 这阶段为什么有价值

虽然还没有恢复到 5.14 原版的完整异步读栈，但第二阶段已经把最关键的实验入口接回来了：

### 6.1 `HMT_BPS_SCACHE` 不再只是摆设

现在打开这个 knob，fault 路径的行为真的会变化。

### 6.2 用户态 swap stats 重新有了真实来源

`get_swap_stats()` 现在返回的：

- `ADC_ONDEMAND_SWAPIN`
- `ADC_PREFETCH_SWAPIN`
- `ADC_HIT_ON_PREFETCH`

已经重新绑定到 6.15 实际 swap fault / readahead 行为，而不再只是空壳计数器。

### 6.3 第三阶段的 backend 改造有了稳定插点

之后若要继续恢复：

- async load
- speculative load
- remoteswap backend

最自然的继续切入点，就是：

- `do_swap_page()` 的 direct path
- `swapin_readahead()` 的 target / neighbor folio 区分
- `swap_read_folio()` 这一层之前或之后的 backend hook

## 7. 仍然保留的缺口

## 7.1 还没有 5.14 的私有 `PG_prefetch`

第二阶段用了 6.15 原生 `PageReadahead` 语义来近似替代。

这意味着：

- “命中过预取页”的统计已经能工作；
- 但它不再区分“upstream readahead”与“未来可能恢复的 Hermit 私有 prefetch”。

## 7.2 还没有真正的异步远程读

当前 direct path 和 `swapin_readahead()` 最终仍然落到：

- `swap_read_folio()`

所以它们还是 6.15 原生读栈的行为模型，而不是 5.14 Hermit 的：

- issue
- poll
- prefetch queue
- async completion

## 7.3 remoteswap/client 仍然依赖 frontswap

这是后续阶段必须正面处理的问题，当前第二阶段没有回避，但也没有强行错误适配。

## 8. 最小编译验证

已执行：

```bash
make -C hermit/linux-6.15 -j4 \
  mm/memory.o mm/swap_state.o \
  mm/memcontrol.o mm/vmscan.o \
  mm/hermit.o mm/hermit_utils.o mm/swap_stats.o
```

结果：

- `mm/memory.o`
- `mm/swap_state.o`

以及第一阶段相关对象均已成功编译。

## 9. 第二阶段结论

第二阶段的本质，不是把 5.14 Hermit 的 swap-in 路径直接照搬到 6.15，而是先做了一个
更稳的过渡版本：

- **行为上**：恢复了 `HMT_BPS_SCACHE`
- **统计上**：恢复了 swapin / prefetch / hit-on-prefetch
- **结构上**：把 Hermit 控制点嵌回了 6.15 native swap-in 主路径

这样第三阶段再去处理：

- async load
- speculative IO
- backend 注册接口
- remoteswap/client 重构

就不会再从“完全没有 Hermit 读路径语义”的状态起步了。
