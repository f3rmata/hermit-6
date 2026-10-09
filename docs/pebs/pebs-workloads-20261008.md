# PEBS 多负载采样开销与策略评估 2026-10-08

数组扫描、Redis、YCSB 和 XGBoost 的完整矩阵已完成，共 **1323 个样本、441 组配置、每组 3 次重复**。所有样本通过运行脚本的基础验收，环境恢复无错误。当前内核下，Redis 的仅采样开销约为 1.8%～2.6%，XGBoost 高频采样开销约为 1.6%；数组的扫描阶段变化较小，YCSB 不同页大小之间波动较明显。本轮 policy 结果没有展示出跨负载一致的加速。

这些结论仅对应本轮“填充/换出后恢复内存上限，再读取或训练”的测量方式。不能直接套用此前 Memcached 持续内存压力下的收益，也不能将所有通过脚本验收的样本视为无丢样、指定大页配置已生效的测量。

## 数据和实验条件

- [原始结果目录](../../tools/rdma/results/pebs/20261007-233523-workloads/)、[执行计划](../../tools/rdma/results/pebs/20261007-233523-workloads/plan.json)、[完成及恢复记录](../../tools/rdma/results/pebs/20261007-233523-workloads/completion.json)。前置 3 样本检查保存在独立目录，不混入本报告。
- [运行方法及 baseline 参数映射](pebs-workload-matrix.md)：7 种负载配置 × 9 种页大小 × 7 种采样/策略配置 × 3 次重复。
- dnet-61，`6.18.38-hermit-pebs #9`，2026-10-06 构建，RDMA rswap 后端。应用与模块记录见 [host.json](../../tools/rdma/results/pebs/20261007-233523-workloads/host.json)。
- load period 为 19997、1999、199，store period 固定 1500003；adaptive=0、force_order=0。period 是事件间隔，不是每秒采样次数，数值越小采样越密。
- 每轮只允许 4 KiB 与当前所选页大小，使用独立进程和 cgroup；采样覆盖填充、换出及读取/训练。每个负载、页大小、重复内的配置顺序随机化。
- 固定本地内存比例 70%、STHD=16、reclaim_mode=0、headroom=2048。数组为 parallel-fault 换出；其余负载填充后降低 memory.max，测量读取/训练前恢复 memory.max。

## 指标与统计方法

应用耗时取原始 CSV 中的 `swapin_scan_sec`（数组）、`bench_sec`（Redis）、`run_sec`（YCSB）和 `train_sec`（XGBoost）。**下文耗时开销不包括填充与换出的全部成本，也不等于全系统 CPU 开销。** static 组包含硬件采样和区域跟踪的成本，不是只测 PMU 中断。

对每个负载、页大小、配置先取 3 次重复的中位数，再计算：

- 采样开销：`100 × (T_static / T_off − 1)`。
- 策略附加变化：`100 × (T_policy / T_static − 1)`，比较相同采样间隔。
- 策略净变化：`100 × (T_policy / T_off − 1)`。

以上均为**正值变慢，负值变快**。汇总表再取 9 个页大小百分比的中位数；这是跨配置摘要，不是统一负载加权收益，也不是配对重复百分比的中位数。图中的 min–max 是 3 次重复范围，不是置信区间。几乎为零的变化不应解释为稳定加速；页大小间的异质性应结合逐格图和 [完整统计 CSV](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/aggregate.csv) 阅读。

## 仅采样开销与策略净变化

| 负载 | 模式 | 19997 | 1999 | 199 |
|---|---|---:|---:|---:|
| anon-1t-full | static | -0.05% | +0.01% | +0.23% |
| anon-1t-full | policy | +0.32% | +0.44% | +0.89% |
| anon-1t-chunk64k | static | +0.06% | +0.07% | +0.18% |
| anon-1t-chunk64k | policy | +0.40% | +0.60% | +0.75% |
| anon-8t-full | static | -0.10% | +0.11% | -0.35% |
| anon-8t-full | policy | +0.27% | +0.63% | +0.47% |
| anon-8t-chunk64k | static | -0.16% | -0.09% | -0.03% |
| anon-8t-chunk64k | policy | +0.71% | +0.08% | +0.45% |
| redis | static | +1.80% | +2.25% | +2.58% |
| redis | policy | +2.36% | +2.66% | +2.31% |
| ycsb | static | +0.03% | +0.28% | -0.73% |
| ycsb | policy | +0.60% | +0.00% | +1.14% |
| xgboost | static | +0.05% | +0.27% | +1.57% |
| xgboost | policy | -0.08% | +0.28% | +1.45% |

表中的 static 为采样开销，policy 为相对 off 的净变化。包括所有请求页大小；Redis/YCSB 的 2048 KiB 实际为 4 KiB 传输，不能据此推断有效 2 MiB 大页的性能。

![仅采样耗时变化](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/static-time-change.png)

Redis 的 static 在各页大小上均显示正耗时增量；XGBoost 高频档的增加也较一致。YCSB 的低频汇总虽接近零，但逐页存在约 −7% 至 +7% 的变化，不能只根据汇总数声称“没有开销”。数组的多数差异较小，3 次重复不足以支持对小幅收益的强结论。

