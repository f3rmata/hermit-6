# dnet-61 hermit 测试

<!-- - Linux 6.18.38-hermit 运行 Memcached 和 Mutilate，测试 `local`、`cgroup-local` 与 `cgroup-Hermit` 三种模式；数据集为 3200 万条记录。
- Memcached 使用 0–7 核，Mutilate 使用 8–15 核，每个负载重复3次并取中位数。
- mTHP页大小 4 KiB–2 MiB，本地和远端内存比例设为70%
- 2026-08-12 后使用 `/dev/sdb6` 块设备，修复后的高阶 swap-out 与 swap-in 均可用； -->

## 2026-08-17：XGBoost 大页 RDMA swap-out/swap-in

使用 HIGGS CSV（1100 万行、28 个特征），`hist` 方法训练 30 轮，XGBoost 线程数为 4；每个页大小重复 3 次取中位数。READY 后实际常驻约 2538–2540 MiB，`memory.max` 为 70%（约 1776–1778 MiB），训练 AUC 在所有配置中均为 0.820776。

XGBoost hist 训练也主要对 dense DMatrix 做块状、顺序或高局部性访问。因此它与内存扫描的结果更接近。

![XGBoost 大页 RDMA swap 测试](../tools/rdma/results/dnet-61/20260817-175412-xgboost-swapio/xgboost-swapio.png)

|  页大小 | order | swap-out 协议 GiB/s | 大页 store 字节占比 | store 请求数 | 大页 load 字节占比 | load 请求数 | train 秒 |
| ------: | ----: | ------------------: | ------------------: | -----------: | -----------------: | ----------: | -------: |
|   4 KiB |     0 |               0.636 |                  0% |      201,862 |                 0% |     194,886 |  195.854 |
|  16 KiB |     2 |               1.457 |             99.668% |       50,349 |            99.971% |      48,991 |  193.863 |
|  32 KiB |     3 |               1.975 |             99.541% |       25,188 |            99.959% |      24,652 |  194.022 |
|  64 KiB |     4 |               2.656 |             99.252% |       12,611 |            99.922% |      12,419 |  193.376 |
| 128 KiB |     5 |               3.155 |             98.636% |        6,298 |            99.815% |       6,244 |  193.330 |
| 256 KiB |     6 |               3.462 |             97.795% |        2,981 |            99.577% |       2,970 |  193.306 |
| 512 KiB |     7 |               3.665 |             97.448% |        1,486 |            99.350% |       1,485 |  193.135 |
|   1 MiB |     8 |               4.255 |             98.317% |        1,435 |            99.374% |       1,435 |  193.395 |
|   2 MiB |     9 |               4.399 |             98.039% |          543 |                 0% |           0 |  187.264 |

结论：

1. swap-out 协议吞吐从 4 KiB 的 0.636 GiB/s 提升到 2 MiB 的 4.399 GiB/s，约S6.9×；store 请求数从 201,862 降到 543，说明减少 WR、DMA map/unmap、入队和Scompletion 的固定开销是主要收益来源。16 KiB–1 MiB 已经有 97.4%–99.7% 的大页 store 字节占比，说明后端确实按目标 order 传输。
2. order 2–8 的 swap-in 大页字节占比为 99.35%–99.97%，与 dense 训练矩阵的访问特征一致。训练时间从 195.854 s（4 KiB）降到约 193.1–193.9 s（16 KiB–1 MiB），实际应用收益只有约 1%–1.4%，因为 XGBoost 主要受计算限制，而不是受 swap 协议带宽限制。
3. 2 MiB 的 `large_load_pct=0` 且 `target_loads_delta=0`，当前不能作为完整 2 MiB swap-in 的收益证据。现有内核的swap-in 候选 order 还没有覆盖 order 9，必须补齐 2 MiB swap-in 路径后再纳入结论。

## 测试方法

- memcached 主要暴露了随机访问下的大页读放大；
- 匿名数组扫描测到的是连续、全覆盖访问下的大 WR 协议收益；
- XGBoost 也是高空间局部性、计算占主导的 dense workload，因此只会看到交换协议收益被训练计算掩盖；

### 大页收益

匿名数组扫描和XGBoost测试随页变大而变快
增大 folio 后，主要减少了：

- DMA map/unmap；
- RDMA WR post；
- CQ completion；
- 锁和对象管理；
- 每次请求的固定延迟。

### 读/写放大问题

