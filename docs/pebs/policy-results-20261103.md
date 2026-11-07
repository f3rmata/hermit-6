# Hermit 4 KiB 策略矩阵结果与设计分析

> 更新：2026-11-03。实测内核为 `6.18.38-hermit-pebs #14`，运行于 dnet-61。
> 本文分析 Memcached、Redis 和 YCSB 的 567 个完整测试样本。
> 图表沿用 [上一轮策略分析](policy-design-review.md) 的展示方式；两轮内核与实验条件不同，不直接合并成绩。

新的 **4 KiB 预测、逐页记账和 SG 读取合并路径已经运行**，但本轮结果没有体现普遍的吞吐和延迟收益。policy 能显著减少部分配置的过度读取：在 2 MiB 分配上限、采样周期 1999 下，Memcached 和 Redis 的配对吞吐分别提高 **320.16%** 和 **94.63%**。同条件的 YCSB 吞吐却下降 **16.28%**，读 p99 增加 **37.47%**。

收益也不等同于合并成功。Redis 在上述配置中读字节/op 减少 **93.72%**，但读 WR/op 增加 **306.40%**；YCSB 的读 WR/op 增加 **736.84%**。Memcached 的 policy 几乎全部使用 4 KiB 读请求，在 4 KiB 上限下吞吐反而下降 **75.16%**。因此，当前策略的主要收益来自抑制过度读取，仍需解决基础页路径开销和连续负载的请求数量问题。

本轮应用测量阶段的 `cold_observations` 和 `batch_write_wrs` **全部为零**。已有数据不能支持“冷页检测和换出合并已带来收益”的结论。

## 测试范围与比较口径

| 项目 | Memcached | Redis | YCSB / Redis |
|---|---|---|---|
| 完成样本 | 189 | 189 | 189 |
| 数据与访问 | 80 万条、1 KiB value；90% GET、10% update | 16 GiB 大 value；每个 2 MiB value 顺序读取 64 KiB | 8192 条、每条单字段 2 MiB；uniform、只读整条记录 |
| 测量工作量 | 预热 30 s，测量 120 s，`qps=0` | 8192 次 range GET | 8192 次 READ |
| 并发与绑核 | 服务端 0–7；客户端 16–31；连接参数 32，估算共 512 条连接 | 单客户端、单服务端；服务端 0–7，客户端 8–15 | 服务端 0–7，客户端 8–15 |
| 内存限制 | `memory.high=640 MiB`，`memory.max=2048 MiB` | 装载后驻留量的 70%，作为固定 `memory.max` 保持到本次测量结束 | 同 Redis |
| 回收配置 | `apt_reclaim=Y`、`reclaim_mode=1`、headroom 16384 页 | `apt_reclaim=N`、`reclaim_mode=0`、headroom 2048 页 | 同 Redis |

每个负载都覆盖 4、16、32、64、128、256、512、1024、2048 KiB 九档**最大分配页大小**；每档保留更小的合法页面档位。每个重复包含一个 off，以及 static/policy 各三个 load 采样周期：19997、1999、199。store 周期为 1500003，每个组合重复三次。

- **off**：关闭 PEBS 采样和策略，仍使用 Hermit 远端后端。
- **static**：开启采样，不开启动态策略。
- **policy**：开启采样和新的换入、回收策略。

同一负载内三个模式的后端掩码均为 `0x3fd`，`pebs_force_order=0`，`bypass_swapcache=Y`。off 是策略关闭的对照，不是原生 Linux 本地 swap。Memcached 与 Redis/YCSB 使用不同的回收和内存限制配置，不能把跨负载的绝对吞吐差异归因于策略。

本文的百分比按**相同负载、页面上限、重复编号**配对 off 后计算，再取三次比例的中位数。主图默认使用 load 周期 1999；折线为各组吞吐或延迟的中位数，阴影为最小至最大范围，不是置信区间。比例中位数不必等于两个组中位数之比。