![策略净耗时变化](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/policy-time-change.png)

中频 policy 相对同频 static 的跨页耗时变化中位数为：单线程全扫描 +0.35%、单线程 chunk64k +0.24%、8 线程全扫描 +0.16%、8 线程 chunk64k +0.32%、Redis +0.16%、YCSB −0.01%、XGBoost −0.10%。这些值表明本轮策略的额外收益很小，尚不能证明稳定优化效果。

![中频各负载绝对耗时与重复范围](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/phase-time.png)

## 采样质量和大页有效性

[审计 JSON](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/audit.json) 给出以下记录：

| 检查项 | 结果 | 解释 |
|---|---:|---|
| 基础验收通过 | 1323/1323 | 不代表所有质量条件均通过 |
| sampled 增量合计 | 207255493 | 全系统有效地址记录计数，不是目标 cgroup 独占计数 |
| lost 增量合计 | 70781 | 不能直接当作精确丢失样本数或据此计算丢样率 |
| lost 增加的样本 | 90 | 全部为 load period=199 |
| throttled 增量 | 0 | 不意味着不存在 perf 全局限流影响 |
| perf_event_max_sample_rate | 全程 2750 | 本轮未再次下调，但起点已低于此前默认值 |
| 恢复错误 | 0 | 见 completion.json |

lost 的 90 个样本分布：单线程全扫描 static/policy 分别 12/15 个；单线程 chunk64k 分别 3/6 个；XGBoost 分别 27/27 个。8 线程数组、Redis、YCSB 以及所有低、中频样本未出现 lost 增量。

当前实现的 `lost` 同时记录 ring 处理异常和 `PERF_RECORD_LOST` 记录事件，并非累加每条记录携带的实际丢失样本数。因此这里只能确认高频档存在采样质量问题，不能报告精确丢样百分比。全系统 sampled 也不能作为目标负载采样覆盖率的替代指标。

Redis 与 YCSB 在请求 **2048 KiB** 的 off 组中，大页读取字节占比中位数均为 **0%**；16～1024 KiB 则接近 100%。这个配置应标记为“大页未生效”，其性能数字只代表实际发生的 4 KiB 路径。尚未确认根因，不能直接归因于 PEBS。需要结合 THP 策略、Redis 映射和 smaps 的实际页分布继续排查。

## 策略为何尚未体现明显收益

本轮策略确实有非零 order 决策记录，不能简单解释为“policy 完全没有运行”。在每个负载的 81 个 policy 样本中，决策计数有增量的样本数分别为：XGBoost 81、YCSB 81、Redis 79，四种数组配置则为 23、20、15、11（依次为单线程全扫描、单线程 chunk64k、8 线程全扫描、8 线程 chunk64k）。这些是整轮计数，不能据此证明每个应用测量阶段都获得了充分的策略覆盖。

可验证的限制有两点：一是每轮候选集合仅有 4 KiB 和所选页大小；二是读取/训练阶段已恢复 memory.max，不是此前 Memcached 的持续内存压力场景。访问阶段、候选粒度及历史覆盖不同，使本轮结果不能用来否定或推广 Memcached 的收益。

例如，2048 KiB 下单线程 chunk64k 的 off 组仍有约 32 倍读放大；中频 policy 的 RDMA 读取字节中位数与 off 没有变化。这支持“该数组场景尚未得到有效流量优化”，但不足以单独确定原因是采样不足、访问历史不匹配还是策略选择受限。

## 与旧 baseline 及 Memcached 结果的关系

本轮负载参数映射到 [2026-09-24 pre-PEBS baseline](../rdma/prepebs-baseline-20260924.md)，但本文的性能百分比全部采用**当前内核的 off 组**作为直接对照。尚未把旧内核与新内核的 off 数据按应用版本、数据规模、实际页大小、内存驻留量逐项验收并量化比较，不能将跨内核差异全部记为采样成本。

[2026-10-06 Memcached 报告](pebs-performance-summary-20261006.md) 中约 +124% 的饱和吞吐收益属于那一套内存压力、THP 与候选 order 配置。本轮结果说明该收益没有自动推广到数组、Redis、YCSB 和 XGBoost；报告应同时保留两类证据。

后续优先顺序：先排查 Redis/YCSB 2 MiB 大页未生效；再用阶段差分核对数组的采样、决策及 RDMA 字节变化；对有 lost 的高频组修复采样质量后复测；最后补充分阶段 CPU、填充/换出成本与旧 baseline 的严格匹配比较。在这些检查完成前，1999 可作为本轮未出现 lost 增量的复测起点，不应宣称为全场景最优采样间隔。

## 复现统计与图表

```sh
python3 tools/rdma/pebs/summarize_workload_matrix.py \
  tools/rdma/results/pebs/20261007-233523-workloads
```

脚本从原始 case/status JSON 与负载 CSV 重新生成 `analysis/samples.csv`、`aggregate.csv`、`audit.json`、`tables.md` 和三组 PNG/PDF，不修改原始测量文件。PDF 下载：[仅采样](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/static-time-change.pdf)、[策略净变化](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/policy-time-change.pdf)、[绝对耗时](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/phase-time.pdf)。
