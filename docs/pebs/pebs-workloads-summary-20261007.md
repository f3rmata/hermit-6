# PEBS 全负载扫描 · 验收与结论（2026-10-07）

> 2026-10-07 23:35 – 10-08 14:01，在 PEBS 内核（`6.18.38-hermit-pebs #9`）上对
> memcached 之外的四类负载做全矩阵扫描。结果目录
> [20261007-233523-workloads](../../tools/rdma/results/pebs/20261007-233523-workloads/)。
> 本文承接 [pebs-performance-summary-20261006.md](pebs-performance-summary-20261006.md)。

## 1. 验收结论：全部通过

| 验收项 | 结果 |
|---|---|
| 规模 | 1323 个样本 = 441 组 × 3 重复；每组 = 9 页档 × 7 配置 |
| 完成/恢复 | `completed=true`，`restore_errors=[]` |
| 样本有效性 | 1323/1323 `exit_code=0`、`usable=true`、`validation_issues=[]` |
| 数据正确性 | checksum_errors=0、target_errors=0、fallback=0、read_ok=8192、AUC=0.820776（0 异常） |
| perf 动态上限 | 全程 2750 未变（`perf_changed_cases=0`），throttled=0 |
| 内核日志 | dmesg 前后无新增 BUG / Oops / WARNING / soft lockup |
| 采样丢失 | lost 共 70781（≈总采样 2.07 亿的 0.03%），xgboost 占 70732（54 case）、anon-1t 占 49；不影响正确性 |

配置矩阵：`off`（采样关 + 静态全页档）、`static-199/1999/19997`（采样开 + 静态决策）、
`policy-199/1999/19997`（采样开 + 策略动态决策）。页档 4–2048 KiB 共 9 档。

## 2. 核心结论：策略在这四类负载上几乎无收益

与 memcached（policy-medium **+124%** 吞吐、**−91%** RDMA 读流量）形成鲜明对比：这四类负载
的采样/策略对运行时间和 RDMA 读流量的影响都近乎为零。

时间为各页档中位数相对 `off` 的变化，三列对应 load 周期 19997 / 1999 / 199：

| 负载 | static 时间变化 | policy 时间变化 | 读流量变化(vs off) |
|---|---:|---:|---:|
| anon-1t-full | −0.05% / +0.01% / +0.23% | +0.32% / +0.44% / +0.89% | ≈0% |
| anon-1t-chunk64k | +0.06% / +0.07% / +0.18% | +0.40% / +0.60% / +0.75% | ≈0% |
| anon-8t-full | −0.10% / +0.11% / −0.35% | +0.27% / +0.63% / +0.47% | ≈0% |
| anon-8t-chunk64k | −0.16% / −0.09% / −0.03% | +0.71% / +0.08% / +0.45% | ≈0% |
| redis | +1.80% / +2.25% / +2.58% | +2.36% / +2.66% / +2.31% | +0.14% |
| ycsb | +0.03% / +0.28% / −0.73% | +0.60% / +0.00% / +1.14% | ≈0% |
| xgboost | +0.05% / +0.27% / +1.57% | −0.08% / +0.28% / +1.45% | ≈0% |

三点结论：

1. **纯采样开销（static vs off）很低**：anon ≈ 0，redis +1.8~2.6%、xgboost ≤1.6%，与
   memcached resident 阶段测到的 ~0.5–0.8% CPU 量级一致。
2. **策略收益（policy vs off）≈ 0**：读流量（load_bytes）几乎不变，策略**没有降低读放大**。
   以 anon-1t-chunk64k 的 2048k 档为例，policy-199 的 decisions 仅 2 次、large_load_pct 仍 100%，
   没有把传输粒度降到 64 KiB（本可避免 32× 读放大）。
3. **根因是访问模式**：这四类负载都是**一次性/流式顺序访问**（anon 全扫、XGBoost 按列扫描、
   Redis GETRANGE 扫描、YCSB uniform 整值读），每个 region 只访问一次；PEBS 样本在访问时才产生，
   swap-in 决策时没有"上一次访问"的样本可用，于是回退静态页档。策略收益依赖**同一 region 被反复
   访问**（时间局部性）——memcached 的热 key 满足，这四类流式负载不满足。

![逐负载吞吐对比（归一化）](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/fig4-throughput-normalized.png)

![逐负载吞吐 vs 页档](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/fig5-throughput-pagesize.png)

## 3. 与 memcached 的对照

| | memcached（20261006） | anon/redis/ycsb/xgboost（20261007） |
|---|---|---|
| 访问模式 | 热 key 反复访问 | 一次性顺序/流式扫描 |
| 策略对吞吐 | +124%（vs off） | ≈0% |
| 策略对读流量 | −91% | ≈0% |
| 纯采样开销 | ~0.5–0.8% CPU / −6.2% 饱和吞吐 | 时间 +0~2.6% |

**策略的价值高度依赖访问模式**：它解决的是"同一 region 被反复访问时按密度选传输粒度、削读放大"
的问题，对无时间局部性的流式负载无效。这不是缺陷，而是策略的适用边界。

## 4. 数据与复现

- 汇总：[aggregate.csv](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/aggregate.csv)、
  [tables.md](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/tables.md)、
  [samples.csv](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/samples.csv)
- 验收：[audit.json](../../tools/rdma/results/pebs/20261007-233523-workloads/analysis/audit.json)、
  [completion.json](../../tools/rdma/results/pebs/20261007-233523-workloads/completion.json)
- 驱动：[run_workload_matrix.py](../../tools/rdma/results/pebs/20261007-233523-workloads/source/run_workload_matrix.py)
- 图表：`analysis/fig4-throughput-normalized.png`、`fig5-throughput-pagesize.png`（吞吐对比），
  `phase-time.png`、`policy-time-change.png`、`static-time-change.png`（运行时间）
- 计划与配置：[plan.json](../../tools/rdma/results/pebs/20261007-233523-workloads/plan.json)