Memcached 每轮固定运行 120 s，完成操作数随吞吐变化。它的原始 `paired.csv` 按总 WR/总字节计算变化，不能直接代表完成相同工作量的成本。本文重新计算：

```text
读 WR/op       = 应用测量阶段 read_wrs / 完成操作数
读 KiB/op      = 应用测量阶段 read_bytes / 完成操作数 / 1024
实际平均读大小 = 三次重复的 read_bytes 总和 / read_wrs 总和 / 1024
```

Memcached 的操作数包含 GET 和 update；Redis 为完成的 range GET；YCSB 为完成的 READ。`pswpin/pswpout` 统计 4 KiB 页数量，WR 统计实际 RDMA 请求次数，二者不能互换。

## 吞吐和尾延迟

![三种负载在九档页面上限下的吞吐中位数及范围](assets/20261103-policy-analysis/throughput-period-1999.png)

2 MiB 上限、周期 1999 的核心结果如下。WR 与流量均按完成操作数归一化；百分比均为 policy 相对同档 off 的配对变化。

| 负载 | off 吞吐中位数 | policy 吞吐中位数 | 吞吐变化 | 读 WR/op 变化 | 读 KiB/op 变化 | 读 p99 变化 |
|---|---:|---:|---:|---:|---:|---:|
| Memcached | 11,571 ops/s | 48,778 ops/s | +320.16% | −26.69% | −98.42% | −43.82% |
| Redis | 4,432.78 GET/s | 8,625.86 GET/s | +94.63% | +306.40% | −93.72% | 未测量 |
| YCSB / Redis | 493.35 ops/s | 413.11 ops/s | −16.28% | +736.84% | −40.84% | +37.47% |

**Memcached 的收益依赖页面配置。** 在 4 KiB 上限下，off 达到 230,831.5 ops/s，policy 为 58,555.0 ops/s；配对吞吐下降 75.16%，读 p99 从组中位数 5.62 ms 增至 32.98 ms。在 64 KiB 上限下，policy 吞吐提高 26.75%，但读 p99 仍增加 66.59%。不能仅凭吞吐改善认定延迟同时改善。

**Redis 的 policy 比较稳定，但没有超过本轮观察到的最佳 off 配置。** 周期 1999 下，policy 在 2 MiB 上限达到约 8626 GET/s；off 在 128 KiB 上限达到约 10840 GET/s。policy 对 1–2 MiB 上限的帮助，主要发生在 off 开始大量过度读取之后。

**YCSB 在所有九档上限下都慢于同档 off。** 在 4 KiB 上限下，policy 虽然把读 WR/op 降低 39.82%，吞吐仍下降 27.68%，读 p99 增加 198.47%。减少请求数量本身也不足以抵消当前策略路径的其他成本。

![Memcached 和 YCSB 的读 p99](assets/20261103-policy-analysis/read-p99-period-1999.png)

图中每点是三次运行各自 p99 的中位数，不是将全部请求合并后重算的 p99。Redis range-read harness 未记录延迟分位数，不能由 GET/s 推算它的 p99。

## 字节减少与请求减少是不同的结果

![每次应用操作的实际读请求数和读取字节](assets/20261103-policy-analysis/requests-and-bytes-period-1999.png)

图中纵轴采用对数刻度；每个负载独立显示其请求数和字节范围。

Memcached 在 2 MiB 上限下，读 WR/op 约从 1.00 降至 0.73，写 WR/op 的配对降幅为 38.68%；读字节/op 则从组中位数约 190,347 B 降至 2,997 B。每次请求传输量大幅缩小，使字节变化远大于请求次数变化。这里的收益不需要以“大批量读合并成功”为前提。

Redis 在 2 MiB 上限下，读 WR/op 约从 0.86 增至 3.50，而读字节/op 从约 483,332 B 降至 30,465 B。它用更多小请求换取了更少的无效读取，最终吞吐提高。到 64 KiB 上限时，原路径已接近实际访问范围；policy 读字节/op 减少 46.68%，但读 WR/op 增加 332.65%，吞吐下降 21.68%。