Memcached的测试时吞吐随页面大小提升而下降
其中16–256 KiB 的情况符合随机小对象访问的预期：一次 fault 会把整个 folio 搬回来，但实际只访问其中很小的一部分，产生读放大

> 读放大 = 远端实际读回字节 / 应用真正访问字节

此时大页虽然减少了 RDMA 请求数，却增加了：

- 无用数据传输；
- fault 延迟；
- RDMA 队列和 CQ 压力；
- cache 污染；
- 访存线程等待时间。

所以吞吐下降，128 KiB 附近最差。

#### 访问热度偏斜的收益分析

即决定了大页带来的固定开销节省是否会被读放大抵消。
按当前三类负载，潜在收益排序为：Memcached 最大，XGBoost 中等，顺序数组扫描最小。

- **Memcached**：随机且离散的热点最适合小粒度 swap-in；swap-out 可以继续使用较大 WR 以降低回收成本，形成“大粒度换出、小粒度换入”的非对称策略。若热点在一个 folio 内连续，中等粒度才有收益。
- **匿名数组扫描**：访问热度接近均匀，静态大页已经接近最优；按热度拆分只会增管理成本，收益很小。
- **XGBoost**：训练矩阵的 dense 区域适合大粒度双向传输，但不同训练阶段或特征块
  可能存在区域性热点，未来可按区域/阶段选择粒度，而不是全局固定一个 order。

### 理想 workload

小粒度：请求固定开销较高
中等粒度：达到最佳点
大粒度：读/写放大主导，吞吐下降

## 2026-08-20：Sparse/Random 匿名内存测试

<!--
测试目录为 `20260820-114721-anon-sparse-full`。工作集为 16 GiB，`memory.max`为 11468 MiB（约 70%），通过 RDMA 换出约 4.8–5.0 GiB。测试覆盖 9 种页大小、5 种访问比例、顺序/随机 folio 顺序和高/低 folio 内局部性，每种组合重复 3 次，共 `9 × 5 × 2 × 2 × 3 = 540` 轮。图中数据为每个页大小和访问比例下 12 个样本的中位数。 -->

有效吞吐：控制一个mTHP folio中设置的热页比例，并测量得到的吞吐
每次测试都按不同的folio顺序和folio内偏斜测试。图中数据为每个页大小和访问比例下 12 个样本的中位数。

- sequential + high locality
- sequential + low locality
- random + high locality
- random + low locality

![Sparse/Random 匿名内存传输收益与读放大](assets/dnet61-anon-sparse-20260820/anon-sparse-benefit-summary.png)

### 结果分析

|  页大小 | swap-out 协议 GiB/s | 100% 有效 GiB/s | 25% 有效 GiB/s | 6.25% 有效 GiB/s | 1 页/folio 有效 GiB/s | 1 页/folio 读放大 | 大页 load 字节占比 |
| ------: | ------------------: | --------------: | -------------: | ---------------: | --------------------: | ----------------: | -----------------: |
|   4 KiB |               0.619 |           1.847 |              x |                x |                 1.847 |                1× |                 0% |
|  16 KiB |               1.484 |           3.905 |          1.221 |                x |                 1.206 |                4× |               100% |
|  32 KiB |               2.028 |           5.178 |          1.694 |                x |                 0.913 |                8× |               100% |
|  64 KiB |               2.787 |           6.612 |          2.404 |            0.679 |                 0.684 |               16× |               100% |
| 128 KiB |               3.439 |           7.537 |          2.879 |            0.837 |                 0.429 |               32× |               100% |
| 256 KiB |               3.974 |           8.153 |          3.269 |            0.965 |                 0.253 |               64× |               100% |
| 512 KiB |               4.330 |           8.460 |          3.486 |            1.043 |                 0.139 |              128× |               100% |
|   1 MiB |               4.555 |           8.639 |          3.596 |            1.081 |                 0.073 |              256× |               100% |
|   2 MiB |               5.110 |           2.390 |          2.349 |            2.224 |                 1.307 |                1× |                 0% |

### 收益与放大

