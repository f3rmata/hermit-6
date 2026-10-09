# PEBS 结论汇总

<!-- **PEBS 采样性能开销很低，但是现在的策略收益高度依赖访问模式**   -->
<!-- 对热区域反复访问的 memcached 可以提升 +124% 的吞吐，对一次性流式扫描的数组/xgboost/redis/ycsb 则几乎为零。 -->

<!-- ## 1. PEBS 在 Hermit 里做什么 -->

<!-- Hermit 用 RDMA 把内存换出到远端、缺页时再读回。每次 RDMA 写/读都有约 7µs 的固定开销， -->
<!-- 所以传输粒度越大越划算（4 KiB 只有 ~0.6 GiB/s，2 MiB 能到 10+ GiB/s）；但如果一个 2 MiB -->
<!-- folio 里只有一小片被真正访问，整块读回就是读放大。 -->

<!-- PEBS 用硬件采样 `MEM_LOAD_RETIRED.L3_MISS`（读）和 `MEM_INST_RETIRED.ALL_STORES`（写） -->
<!-- 两个事件，按 memcg 把样本聚合成每个 2 MiB region 的"触及位图"，再用代价模型 -->
<!-- `c(o) = 固定开销/2^o + 读放大×4KiB/带宽` 为每次传输在线选 folio 粒度（order 0/2–9）， -->
<!-- 替代原来全局静态的传输页大小设置（`remote_order_mask`）。没有样本、或 `pebs_enabled=0` 时，回退静态页档， -->
<!-- 数据路径与没有 PEBS 的内核完全等价。 -->

## 采样开销

只开采样、不改决策（static vs off）时，采样线程、硬件采样、ring 消费和 region 维护加起来
的成本很小：无换页时整机 CPU/请求只多 0.5%–0.8%，读尾延迟绝对增量 1.3–1.6 µs。
换页饱和负载下，纯采样让 memcached 吞吐从 63.4k 掉到 59.5k QPS，约 −6%。

![纯采样开销](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/fig2-overhead.png)

### 采样频率影响

三档固定频率（low≈820 / medium≈900 / high≈1500 samples/s）对原始吞吐几乎无差异
，无换页时 memcached QPS 都保持在 30000 附近，CPU 开销保持0.5%–0.8%。

采样频率决定了策略能拿到多少有效样本。
下图中横轴是实际采样率、纵轴是 RDMA 读流量（绝对值，越低越好）：
adaptive 的采样率是 high 的 8 倍多（~12800 vs ~1500/s），但读流量没有进一步下降，说明**收益达到覆盖阈值后即饱和，不随采样率线性增长**。

![采样率与 RDMA 读流量](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/fig3-frequency.png)

<!-- ### 为什么提高频率几乎不影响性能 -->

<!-- 三层原因叠加： -->

<!-- **第一层：采样率其实没涨多少。** “频率档”改的是 load 事件（L3 miss）的周期 -->
<!-- 19997→1999→199（100 倍），但实测 samples/s 只从 ~820 涨到 ~1500，只有 1.8 倍。 -->
<!-- 因为 store 事件周期固定（1500003），贡献了约 815 samples/s 的恒定基线——这个负载每秒 -->
<!-- 约 12 亿次 store 指令 ÷ 1500003。load 事件受限于负载本身 ~10 万/s 的 L3 miss 率， -->
<!-- 周期 19997→199 时只贡献 ~5→507 samples/s，被 store 基线稀释。所以拧的旋钮只控制 -->
<!-- 采样里很小的一部分。 -->

<!-- **第二层：即便涨到 1500/s，绝对开销也可忽略。** 每个样本约 1µs（PEBS 硬件写 32 字节 -->
<!-- 记录 + PMI 中断 + `hermit_pebsd` 每 2ms 批量排空 ring + region 位图更新），1500/s 总共 -->
<!-- 才 ~1.5ms/s ≈ 0.15% 单核 CPU。从 820 提到 1500 多出来的 ~700 个样本只多 ~0.05% CPU—— -->
<!-- 实测三档 CPU/请求开销 +0.73% / +0.52% / +0.79% 彼此差异无统计意义，等于没变。 -->