YCSB 读取整条 2 MiB 记录，大请求具有直接用途。在 2 MiB 上限下，off 的读 WR/op 约为 1.55，policy 约为 13.05。即使读字节/op 减少 40.84%，更高的请求频率及额外软件工作仍与吞吐下降、尾延迟上升同时出现。现有计数不能把二者各自的时间贡献分离出来。

## 实际读取大小与合并效果

![实际平均 RDMA 读取大小随分配上限变化](assets/20261103-policy-analysis/actual-read-size-period-1999.png)

实际平均大小按三个重复合并计算，使用 `order` 和 `pages batch_*` 两张 WR 表的总量；相同大小的两类请求相加，12、20、24、52 KiB 等非二次幂请求也保留。

在 2 MiB 上限、周期 1999 下：

- **Memcached policy：约 4.0002 KiB/WR。** 4 KiB 请求贡献 99.9924% 的读字节，实际读取合并极少。
- **Redis policy：约 8.4991 KiB/WR。** 4、12、24 KiB 分别贡献约 29.42%、35.29%、35.29% 的读字节，SG 合并确实发生，但平均请求仍小于一次 range GET 的 64 KiB。
- **YCSB policy：约 49.0082 KiB/WR。** 64 KiB 请求贡献 93.82% 的读字节，连续读取能形成较大批次，但批次仍明显小于整条记录。

![policy 实际读字节的粒度分布](assets/20261103-policy-analysis/policy-read-granularity-period-1999.png)

**4 KiB 分配上限下出现更大的 RDMA 读请求是符合设计的。** 多个基础页在满足证据和布局条件后可以组成一个 SG 请求；分配上限不直接限定网络请求大小。

当前工作区中 [`HERMIT_BATCH_MAX`](../../linux-stable/include/linux/hermit_backend.h) 为 16，单批最多 16 个基础页，即 64 KiB。[`rswap_backend_submit_batch()`](../../remoteswap/client/rswap_rdma_ops.c) 还会按设备 SGE 能力、远端连续性及边界进一步切分。图中的 YCSB 大量 64 KiB 请求与这个软件上限相符；仅凭当前统计不能区分每一种拆分原因。

应保持 4 KiB 的预测、有效性和容量记账，同时让已经确认需要的连续页有机会组成更大的提交范围。提高批次容量必须同时遵守设备能力和远端布局；换出 extent 大小不能作为额外预取页面的证据。

## 换出策略尚未形成可观测的冷页合并收益

下表累计所有页面档位、三个周期、三次重复中 **policy 应用测量阶段**的计数。计数表示事件或累计候选，不是不同物理页数，也不能据此直接计算预测命中率。

| 计数 | Memcached | Redis | YCSB / Redis |
|---|---:|---:|---:|
| `observed_4k` | 8,385,392 | 0 | 0 |
| `cold_observations` | 0 | 0 | 0 |
| `invalid_epochs` | 913,339,786 | 0 | 64,847,107 |
| `predicted_4k` | 85,203 | 2,945,973 | 113,718,423 |
| `refault_4k` | 7,923,563 | 0 | 15,044 |
| `ranked_candidates` | 826,813,994 | 0 | 19,974,830 |
| `batch_read_wrs` | 355,211,039 | 2,893,934 | 18,567,915 |
| `batch_write_wrs` | 0 | 0 | 0 |

Redis/YCSB 准备阶段同样没有增加 `cold_observations`，也没有 SG 批量写入。Redis 测量阶段几乎没有换出，因此其应用阶段冷页计数为零并不意外；准备阶段大量 `invalid_epochs` 和排序活动仍说明“路径执行过”不能替代“确认了冷页”。Memcached 在持续内存压力下有大量有效观察、排序和 refault，却没有产生冷确认，尤其值得定位。

代码给出两个需要区分的事实：