1. swapout在页面大小增大时收益一直上升：吞吐从 4 KiB 的 0.619 GiB/s 增至 1 MiB 的 4.555 GiB/s（7.36×）和 2 MiB 的 5.110 GiB/s（8.25×）。
2. 50%、25% 和 6.25% 访问分别产生约 2×、4× 和 16× 的读放大。对 16 KiB–1 MiB，同一比例的读放大不随页大小变化，而 RDMA 协议吞吐随粒度提高，因此 25% 有效吞吐从 16 KiB 的 1.221 GiB/s 增至1 MiB 的 3.596 GiB/s。此前全扫描和 XGBoost 随页大小提高而变快，符合这一结果。
3. 读放大随 folio 大小从 4×、8×、16×一直增长到 1 MiB 的 256×，实测值与理论值一致；有效吞吐从 16 KiB 的1.206 GiB/s 降到 1 MiB 的 0.073 GiB/s。这才是随机稀疏热点下预期的大页性能下降。

### 顺序与局部性

在实际使用大粒度 load 的 16 KiB–1 MiB 范围内，随机 folio 顺序相对顺序访问的协议吞吐中位数低 6.3%。惩罚从 16 KiB 的约 17.0% 逐渐降至 1 MiB 的约 1.6%，说明大传输能更充分地摊薄随机 fault 和请求调度开销。

低 folio 内局部性相对高局部性只低约 1.3%。这不是局部性不重要，而是当前 workload 会访问每一个 folio：第一次 fault 已经读回整个 folio，之后访问连续还是分散的基页都不会改变 RDMA 字节数。若要测试热度偏斜收益，需要固定逻辑区域和地址集合，让高局部性访问集中在少量传输 folio、低局部性访问分散到更多传输 folio。

### 用访问密度控制写/读放大

在相同 workset、相同 cgroup 压力和相同远端数据量下，构造四种模式：

1. 顺序扫描全部 4 KiB 子页（`f≈1`）；
2. 每个 folio 只访问一个随机子页（最大读放大）；
3. 每个 folio 访问连续的 `k` 个子页（可控的空间局部性）；
4. Zipf/热点-冷数据访问（接近 Memcached），分别改变热点比例和热点是否连续。

这样可以直接画出 `active_ratio` 与最佳传输粒度的关系：密集访问应随粒度增大
后平台，稀疏随机访问应在某个粒度后下降。

<!-- ### 分离 swap-out、swap-in 和应用阶段

每一档分别记录：实际传输字节数、WR 数、协议阶段 wall time、应用阶段 wall time、
`large_*_pct`、fallback/error、以及 checksum/AUC 等正确性指标。每档至少 3 次，建议随机化页大小顺序并报告中位数和离散度。 -->

## 统一分析：传输粒度、访问密度与预期收益

本项目中的 page sweep 同时改变了两件事：mTHP 实际分配的 folio 大小，以及
Hermit 的 `remote_order_mask`（目标 RDMA 传输 order）。因此，当前曲线应解释为“folio 大小 + 传输粒度”的组合结果，不能单独归因于其中一个因素。
固定有效数据量`W`，`G` 是一次传输的粒度：

```text
请求数 N ≈ W / G
 总时间 T ≈ N × 每个请求的固定开销 + W / RDMA 有效带宽
```

如果一个 `G` 大小的 folio 中只有比例 `f` 的子页真正被访问，则读放大约为 `1/f`。
例如只访问一个 4 KiB 子页时，16 KiB、256 KiB 和2 MiB folio 的潜在读放大分别为 4×、64× 和 512×。
所以大页的收益不是由页大小本身决定，而是取决于请求固定开销节省与无效数据传输代价的平衡。

三类负载的预期如下：

- **Memcached** 是随机小对象访问，`f` 通常很低且热点分散。4 KiB 或较小的中等粒度应接近最佳；页继续增大后，单次换入带回大量未使用数据，吞吐和 p99 会恶化。如果热点在 folio 内连续或具有较强空间局部性，最佳点才可能向 16–64 KiB 移动。
- **匿名数组扫描** 是顺序、完整覆盖访问，`f≈1`。大 folio 的数据几乎都会被消费，因此不会出现预期的写放大；随着粒度增大，请求数下降，吞吐应单调上升后在 RDMA、DMA、CPU 和 reclaim 带宽处平台化。
- **XGBoost `hist`** 对连续训练矩阵进行块/列扫描，整体更接近 dense 顺序访问，因而协议吞吐应随粒度提高；但训练本身是计算密集型，应用总时间的改善会明显小于后端 RDMA 带宽的改善。

<!--
这也解释了为什么“数组扫描和 XGBoost 吞吐随页面大小提高”并不反常：它们并没有
触发大页内部的稀疏访问放大。cgroup 的作用主要是制造可控的内存压力并触发 swap，
不是吞吐上升的根因；真正的区别是访问覆盖率和空间局部性。 -->