<!-- **第三层：固定 QPS 下开销被空闲 CPU 吸收。** resident 阶段固定 30000 QPS（非饱和）， -->
<!-- 系统本就有 headroom，采样多花的 CPU 被空闲核吃掉，QPS 纹丝不动，只在 CPU 占用上露出 -->
<!-- 0.5–0.8% 的尾巴。采样成本真正变成吞吐损失只有饱和时——memcached 不限速下 static-medium -->
<!-- 相对 off 掉 −6.2%；且饱和只测了 medium 一个频率，“频率–饱和吞吐”曲线尚未测过。 -->

## memcached 吞吐

在 memcached（800k × 1 KiB，热 key 反复访问）的不限速吞吐上，策略的收益是最鲜明的结果。
下图从左到右：只允许 4 KiB 的原始设置（44.5k）、放开大 folio 但不采样（63.4k）、开采样但
静态决策（59.5k，即纯采样成本）、开采样 + 策略（145.3k）。

策略相对纯静态 off 是 **+124%**（相对同频 static 是 +136%，相对只允许 4 KiB 的原始设置是
+223%），同时 read p99 减半、单位请求 CPU 降 ~58%、RDMA 读流量降 ~92%。收益来自于策略按访问密度选定合适的传输粒度。

![不同策略下 4 KiB 与 64 KiB 读取字节占比](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/fig-wr-size.png)

<!-- 上面是“传输 WR 粒度”（large_wr_read_pct）。下面这张是“实际分配的 folio 大小” -->
<!-- （换入侧 large_load_pct）。关键差别在 off-original：它的 folio 仍分配 64 KiB（~98%）， -->
<!-- 只是 remote_order_mask=0x1 把传输切成 4 KiB 基页——所以它慢在“传输粒度”，不是“分配粒度”。 -->
<!-- policy（尤其 high/adaptive）则连换入的 folio 分配本身都缩到 4 KiB（大 folio 占比降到 ~11%）。 -->

<!-- ![不同策略下实际分配的 folio 大小对比](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/fig-folio-size.png) -->

![memcached 不限速吞吐](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/fig1-throughput.png)

## 流式负载上策略优化不明显

把同样的 off / static / policy 矩阵搬到另外四类负载（anon 数组扫描 4 组、Redis、YCSB、
XGBoost，共 1323 个样本），结论完全反转：所有配置的吞吐都贴着 1.0（下图为相对 off 的
归一化吞吐，各负载 × 各配置都在 ±3.5% 以内），策略没有降低读放大。

![逐负载吞吐对比（归一化）](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/fig4-throughput-normalized.png)

逐页档的绝对吞吐曲线中：off、static-199、policy-199 三条线几乎重合。

![逐负载吞吐 vs 页档](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/fig5-throughput-pagesize.png)

根因是**访问模式**：这四类负载都是一次性/流式顺序访问，每个 region 只碰一次。PEBS 样本
在访问时才会产生，swap-in 决策时没有"上一次访问"的样本可用，
于是回退静态页档。策略的收益依赖同一region 被反复访问

<!-- ## 数据与图表 -->

<!-- - memcached 三阶段（2026-10-06）：[完整统计](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/SUMMARY.md) · -->
<!--   [简明重析](pebs-performance-summary-20261006.md) -->
<!-- - 全负载扫描（2026-10-07）：[验收与结论](pebs-workloads-summary-20261007.md) · -->
<!--   [aggregate.csv](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/aggregate.csv) -->
<!-- - 图表脚本：[plot_pebs_clean.py](../../tools/rdma/pebs/plot_pebs_clean.py) · -->
<!--   [plot_pebs_workloads.py](../../tools/rdma/pebs/plot_pebs_workloads.py) -->