1. [`hermit_policy_observe()`](../../linux-stable/mm/hermit_policy.c) 只接受满足独占等条件的基础页，要求两个有效空观察周期才能确认冷页；large folio 等不符合条件的情况会记入 `invalid_epochs`。该计数不等同于 PEBS 样本丢失。
2. [`vmscan.c`](../../linux-stable/mm/vmscan.c) 中的 SG 换出批次要求 `hint.cold && hint.owner`。当前没有冷确认、也没有 `batch_write_wrs` 增长，尚无冷页批量换出的实测收益。

`ranked_candidates` 在选择并移动回收候选时增长，不能解释为冷页数；`predicted_4k` 累计预测的额外基础页，不保证都提交成功或后来被使用。旧 `decisions_orderN` 在新路径中为零也不代表策略失效，当前的 SG 读计数和新策略计数已证明新路径运行。本轮计数没有将顺序流预测与 PEBS 足迹预测的贡献分开，不能仅凭 `predicted_4k` 断定收益来自哪一种证据。

为何没有形成两个有效空周期，还不能从这些汇总计数唯一确定。当前代码的跟踪表最多容纳 8192 页，约 32 MiB 的基础页身份；相对于这些工作集，表项轮换、代际失效、观察窗口重启和热访问都可能阻止冷证据积累。需要把这些原因分开记录，才能判断应改容量、生命周期还是确认条件。

## 策略开销和采样周期

![三个采样周期下 policy 相对配对 off 的吞吐变化](assets/20261103-policy-analysis/policy-vs-off-periods.png)

三个周期下的主要趋势一致：Memcached 在小上限下退化、大上限下改善；Redis 在中等上限下退化，在 1–2 MiB 上限下改善；YCSB 各档均退化。单纯改变 PEBS 采样频率没有逆转这些趋势。

Memcached 4 KiB 上限是值得优先分析的对照：周期 1999 的 static 吞吐相对 off 下降 10.24%，policy 下降 75.16%，而 policy 的读 WR/op 和读字节/op 只增加约 2.81%。额外退化与策略执行路径相关，不能全部解释为采样本身或网络读流量增加。

当前新路径涉及逐页 swapcache 分配与记账、批次提交和轮询、流状态更新、refault 跟踪，以及回收侧的 rmap、候选排序和观察周期 TLB flush。这些都是代码中可确认的工作，**其各自耗时尚未由本轮测试分离**。因此，优先方向应是减少低证据情况下的额外工作，并区分换入路径与回收路径的成本，而不是直接认定某一把锁或某个函数是主因。

在本轮观察到的配置中，周期 1999 的最佳 policy 吞吐相对最佳 off 吞吐仍分别低约 74.63%（Memcached）、20.42%（Redis）、17.58%（YCSB）。这只是扫描到的最佳配置比较；Redis/YCSB 的绝对内存预算也随档位变化，不能将跨档差异全部归因于页面大小。

## 结果质量与解释边界

567 个完整 KV 样本均标记有效。原始 WR 快照、`granularity.csv` 和 `summary.csv` 的读写请求数及字节数逐项一致；应用阶段策略计数也全部一致。没有记录到这些 KV 样本的 OOM、后端错误、Memcached eviction、miss 或 skipped 请求；两套驱动均完成环境恢复。

还存在以下实际条件：

- **内存预算并非全矩阵统一冻结。** Redis 的 `memory.max` 范围为 12148–14419 MiB，YCSB 为 11547–14424 MiB。同档配对内，Redis 最大差异约 4.46%，162 个配对中有 16 个超过 1%；YCSB 最大差异约 0.035%。主图保留这些结果，不能将小幅差异解释为确定收益。
- **采样速率上限发生漂移。** Memcached 全程记录为 63750；Redis/YCSB 从 63750 下降到 51000、40500，最后到 32500。三个样本内发生上限变化，三个非 off 样本与其配对 off 的起始上限不同。主图周期 1999 的配对起始上限一致，但不同重复仍可能处于不同上限。
- **节流与丢样分别记录。** Memcached 有一个样本的 `throttled` 增量为 2；Redis/YCSB 有七个样本累计增量 24，均为周期 199。`lost` 均为零。`throttled` 同时包含 throttle/unthrottle 事件，不能当作同样数量的独立节流区间。
- **Memcached 客户端 CPU 接近其绑核容量。** 观测范围为 99.81%–99.87%。这意味着结果不能直接当作已经隔离客户端影响的服务端峰值；忙轮询也可能贡献 CPU 占用，不能仅凭这一数值认定客户端是唯一瓶颈。
- **计数是整机后端快照。** 应按应用阶段差分、同条件配对解释。本文不把 `predicted_4k` 当作有效命中数，也不把旧决策计数为零当作新路径未执行。

