# Hermit PEBS 设计与实现

> 状态：2026-10-18。PEBS 历史按 mm 保存，不依赖 cgroup；`CONFIG_HERMIT` 不要求
> `CONFIG_MEMCG`。历史设计与验证记录见 [PEBS 历史报告](../archive/pebs/)。

## 1. 目标

用 Intel PEBS 硬件采样测量每个 mm 内 2 MiB 虚拟区域的访问密度与空间局部性，
在 Hermit swap 路径上用代价模型选择每次传输/读回的 folio 粒度，替代全局静态的
`remote_order_mask`。

需要区分三个量：

| 量 | 含义 | 当前实现 |
|---|---|---|
| 原始 folio | Linux 分配、回收和远端 extent 的单位 | 由 THP/mTHP 与回收决定 |
| 换入 folio | 一次缺页实际读回多少数据 | PEBS 策略限制 `candidate_orders` |
| RDMA WR | 一次请求传输多少数据 | 由 backend 按 transfer order 分段 |

当前策略主要控制“换入 folio”，并经由 folio 的 transfer order 间接影响 WR；
三者尚未完全解耦，见 §9。

## 2. 数据路径

### 2.1 swap-out

```
内存回收（kswapd / 直接回收 / memcg reclaim）
  -> shrink_folio_list()
  -> hermit_swapout_order()           # try_to_unmap() 之前
       anon_vma 读锁 -> vma->vm_mm -> hermit_order_policy(mm, addr)
  -> pageout -> writeout -> swap_writeout_order
  -> hermit_swap_write_folio(folio, transfer_order)
       prepare_remote -> backend_store -> commit_remote
```

- 策略查询在 `try_to_unmap()` 之前完成；`mmgrab()/mmdrop()` 固定 vma 的 mm，
  避免与 `__mmdrop()` 释放 PEBS 状态竞争。
- 远端 store 成功即提交 extent 并跳过本地 swap BIO；失败则 `abort_remote()` 后
  回落到原生 swap。
- 没有样本、映射或策略未启用时返回 -1，由调用方回退静态配置。

### 2.2 swap-in

- `alloc_swap_folio()` 对远端 entry 按策略 order 收紧 `candidate_orders`，
  把单次 fault 的读回聚合粒度限制在模型范围内。
- 远端 extent 必须完整覆盖候选范围；`bypass_swapcache=Y` 时使用 direct
  swapin，`speculative_io=Y` 在 fault 元数据处理前发起异步 load，
  `lazy_poll=Y` 对具体请求非阻塞 poll。
- order-PMD 的远端 extent 可以读入一个 2 MiB folio，再恢复多个 PTE；
  不直接建立 PMD 映射。原生 swap 保留上游 order 2–8 聚合策略。
- 远端 entry 没有本地副本；backend 不可用时读取返回错误，不回退到本地无效页。

## 3. 采样与区域表

### 3.1 事件与 ring

- 两个 Intel PEBS 事件：
  - `0x1d3` `MEM_LOAD_RETIRED.L3_MISS`（PEBS-LL，DLA + latency + data source）
  - `0x82d0` `MEM_INST_RETIRED.ALL_STORES`（DLA）
- `hermit_pebsd` 内核线程为每个在线 CPU 创建 per-CPU perf 事件，
  `PERF_SAMPLE_IP|TID|ADDR`，用户态 only；ring 使用非覆盖模式。
- 采样周期使用质数表，并可按 `pebs_load_period`/`pebs_store_period` 固定；
  `pebs_adaptive=1` 时按 2 秒窗口自适应升降。事件是全局的，`hermit_pebsd`
  的 CPU 亲和性只限制 ring 消费。

### 3.2 归属与区域表

- 样本按 TID→task→`mm` 归属；只有该 mm 被启用（或全局 `pebs_all=1`）时才
  更新历史。