<!-- ## 一些想法

与传统的Tier Memory System相比，我们的RDMA Swap系统有很多可以针对性优化，并做出创新的地方：

- Memory tiering 系统每时每刻都在触发内存的迁移，拆分和合并。但是Swap系统只在整个系统面临内存严重不足/存在大量闲置页的情况下才会触发
- RDMA内存的延迟远高于Memory tiering系统的PMEM，所以我们的内存管理应该是更加消极的
- 可以参考Memory tiering的PEBS等性能监测的设施，调节swap-in,swap-out的页面大小等参数。在提高吞吐量和避免写放大做tradeoff
- 还有就是workload和网卡的背景流量要再更换调整和测试 -->

## 历史结果

下表只统计实测数据；“峰值”是每种页大小在所有 offered load 中达到的最大achieved QPS。

| 测试时间   | 模式与覆盖范围               | 平均峰值（KQPS） | 页大小峰值范围（KQPS） | 说明                                                            |
| ---------- | ---------------------------- | ---------------: | ---------------------: | --------------------------------------------------------------- |
| 2026-07-27 | local，4 KiB–2 MiB           |            928.2 |            925.4–931.9 | 第一轮完整 native 基线                                          |
| 2026-07-27 | cgroup-local，4 KiB–2 MiB    |              5.5 |                4.5–6.5 | 70% 内存比例下发生严重换页抖动                                  |
| 2026-07-30 | cgroup-Hermit，4 KiB–2 MiB   |            672.3 |            659.1–687.9 | 使用普通 swapfile，高阶 folio 在 swap-out 前回退为 4 KiB        |
| 2026-08-03 | cgroup-Hermit，4 KiB–2 MiB   |            713.6 |            705.3–728.1 | 2 MiB 高负载为 `mixed`；只统计 `stable` 时其峰值为 492.4 KQPS   |
| 2026-08-04 | cgroup-Hermit，仅 2 MiB 复测 |            713.3 |                  713.3 | 最高点仍为 `mixed`，稳定峰值为 493.7 KQPS                       |
| 2026-08-08 | local，4 KiB–2 MiB           |            943.0 |            940.4–945.9 | 最新完整 local 基线                                             |
| 2026-08-08 | cgroup-local，4 KiB–2 MiB    |              4.9 |                3.6–6.5 | 仍处于严重 thrashing 区间                                       |
| 2026-08-08 | cgroup-Hermit，4 KiB–2 MiB   |            710.4 |            701.3–716.5 | `/dev/sdb6` 块设备；大页 RDMA store 生效且无 fallback           |
| 2026-08-09 | local，4 KiB–2 MiB           |            941.8 |            938.4–944.4 | 与 8 月 8 日相比变化约在 ±0.6% 内                               |
| 2026-08-09 | cgroup-local，4 KiB–2 MiB    |              5.8 |                4.5–7.1 | 低吞吐抖动状态重复出现                                          |
| 2026-08-12 | local，4 KiB–2 MiB           |            942.0 |            937.5–945.5 | swap-in 修复后同轮 native 基线                                  |
| 2026-08-12 | cgroup-local，4 KiB–2 MiB    |              5.7 |                4.1–6.9 | 70% 内存比例，持续严重 thrashing                                |
| 2026-08-12 | cgroup-Hermit，4 KiB–2 MiB   |            453.8 |            186.3–685.4 | 高阶 swap-in 已修复；64–256 KiB 出现随机访问 read amplification |

### 2026-07-30：早期 Hermit 完整 sweep

![2026-07-30 Hermit 实测吞吐与 p99](../tools/rdma/results/dnet-61/20260730-184935-hermit-6.18-page-sweep/measured-throughput-by-page.png)

该轮 Hermit 峰值约为 659–688 KQPS，但 debugfs 中只有 order 0 的传输计数；
原因是普通 swapfile 无法分配高阶 swap entry，大 folio 在进入 RDMA backend 前已拆分为 4 KiB。

### 2026-08-03：Hermit 完整 sweep

![2026-08-03 Hermit stable 与 mixed 峰值](../tools/rdma/results/dnet-61/20260803-231615-hermit-6.18-page-sweep/cgroup-hermit-throughput-by-page-measured.png)

4 KiB–1 MiB 的负载点均为 `stable`；2 MiB 在 750 KQPS 以上达到约 705 KQPS，
但稳定等待状态为 `mixed`。

