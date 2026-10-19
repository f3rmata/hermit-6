# Hermit PEBS 设计与实现

> 状态：2026-10-19。PEBS 历史按 mm 保存，不依赖 cgroup；`CONFIG_HERMIT` 不要求
> `CONFIG_MEMCG`。历史设计与验证记录见 [PEBS 历史报告](../archive/pebs/)。

## 1. 目标

用 Intel PEBS 与成功的远程 demand fault 记录每个 mm 内的短期足迹，按
`(fault-PC, 首次缺页的区域内页偏移)` 在不同 2 MiB 区域之间学习。预测只决定
swap-in folio 大小，证据不足时读回 4 KiB，不增加异步预取。

| 量 | 当前实现 |
|---|---|
| 原始 folio / 远端 extent | 由分配、回收决定，本次不合并冷页 |
| 换入 folio | 在 extent、对齐、VMA 和连续 swap PTE 合法候选中选择 |
| 读 RDMA WR | 沿用 backend 分片规则，模型只计算其代价 |
| 写 RDMA WR | 选择不超过原 folio 的最大有效 order，完整写出原 folio |

## 2. 数据路径

### 2.1 swap-out

```
内存回收（kswapd / 直接回收 / memcg reclaim）
  -> shrink_folio_list()
  -> hermit_swapout_order()           # try_to_unmap() 之前
       原 folio order 与 effective_order_mask -> 最大合法分片
  -> pageout -> writeout -> swap_writeout_order
  -> hermit_swap_write_folio(folio, transfer_order)
       prepare_remote -> backend_store -> commit_remote
```

- 换出不查询 swap-in 模型，不受 `pebs_force_order` 影响，也不合并页面。
- `hermit_pebs_snapshot(mm, addr, out)` 保留 load/store 分离的最近观测窗口查询
  接口，供后续冷页合并设计使用；本次换出路径不调用它。
- 远端 store 成功即提交 extent；失败则 abort 后回落原生 swap。

### 2.2 swap-in

- `alloc_swap_folio()` 先验证候选范围，再调用 `hermit_swapin_order()`；
  分配或 charge 失败时依次尝试更小的合法 order，最终回退基础页。
- 远端 extent 必须完整覆盖候选范围；`bypass_swapcache=Y` 时使用 direct
  swapin，`speculative_io=Y` 在 fault 元数据处理前发起异步 load，
  `lazy_poll=Y` 对具体请求非阻塞 poll。
- order-PMD 的远端 extent 可以读入一个 2 MiB folio，再恢复多个 PTE；
  不直接建立 PMD 映射。原生 swap 保留上游 order 2–8 聚合策略。
- 远端 entry 没有本地副本；backend 不可用时读取返回错误，不回退到本地无效页。

## 3. 采样与区域表

### 3.1 事件与 ring

- 两个 Intel PEBS 事件：
  - `0x1d3` `MEM_LOAD_L3_MISS_RETIRED.LOCAL_DRAM`（Cascade Lake 编码，
    本次保留现有配置；其他 CPU 需要核对 PMU 事件）
  - `0x82d0` `MEM_INST_RETIRED.ALL_STORES`（DLA）
- `hermit_pebsd` 内核线程为每个在线 CPU 创建 per-CPU perf 事件，
  `PERF_SAMPLE_IP|TID|TIME|ADDR`，用户态 only；时间使用
  `CLOCK_MONOTONIC`，与缺页路径的 `ktime_get_ns()` 一致；ring 使用非覆盖模式。
- 采样周期使用质数表，并可按 `pebs_load_period`/`pebs_store_period` 固定；
  `pebs_adaptive=1` 时按 2 秒窗口自适应升降。事件是全局的，`hermit_pebsd`
  的 CPU 亲和性只限制 ring 消费。

### 3.2 固定事件时间窗口