以上核查及原始文件哈希分别保存在 [audit.json](assets/20261103-policy-analysis/audit.json) 和 [evidence.json](assets/20261103-policy-analysis/evidence.json)。代码解释参照当前工作区；运行内核构建未归档对应的内核源码哈希，因此源码分析用于提出可核验的解释，不能替代运行时因果证据。

## 新应用矩阵单独处理

此前的 `hermit-policy-matrix-20261102/results` 不是完整结果：计划 5670 个测试单元，记录了 2566 个，其中 2401 个成功、165 个失败，3104 个未完成。

165 个失败单元均以 `exit_code=-9` 退出，并且对应 `memory.events` 的 `oom_kill` 增量大于零。失败涉及 sort、SQLite query/update、GAPBS BFS/PageRank，也涉及 off、static 和 policy。不能将它们当作有效吞吐样本，也不能丢掉失败后把幸存结果推广为完整矩阵结论。

本文主图只使用完整的 567 个 KV 样本。新应用矩阵的覆盖情况和失败明细保存在 [incomplete-app-matrix.csv](assets/20261103-policy-analysis/incomplete-app-matrix.csv) 与 [app-failures.csv](assets/20261103-policy-analysis/app-failures.csv)。

## 下一轮设计优先级

1. **先降低 4 KiB 策略路径的成本。** Memcached 的 4 KiB 对照已经显示：接近的传输字节和请求数没有带来接近的吞吐。应分别量化 swapcache/逐页安装、状态锁、rmap/排序和观察 TLB flush 的成本，对没有有效证据的候选避免重复处理。
2. **让已确认的连续页形成更大请求。** YCSB 的读字节主要集中在 64 KiB 批次，而整条记录有 2 MiB。保持逐 4 KiB 验证和记账，扩大有证据的待提交集合，并在后端能力与远端布局约束内合并；不以换出批次大小推定读取需求。
3. **先打通冷证据生命周期，再评价换出合并。** 分别统计不支持的 folio、独占性失败、表项淘汰、代际重置、未满观察间隔和空周期被访问打断。`cold_observations=0` 时，排序次数再多也不能证明冷页识别有效。
4. **下一轮固定同档配对的绝对内存预算。** 继续保留 off/static/policy、页面上限和采样周期维度；同时报告吞吐、p99、WR/op、字节/op及失败率。对已经 OOM 的新应用单元，先确认预算与退出原因，再解释策略收益。

## 数据与图表复现

原始结果目录位于 dnet-61：

```text
/home/xwz/memcached-policy-20261102-170832
/home/xwz/redis-ycsb-policy-20261103-114407
/home/xwz/hermit-policy-matrix-20261102/results
```

随文保存 [567 个标准化样本](assets/20261103-policy-analysis/samples.csv)、[应用阶段实际请求直方图](assets/20261103-policy-analysis/application-granularity.csv)、[按操作数归一化的配对结果](assets/20261103-policy-analysis/paired-per-operation.csv)、[各组汇总](assets/20261103-policy-analysis/group-summary.csv) 和 [测试配置](assets/20261103-policy-analysis/source-context.json)。六张图均提供同名 PNG 与 SVG。

在仓库根目录执行以下命令可从随文 CSV 重建图表和配对统计，不连接服务器、不执行负载：

```bash
python3 docs/pebs/assets/20261103-policy-analysis/render.py
```