### 2026-08-04：2 MiB 单独复测

![2026-08-04 2 MiB 复测叠加](../tools/rdma/results/dnet-61/20260804-195156-hermit-6.18-page-sweep/cgroup-hermit-throughput-by-page-measured-with-rerun.png)

2 MiB 复测峰值由约 705.3 KQPS 提高到 713.3 KQPS，峰值 p99 从约 4.58 ms
降低到 4.51 ms；高负载仍为 `mixed`

## 2026-08-08：修复前基线

![2026-08-08 完整页大小吞吐对比](../tools/rdma/results/dnet-61/20260808-140944-hermit-6.18-native-hermit/latest-mode-throughput-comparison.png)

![2026-08-08 在 500 KQPS 下的 read p99 对比](../tools/rdma/results/dnet-61/20260808-140944-hermit-6.18-native-hermit/latest-read-p99-comparison-500k.png)

该轮在相同的 500 KQPS offered load 下，local 的 read p99 约为 59 µs，
cgroup-Hermit 约为 1.7–2.1 ms，而严重换页抖动的 cgroup-local 达到约 0.22–0.65 s。

## 2026-08-12：高阶 swap-in 修复后结果

测试目录为 `20260812-141803-hermit-6.18-native-hermit`。条件保持为 Linux 6.18.38-hermit、3200 万条记录
Memcached 0–7 核、Mutilate 8–15 核、每档负载重复 3 次取中位数、cgroup 内存比例 70%；

|  页大小 | local 峰值（KQPS） | cgroup-local 峰值（KQPS） | cgroup-Hermit 峰值（KQPS） |
| ------: | -----------------: | ------------------------: | -------------------------: |
|   4 KiB |              941.7 |                       6.9 |                      685.4 |
|  16 KiB |              944.6 |                       6.5 |                      519.7 |
|  32 KiB |              943.5 |                       6.0 |                      377.1 |
|  64 KiB |              937.5 |                       4.9 |                      244.7 |
| 128 KiB |              942.8 |                       4.1 |                      186.3 |
| 256 KiB |              940.6 |                       4.8 |                      220.4 |
| 512 KiB |              945.5 |                       5.3 |                      501.5 |
|   1 MiB |              943.1 |                       6.8 |                      670.1 |
|   2 MiB |              938.5 |                       5.7 |                      679.2 |

![swap-in 修复后不同页大小的峰值吞吐](assets/dnet61-memcached-page-sweep-20260812/memcached-peak-throughput.png)

local 峰值稳定在 937.5–945.5 KQPS；cgroup-local 只有 4.1–6.9 KQPS，处于严重 thrashing。
cgroup-Hermit 为 186.3–685.4 KQPS，并呈明显 U 形：64–256 KiB 实际大量换入高阶 folio，随机小对象只消费其中少数子页，导致 swap-in 读放大；同时 swap-out 若按完整 folio 传输也会产生写放大。1–2 MiB 组则主要退化为 4 KiB load，因此其高吞吐不能当作 1–2 MiB 协议收益。

> 目前Linux内核还没有为2MiB的大页做swapin的单独路径优化，所以测试时所有2MiB的大页都会被拆分成4KiB的小页，导致退化

![500 KQPS offered load 下的 read p99](assets/dnet61-memcached-page-sweep-20260812/memcached-read-p99-500k.png)

![memcached 实际高阶 RDMA load 字节占比](assets/dnet61-memcached-page-sweep-20260812/memcached-high-order-load-share.png)

逐 order 前后快照表明，16–256 KiB 的目标 order load 字节占比为84.2%–98.1%；512 KiB 降至 20.4%，1 MiB 仅 0.15%，2 MiB 为 0%。因此本轮memcached在随机访问模式没有表现出协议性能收益；更适合用顺序或空间局部性更强的匿名内存 workload 衡量大 WR 收益。

<!-- ![专项 mTHP swap-in 修复前后对比](assets/dnet61-memcached-page-sweep-20260812/mthp-swapin-before-after.png)

专项测试顺序换入固定 512 MiB 匿名区域。修复前所有配置均退化成 131072 次4 KiB load；修复后 16 KiB–1 MiB 均完全使用目标 order，耗时由约0.69–0.74 s 降至 0.091–0.296 s，提升 2.47–7.68 倍。 -->

## 2026-08-13：顺序匿名内存大页 swap-out/swap-in