- 历史属于 mm，无需 memcg。PID 启用或 `pebs_all=1` 决定跟踪范围。
- 成功安装远程缺页 PTE 后，以此次缺页入口时间启动窗口；真实用户 fault-PC
  由 `handle_mm_fault()` 的寄存器参数传入，不使用采样 IP 替代。
- 窗口为 `[start, start + 10 ms)`，之后再保留 10 ms 收集宽限期。
  持续访问不延长窗口。每区域两个槽容纳相邻窗口及其迟到样本，load/store 分离；
  demand fault 补充 load 足迹，不计入 PEBS load 样本数。
- 样本按事件时间归属，允许不同 CPU ring 乱序消费；收集期结束的样本不能修改
  已发布历史。没有活动窗口的样本不训练模型。
- 完成窗口按截止时间排入红黑树，后续缺页、样本或策略查询时懒发布；每次最多
  处理 64 个，积压只会延迟学习。元数据分配不回收、不睡眠，失败保守退回 4 KiB。

### 3.3 跨区域 fault-PC 足迹表

每个 mm 内用 `(fault-PC, 触发页偏移)` 查找最近 8 个完成窗口，保留 1 秒。
至少来自 4 个不同虚拟区域，合计 32 条 load 样本，才允许扩大读取。
同一区域窗口由第一个成功缺页的 PC 归属，其他线程/PC 的窗口内访问会补充该区域足迹。

区域表默认上限 32768，PC 表 4096，均按 LRU 淘汰；新 PC、新 mm、不同触发偏移、
历史过期或证据不足均选择 4 KiB。位图未置位仅代表未观测到，不能解释为确定冷页。

### 3.4 生命周期与并发

`mm_init()` 初始化，`__mmdrop()` 释放全部状态；sampler 持有 `get_task_mm()` 引用，
缺页路径持有当前 mm 生命周期。线程共享历史，新 fork mm 不继承。
启动/停止采样和重新按 PID 启用更新 epoch，旧历史立即逻辑失效、随后懒清理；
元数据内存仍受 LRU 上限约束，mm 退出时释放。修改窗口/容量参数应在停止采样时进行。

## 4. 决策模型

对合法候选 order `o ∈ {0, 2..9}`：

```
C(o) = [L_read + N_WR(o) * L_WR + B(o) / BW] / U(o)
```

- `B` 是实际读回字节数。`U` 是历史窗口中该对齐区间的平均观测有效页数；
  每个历史窗口都把本次 demand 页视为已用，因此 `U >= 1`。
- `L_read` 默认 7000 ns，表示整次逻辑读取固定开销。额外每 WR 开销 `L_WR`
  默认 0，避免重复计入整次读取延迟；带宽默认 12288 MiB/s。均需实机标定。
- `N_WR` 遵循现有读后端规则：候选 order 在有效掩码内时一个 WR，否则按 4 KiB
  分片。异常重试和带宽竞争不纳入 v1 模型。
- 固定与传输成本统一按有效页摊销；无密度下限。使用整数定点
  `ns/useful-page * 1024`，传输时间向上取整，成本相等选较小 order。
- 滞回参照同一 PC 表项上次预测的合法 order，默认要求改善至少 10%。
  历史失效或模型参数非法立即选择 4 KiB，不被旧 order 锁住。
- `pebs_force_order=2..9` 是全局实验覆盖，只约束 swap-in，仍需通过合法性检查；
  `0` 关闭覆盖。策略关闭或 mm 未启用时保留原换入路径。

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
| `pebs_force_order` | 0=关；2..9 指定 swap-in folio 上限 |
| `pebs_fixed_cost_ns` | 单次逻辑读取固定开销，默认 7000 ns |
| `pebs_wr_cost_ns` | 额外每 WR 开销，默认 0 |
| `pebs_bw_mibps` | 链路带宽 |
| `pebs_min_f` | 兼容节点，已弃用，不参与模型 |
| `pebs_hysteresis` | 迟滞（‰） |
| `pebs_cooling_period` | 兼容节点，已弃用，不参与窗口 |
| `pebs_window_ms` / `pebs_history_ms` | 观测窗口 10 ms / 历史 TTL 1000 ms |
| `pebs_min_regions` / `pebs_min_samples` | 证据阈值，默认 4 / 32 |
| `pebs_pc_max` | 每 mm PC 表上限，默认 4096 |
| `pebs_region_max` | 每 mm 区域表上限 |
| `pebs_order_stats` | 全局决策/采样/丢失计数 |
| `pebs_test_eval` | 8 个历史窗口使用相同输入位图的确定性模型测试 |
| `pebs_test_offset` / `pebs_test_candidates` | 模型测试的 fault 页偏移 / 候选掩码 |
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