- 区域表是**每 mm** 一份：key=`vaddr>>21`，512-bit 触及位图 + 冷却时钟；
  两拍无访问即清空位图（保守衰减到未知）；超过 `pebs_region_max` 时按 LRU
  淘汰。
- 首次采样时懒分配 `hermit_pebs_state`，通过 `cmpxchg` 挂到
  `mm->hermit_pebs`；`mm->hermit_pebs_enabled` 表示该 mm 已启用。
- 冷却时钟按 `pebs_cooling_period` 个样本推进一次，不是固定时间窗口；
  历史长度会随采样率和负载变化。

### 3.3 生命周期与并发

- `mm_init()` 初始化指针；`__mmdrop()` 在最后一个 `mm_count` 引用释放时调用
  `hermit_pebs_mm_exit()` 释放区域表。
- 采样路径持有 `get_task_mm()` 的引用；swap-out 用 `mmgrab()/mmdrop()`；
  swap-in 使用当前 fault 的 mm，因此不会与状态释放竞争。
- 线程共享同一 mm，也共享同一份历史；`fork` 出的新 mm 默认不继承历史。

## 4. 决策模型

对候选 order `o`（页数 `2^o`，`o ∈ {0, 2..9}`，排除非法的 order 1）：

```
c(o) = C_fix / 2^o + (1 / max(f(o), min_f)) * 4096 / BW     [ns per 4KiB page]
```

- `f(o)`：该 folio 对齐子区间内被采样触及的 4 KiB 子页比例。
- `C_fix`：单次 WR 固定开销，默认 7000 ns。
- `BW`：链路带宽，默认 12288 MiB/s。
- `min_f`：`f` 下限（‰），默认 50，防止 `1/f` 爆炸。
- 选 `argmin c(o)`，平手取较大 order；新 order 的改善不足
  `(1000 - pebs_hysteresis)`‰ 时保持当前 order，防止振荡。
- `pebs_force_order=2..9` 绕过策略，用于功能验证；`0` 关闭。
- 无状态/无样本/样本过期时返回 -1，回退静态 `remote_order_mask`。

## 5. 控制面与 ABI

### 5.1 启用与停止

- syscall **473 `hermit_pebs_start(pid, mode)`** / **474 `hermit_pebs_end(pid)`**；
  需要 `CAP_SYS_ADMIN`。
- `pid=-1` 只启动/更新全局采样；`pid>0` 同时启用该进程的 mm。采样已在运行时
  再次调用只执行启用/更新，不返回 `EBUSY`。
- debugfs `pebs_all=1` 自动启用所有 mm，适合没有 cgroup、也不方便逐 pid
  启用的场景。
- cgroup 不是 PEBS 的启用路径；实验仍可用 `memory.max/high`、
  `memory.swap.max` 做限额和隔离。

### 5.2 debugfs

| 文件 | 含义 |
|---|---|
| `pebs_enabled` | 全局采样/策略主开关 |
| `pebs_all` | 1=自动跟踪所有 mm |
| `pebs_mode` | 0=static，1=policy |
| `pebs_force_order` | 0=关；2..9 强制传输 order |
| `pebs_fixed_cost_ns` | 单次 WR 固定开销 |
| `pebs_bw_mibps` | 链路带宽 |
| `pebs_min_f` | f 下限（‰） |
| `pebs_hysteresis` | 迟滞（‰） |
| `pebs_cooling_period` | 冷却周期（样本数） |
| `pebs_region_max` | 每 mm 区域表上限 |
| `pebs_order_stats` | 全局决策/采样/丢失计数 |
| `pebs_test_eval` | 复用生产决策函数的合成位图测试 |
| `pebs_ring_test` | 跨页/回绕与 perf ring 生命周期测试 |
| `remote_order_mask` | 允许的传输 order 位图，默认 `0x1` |
| `effective_order_mask` | 配置与 backend 能力的交集 |
| `order_stats` | 逐 order store/load/4 KiB fallback/error |
| `bypass_swapcache` | 远端 entry 使用 direct swapin |
| `speculative_io` | 在 fault 元数据前发起异步 load |
| `lazy_poll` | 非阻塞 poll，未完成时继续等待 |
| `apt_reclaim` | 可选 memcg 主动回收（仅 `CONFIG_MEMCG=y`） |
| `reclaim_mode` / `reclaim_headroom_pages` / `sthd_cnt` | 可选回收参数 |