用mmap分配匿名内存，再按指定mTHP大小分配并顺序写入内存。
再把专用 cgroup 的 `memory.max` 从 `max` 降至 11468 MiB，每轮通过 RDMA 换出约5 GiB；换出稳定后将上限恢复为 `max`，顺序扫描完整映射触发 swap-in，记录时间和吞吐数据。

![顺序匿名内存大页 RDMA swap-out 和 swap-in](assets/dnet61-anon-swapio-20260813/anon-swapio-throughput-by-page.png)

表中采用 3 次测试的中位数。swap-in 吞吐以实际远端读回字节为分子，以顺序扫描完整 16 GiB 映射的时间为分母：

|  页大小 | swap-out（GiB/s） | 相对 4 KiB | swap-in（GiB/s） | 相对 4 KiB |                store/load 数 |
| ------: | ----------------: | ---------: | ---------------: | ---------: | ---------------------------: |
|   4 KiB |             0.617 |      1.00× |            0.626 |      1.00× |        1,301,009 / 1,301,009 |
|  16 KiB |             1.477 |      2.39× |            1.322 |      2.11× |            326,536 / 326,536 |
|  32 KiB |             2.029 |      3.29× |            1.738 |      2.78× |            163,272 / 163,272 |
|  64 KiB |             2.836 |      4.60× |            2.180 |      3.48× |              81,641 / 81,641 |
| 128 KiB |             3.423 |      5.55× |            2.415 |      3.86× |              40,825 / 40,825 |
| 256 KiB |             3.997 |      6.48× |            2.509 |      4.01× |              19,793 / 19,793 |
| 512 KiB |             4.322 |      7.00× |            2.587 |      4.13× |                9,897 / 9,897 |
|   1 MiB |             4.534 |      7.35× |            2.635 |      4.21× |                4,949 / 4,949 |
|   2 MiB |             5.099 |      8.26× |            0.722 |      1.15× | 2,475 / 0（load 为 order-0） |

swap-out 吞吐在 4–256 KiB 区间增长最快，512 KiB 后逐渐接近 4.3–5.1 GiB/s 的平台，说明瓶颈开始从每-WR提交和 completion 开销转向内存/DMA/RDMA 带宽以及cgroup reclaim、swap slot 和 folio accounting。

swap-in 吞吐从 4 KiB 的 0.626 GiB/s 增至 1 MiB 的2.635 GiB/s。完整 16 GiB 映射的扫描带宽同时从 2.02 GiB/s 增至8.72 GiB/s。所有 27 轮的 `fallback=0`、`errors=0`、`checksum_errors=0`、`oom_kill=0`，且 swap-in 扫描期间 `pswpout_delta=0`，没有边换入边换出的抖动。

重复性方面，4 KiB–2 MiB 的 swap-out 吞吐差为 0.4%–4.0%，swap-in 为0.02%–1.1%
4 KiB swap-out 的 3 次中有一次耗时10.66 s，使其吞吐差达到 24.5%，说明大量小 WR 对 CPU 调度、CQ 轮询和队列竞争更加敏感。

每个 WR 都涉及：

- 构建 RDMA 请求；
- DMA map/unmap；
- 入队和 post send；
- CQ completion 处理；
- pending 计数和对象释放；
- CPU 调度和锁竞争。

原始汇总数据：

- [2026-07-27 Native](../tools/rdma/results/dnet-61/20260727-235121-hermit-6.18-native-hermit/native/page-sweep-summary.csv)
- [2026-07-30 Hermit](../tools/rdma/results/dnet-61/20260730-184935-hermit-6.18-page-sweep/page-sweep-summary.csv)
- [2026-08-03 Hermit](../tools/rdma/results/dnet-61/20260803-231615-hermit-6.18-page-sweep/page-sweep-summary.csv)
- [2026-08-04 2 MiB Hermit 复测](../tools/rdma/results/dnet-61/20260804-195156-hermit-6.18-page-sweep/page-sweep-summary.csv)
- [Native page-sweep-summary.csv](../tools/rdma/results/dnet-61/20260808-140944-hermit-6.18-native-hermit/native/page-sweep-summary.csv)
- [Hermit page-sweep-summary.csv](../tools/rdma/results/dnet-61/20260808-140944-hermit-6.18-native-hermit/hermit/page-sweep-summary.csv)
- [2026-08-09 Native](../tools/rdma/results/dnet-61/20260809-114645-hermit-6.18-native-hermit/native/page-sweep-summary.csv)