`pebs_order_stats` 保留旧字段格式，新增窗口完成/过期、迟到或无窗口样本、
PC 命中、证据不足、元数据分配失败、淘汰及分配降级计数。`actual_order_N`
统计成功映射的远程 folio 大小；旧的逐 order 决策计数表示预测值。

## 6. 远端 extent 与传输

- 每个远端 folio 由 XArray extent 表示：先 reserve 全部 slot，store 成功后
  commit，失败则 erase 后回落原生 swap；slot 释放支持 partial invalidate。
- 大 folio 可以用一个可变长 RDMA WR 传输；DMA map、post 或 completion 失败时
  排空请求，再按 4 KiB 子 WR 重试整个 folio。
- `wr_stats` 按实际 WR order 统计请求数与字节数（含重试）；`order_stats`
  仍按原始 folio order 记账。
- 大于 4 KiB 的远端传输要求 swap 落在块设备（裸分区或 loop 设备）；swapfile
  会被内核拆成 4 KiB。

## 7. 验证

新增 `tools/rdma/tests/test_pebs_policy.py`，直接编译执行生产窗口、哈希表、
LRU、红黑树和模型代码；`test_kernel_paths.py` 覆盖真实分配循环的候选裁剪、
分配/charge 失败与最大粒度换出。测试中的时间、PC、区域、掩码均可确定性控制。
`pebs_test_eval` 的单页预期改为 order 0，全扫描仍为 9，chunk64k 仍为 4。

```sh
python3 tools/rdma/tests/test_pebs_policy.py
python3 tools/rdma/tests/test_kernel_paths.py
python3 tools/rdma/tests/test_pebs.py
```

测试启用 ASan/UBSan；在不支持 LeakSanitizer 的沙箱中可加
`ASAN_OPTIONS=detect_leaks=0`，PC/区域元数据仍有分配余额检查。
本次验证包括相关内核对象在 Hermit、MEMCG、THP 开关组合下编译；未启动 QEMU、
未运行真实 PEBS/RDMA 实验。旧版实验结果见 archive，不能当作新策略的性能证据。

## 8. 已知限制与后续工作

- 只在现有 `alloc_swap_folio()` direct 路径控制分配；swapcache 命中和常规
  readahead 不重新分配 folio。真实效果需要结合 `bypass_swapcache` 配置判断。
- 预测基于稀疏观测，偏保守；冷启动和一次性快速扫描未必有足够训练时间。
  不完整窗口、ring 丢样和多线程区域共享都会影响预测质量。
- 不能跨远端 extent 合并读取。已换出为 4 KiB extent 的页面不会仅靠预测自动
  恢复为大 extent；冷页合并另行设计。
- mm 内 unmap/remap 不单独建立代际，历史以窗口/TTL 衰减；PC 表只提供大小提示，
  extent 和 PTE 检查始终负责保证读取范围正确。
- 全机 per-CPU 采样成本仍是全局的；支持按 mm 过滤历史，不代表只采目标进程。
- 当前 `lost` 混合 ring 异常和丢失记录事件数，不是精确丢样数量；本次没有改变
  该字段语义。真实采样质量与模型参数需在目标 PMU/RDMA 平台验证。