`memory.hermit_pebs` 已删除；旧的 memcg PEBS 状态和 `hermit_pebs_memcg_*`
接口不再存在。

## 6. 远端 extent 与传输

- 每个远端 folio 由 XArray extent 表示：先 reserve 全部 slot，store 成功后
  commit，失败则 erase 后回落原生 swap；slot 释放支持 partial invalidate。
- 大 folio 可以用一个可变长 RDMA WR 传输；DMA map、post 或 completion 失败时
  排空请求，再按 4 KiB 子 WR 重试整个 folio。
- `wr_stats` 按实际 WR order 统计请求数与字节数（含重试）；`order_stats`
  仍按原始 folio order 记账。
- 大于 4 KiB 的远端传输要求 swap 落在块设备（裸分区或 loop 设备）；swapfile
  会被内核拆成 4 KiB。

## 7. 验证矩阵

| 层次 | 用例 | 状态 |
|---|---|---|
| 单元 | `pebs_test_eval`：全扫描→9、chunk64k→4、单页→2 | 通过（QEMU guest） |
| 功能 | QEMU DRAM：checksum、backend error=0、本地 swap 写扇区不变 | 通过 |
| 功能 | QEMU 64 KiB mTHP / 2 MiB THP / lazy poll / 普通 swapcache | 通过（历史记录见 archive） |
| 编译 | `CONFIG_MEMCG=y` 与 `CONFIG_MEMCG=n` 的 `bzImage` | 通过 |
| 对照 | dnet61：`perf mem record` 真值与 region 位图对比 | 待做 |
| 端到端 | dnet61：memcached/redis policy vs static | 已有阶段性结果，见 evaluation |

## 8. 已知限制

- QEMU 内没有真实 PEBS：本地只验证策略、ring 和传输正确性；硬件采样需要在
  支持 PEBS 的机器上验证。
- 常规回收在 unmap 前取策略；已经未映射且没有策略快照的 folio 仍回退静态。
- swap-out 仍是单一 extent（folio order），策略只控制 WR 粒度和 swap-in 聚合；
  folio 内异构子 extent 拆分尚未实现。
- PEBS 历史按 mm 保存；多进程负载需要逐进程启用或 `pebs_all=1`。
- 6.18 没有 per-memcg mTHP 分配控制；分配侧仍依赖全局 THP sysfs。
- 全机 per-CPU 事件会采到客户端等其他进程；区域跟踪只记录被启用的 mm，但
  采样开销和 ring 消费仍是全局的。
- 高频采样（如 load period=199）在部分负载上出现 lost；精确丢样率不能由现有
  计数器直接得出，需要先修复计数/ring 处理。

## 9. 下一步

1. **拆分三个尺寸**：原始 folio、换入 folio、RDMA WR 独立决策，允许子 extent
   读取；swap-out 仍尽量整 folio 写出。
2. **从“累计触及”转为“预测本次读回会用多少”**：围绕 fault 地址估计近期窗口
   的邻页复用、额外 fault 节省和冷数据代价，而不是只看历史位图。
3. **冷启动策略**：首触 4 KiB、二次缺页再 readahead，或基于 fault-around 的
   预测；这直接对应一次性流式负载无收益的问题。
4. **页面选择**：在换出侧区分“误换出热页”和“传输粒度”，评估 `MADV_PAGEOUT`
   受控实验中的实际 folio、WR 字节和读放大。
5. **统计与采样范围**：per-mm/per-cgroup 决策统计、按目标 CPU/cgroup 创建
   perf 事件、修复高频 lost。
