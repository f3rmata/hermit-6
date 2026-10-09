# PEBS 性能评估 · 简明重析（2026-10-06）

后续结果：[2026-10-08 数组、Redis、YCSB、XGBoost 评估](pebs-workloads-20261008.md)。本文的吞吐收益属于 Memcached 实验条件；后续多负载矩阵尚未出现一致加速，不能把本文收益直接推广到其他工作负载。


> 对 `docs/pebs/pebs-performance-20261006.md` 同一份数据的重析与重排，聚焦四个问题：
> ① PEBS 开/关的吞吐、② 不同采样频率的影响、③ 纯采样开销、④ 优化策略的收益。
> 数值全部取自 [SUMMARY.md](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/SUMMARY.md) 与各阶段 `summary.csv`，
> 中位数为点估计、方括号为 5 次重复的 min–max（不是置信区间）。

## 0. 先看结论

| 问题 | 一句话回答 |
|---|---|
| PEBS 开 vs 关（吞吐） | 采样本身让饱和吞吐 **−6.2%**（static-medium vs off）；但叠加策略后吞吐 **+124%**（vs off） |
| 采样频率影响（吞吐） | 无换页时三档频率的原始吞吐**几乎无差异**（开销 <1% CPU）；频率真正决定的是**策略能提取多少收益**——低频样本不足使策略近乎失效，高频/中频才能拿到 −86% 以上的 RDMA 流量削减 |
| PEBS 纯开销 | 无换页：**CPU/请求 +0.5%～+0.8%**，read p99 绝对增量 **1.3–1.6 µs**；换页饱和负载下纯采样吞吐 −6.2% |
| 策略收益 | 饱和吞吐 **+136%**（vs 同频 static）、**+124%**（vs off）、**+223%**（vs 只允许 4 KiB 的原始设置）；固定 QPS 下 CPU/请求 −16～−18%、RDMA 读流量 −86～−91% |

---

## 1. 术语与对照关系

这次矩阵里同一组硬件（dnet-61，Memcached 800k 项 × 1 KiB value，64 KiB THP）跑了四个
“配置族”，理解它们的差异是读懂全部数字的前提：

| 配置 | 采样 | 决策 | 含义 |
|---|---|---|---|
| `off-original-mask` | 关 | 只允许 4 KiB（0x1） | 最原始基线，每个 4 KiB 页一个 WR，写放大最严重 |
| `off` | 关 | 允许全部页档（0x3fd） | 只放开大 folio、**不采样**的基线 |
| `static-{low,medium,high,adaptive}` | **开** | 静态全页档 | PEBS 采样开着，但**决策仍是静态页档**（对照采样纯开销） |
| `policy-{low,medium,high,adaptive}` | **开** | **动态策略** | 采样 + 用样本在线选传输粒度（本文说的“优化策略”） |

- **采样频率档**：`low/medium/high` 只改 load 事件周期（19997 / 1999 / 199），store 周期固定 1500003；
  `adaptive` 同时改 load/store 周期。周期是硬件事件数阈值，不是 Hz。
- **实测采样率**（samples/s）：low ≈ 820，medium ≈ 870–1000，high ≈ 1300–2200，adaptive ≈ 12800。
- **策略（policy）做什么**：PEBS 采样每个 2 MiB region 的访问密度，用代价模型
  `c(o)=固定开销/2^o + 读放大×4KiB/带宽` 为每次 RDMA 传输选 folio 粒度（order 0/2–9），
  替代全局静态页档。详见 [pebs-order-policy.md](pebs-order-policy.md)。

**“策略收益”的正确读法**：`policy vs static`（同频）才等于“我在 PEBS 基础上加的优化”带来的收益；
`policy vs off` 是“采样 + 策略”相对纯静态的净变化；`policy vs off-original-mask` 是相对最原始实现的提升。

---

## 2. 结论一：PEBS 开 vs 关 —— 应用吞吐

不限速（saturation）阶段直接回答这个问题。唯一被纳入不限速测试的采样配置是 `medium`（在看结果前选定，避免事后挑档）。

| 配置 | QPS 中位数 [范围] | read p99 µs | CPU µs/请求 | RDMA read KiB/请求 |
|---|---:|---:|---:|---:|
| off-original-mask（只允许 4 KiB） | 44,492 [42,009–47,739] | 12,848 | 168.88 | 49.64 |
| off（全部页档，不采样） | 63,424 [60,228–65,582] | 10,156 | 118.41 | 43.58 |
| static-medium（PEBS 开·静态） | 59,489 [52,372–64,388] | 10,773 | 122.88 | 44.04 |
| **policy-medium（PEBS 开·策略）** | **145,338** [137,874–152,345] | **5,444** | **48.90** | **3.59** |

**开采样但不动决策（static vs off）**：吞吐 −6.2%（配对中位数，范围 −20.07%～+3.49%）。
这就是 PEBS 硬件采样 + ring 消费在饱和负载下的净成本——它把吞吐从 63.4k 拉到 59.5k QPS。

**开采样 + 用策略（policy）**：吞吐 145k QPS，是纯静态 off 的 **2.24 倍**、是同频 static 的 **2.36 倍**。
换言之：PEBS 采样单独看是 −6%，但它为策略提供的信息把吞吐拉高了 +124%（相对 off）、
+136%（相对 static），净效果远大于采样成本本身。

![不限速吞吐](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/fig1-throughput.png)

---

## 3. 结论二：不同采样频率对吞吐的影响

这里要把“原始吞吐”和“策略收益”分开看，否则会得出误导性结论。

### 3.1 对原始吞吐：几乎无影响（无换页、固定 QPS）

resident 阶段（无换页、QPS 固定 30000）是“纯采样”场景，三档频率都能轻松打满 30000 QPS：

| 频率档 | samples/s | QPS | read p99 µs | CPU µs/请求 |
|---|---:|---:|---:|---:|
| off | 0 | 29,984 | 23.30 | 142.03 |
| low | 821 | 29,955 | 24.60 | 143.07 |
| medium | 868 | 29,966 | 24.80 | 142.79 |
| high | 1324 | 29,996 | 24.90 | 143.18 |

QPS 三档都锁在 ~30000（固定 QPS 阶段本来就测不出吞吐差异）；频率差异只体现在
CPU（+0.52%～+0.79%）和尾延迟（+6.0%～+7.3%，绝对增量 1.3–1.6 µs），且幅度很小、
基本平坦，**不存在“采样频率越高吞吐越低”的单调关系**。

### 3.2 对策略收益：频率是决定性的（换页、固定 QPS）

真正被频率影响的，是策略能从样本里提取多少信息。pressure 阶段（固定 30000 QPS + 换页）
中，RDMA 读流量（KiB/请求）随采样率变化剧烈——**不采样基线 off 为 50.90 KiB/请求**：

| 频率档 | samples/s | RDMA 读流量 static（KiB/请求） | RDMA 读流量 policy（KiB/请求） |
|---|---:|---:|---:|
| low | ~820 | 48.27 | 42.01 |
| medium | ~900 | 55.94 | 7.93 |
| high | ~1500 | 54.35 | 4.64 |
| adaptive | ~12800 | 50.89 | 4.66 |

- **static 系列纹丝不动**：不管采样率多高，读流量都停在 ~48–56 KiB/请求（≈ off 的 50.90）——
  光开采样但不改决策，读放大不会下降。
- **policy 系列随采样率骤降**：low 42 → medium 7.9 → high 4.6 → adaptive 4.66，读流量降低约 9 倍。
  low 样本太稀疏（unknown 占比 ~22%），策略退化到只降一点点；medium 及以上达到覆盖阈值，
  读流量稳定在 4.6–7.9 KiB/请求。
- **收益饱和**：`adaptive` 的采样率（~12800/s）是 `high`（~1500/s）的 8 倍多，但读流量没有进一步下降——
  **采样覆盖足够后收益不再随采样率线性增长**。

> 结论：选采样频率的判据不是“哪个开销更低”（三档开销都 <1%），而是“哪个能给策略足够覆盖”。
> low 不足以支撑策略；medium/high/adaptive 都能支撑。就本次负载，high 在压力场景尾延迟最稳（read p99 612 µs）。

![采样率与 RDMA 读流量](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/fig3-frequency.png)

> low/medium/high/adaptive 背后的采样周期实现（周期表、固定档覆盖、2 秒自适应窗口）见文末「附录 8」。

---

## 4. 结论三：PEBS 纯采样开销

“纯采样开销”= static vs off（同页档、同静态页档，唯一区别是采样开关）。

### 4.1 无换页（resident，固定 30000 QPS）

| 频率档 | CPU/请求 配对变化 | read p99 配对变化 | read p99 绝对增量 |
|---|---:|---:|---:|
| low | +0.73% [+0.66%, +0.78%] | +5.98% | 23.3 → 24.6 µs |
| medium | +0.52% [+0.41%, +0.70%] | +6.01% | 23.3 → 24.8 µs |
| high | +0.79% [+0.48%, +0.92%] | +7.26% | 23.3 → 24.9 µs |

CPU 开销 **0.5%–0.8%**，尾延迟绝对增量 **1.3–1.6 µs**。这是 PEBS 硬件采样 + ring 消费 +
region 维护的总成本，采样线程 CPU 只是其中一部分。

![纯采样开销](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/fig2-overhead.png)

### 4.2 换页压力（pressure，固定 30000 QPS）

| 纯采样配置 | CPU/请求 配对变化（vs off） |
|---|---:|
| static-low | +0.03% [−8.04%, +0.52%] |
| static-medium | +4.26% [−3.03%, +6.69%] |
| static-high | +0.71% [−3.48%, +4.72%] |
| static-adaptive | +1.09% [+0.58%, +4.07%] |

此场景含回收与 RDMA 行为波动，数值散布大，不能全归因于硬件 PEBS 指令成本。

### 4.3 饱和换页（saturation，不限速）

| 对照 | QPS 配对变化 |
|---|---:|
| static-medium vs off | **−6.20%** [−20.07%, +3.49%] |

**结论**：不能把 PEBS 概括为“所有场景开销 <1%”——无换页时 CPU 开销确实 ~0.5–0.8%，
但在饱和换页负载下，纯采样的吞吐损失约为 6%。

---

## 5. 结论四：优化策略带来的收益

“优化策略”= policy（用 PEBS 样本动态选传输粒度），相对 static 的收益才是策略本身的贡献。

### 5.1 不限速吞吐（saturation）

| 对照 | QPS 配对变化 | read p99 配对变化 | CPU/请求 配对变化 | RDMA read/请求 配对变化 |
|---|---:|---:|---:|---:|
| policy-medium vs static-medium | **+136.11%** [+118.7%, +190.9%] | −49.64% | −59.40% | −91.73% |
| policy-medium vs off | **+123.76%** [+117.4%, +144.4%] | −47.00% | −58.24% | −91.79% |
| policy-medium vs off-original-mask | **+223.10%** [+204.4%, +250.3%] | −58.36% | −70.57% | −92.58% |

策略让饱和吞吐从 ~59k（static）翻到 ~145k QPS：**+136%**。同时 read p99 减半、
单位请求 CPU 减 ~59%、RDMA 读流量减 ~92%。收益来自“按访问密度选对传输粒度”，
而非仅仅切换开关——WR 分布真实改变（大 WR 读字节占比从 static 的 ~98% 降到 policy 的 ~14%）。

### 5.2 固定 QPS 压力（pressure）：策略净收益（vs off）

| 频率档 | CPU/请求 净变化 | RDMA 读流量 净变化 |
|---|---:|---:|
| policy-low | −2.98% | −16.03% |
| policy-medium | −14.45% | −84.39% |
| **policy-high** | **−15.69%** | **−90.90%** |
| policy-adaptive | −15.71% | −90.78% |

在固定 30000 QPS 下，策略通过削减读流量换来净成本收益：CPU/请求 −14%～−16%、
RDMA 读流量 −84%～−91%（medium/high/adaptive）。相对同频 static 的收益更大（CPU −17.8%、RDMA −91.5%）。

**一个需要保留的 caveat**：medium 的不限速吞吐最高，但其压力场景尾延迟波动大
（read p99 4383 µs，范围 479–40046 µs）；high 的尾延迟更稳（612 µs，范围 604–1805 µs）。
medium 的 +136% 吞吐不能直接套用到 high 的配置上，high 的饱和吞吐本次未测。

---

## 6. 局限（诚实边界）

- 单机（dnet-61）、单一 Memcached 工作集（800k × 1 KiB）、每配置 5 次重复；min–max 不是置信区间，
  不能据此声明统计显著或普遍适用。
- 压力场景用 `memory.high=640 MiB`（软压力），与此前 70% `memory.max` 硬限额基线不可跨实验归因。
- 压力日志出现 9 次 `perf: interrupt took too long`，内核把 `kernel.perf_event_max_sample_rate`
  从 24000 自动下调到 2750；各轮可能受此历史状态影响，未关闭 perf 保护机制。
- 采样周期是事件数阈值不是 Hz；实测 samples/s 是观测量（受策略改变后的访存行为影响），
  不能当独立控制变量反推因果。
- 采样频率与吞吐：resident 阶段 QPS 被固定，测不出频率对最大吞吐的影响；saturation 只测了 medium 单点，
  没有“频率–吞吐”曲线。

## 7. 数据与复现

- 完整数据与配对统计：[SUMMARY.md](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/SUMMARY.md)
- 原始 CSV：`pressure/resident/saturation/summary.csv`（同目录下）
- 采样频率快照：[sampling-frequencies.csv](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/sampling-frequencies.csv)
- 本文图表脚本：[tools/rdma/pebs/plot_pebs_clean.py](../../tools/rdma/pebs/plot_pebs_clean.py)
- 完整原始记录（含全部 caveat 与验收细节）：[pebs-performance-20261006.md](pebs-performance-20261006.md)

---

## 8. 附录：采样频率档的实现（low / medium / high / adaptive）

采样频率档本质是**采样周期（sample period）**的四种配置。`hermit_pebsd` 内核线程给每个
CPU 打开两个系统级 PEBS 事件（`precise_ip=1`、`exclude_kernel/hv=1`，
`mm/hermit_pebs.c:401/420`）：

| 事件 | raw code | 含义 |
|---|---|---|
| `HERMIT_PEBS_L3_MISS` | `0x1d3` | `MEM_LOAD_RETIRED.L3_MISS`（读） |
| `HERMIT_PEBS_ALL_STORES` | `0x82d0` | `MEM_INST_RETIRED.ALL_STORES`（写） |

`attr.sample_period` 是**两次采样之间间隔的硬件事件数**，周期越小采样越密。周期表移植自
MEMTIS（`htmm_sampler.c`，`hermit_pebs.c:54-62`）：

```c
/* load 周期表，30 档 */
{199, 293, 401, ..., 1999, ..., 19997};
/* store 周期表，5 档 */
{100003, 300007, 600011, 1000003, 1500003};
```

**固定档 low / medium / high**：由“固定周期控制”补丁新增 debugfs `pebs_load_period` /
`pebs_store_period` / `pebs_adaptive`（`hermit_pebs.c:877-879`）。当 `pebs_load_period != 0`
时用 `?:` 覆盖周期表（`hermit_pebs_update_periods`，`hermit_pebs.c:292-295`），并写
`pebs_adaptive=0` 关掉自适应。驱动只改 load 周期、store 固定 1500003
（`pebs_perf_matrix.py:113,152`）：

| 档 | load 周期 | store 周期 | 实测 samples/s |
|---|---:|---:|---:|
| low | 19997 | 1500003 | ~820 |
| medium | 1999 | 1500003 | ~900 |
| high | 199 | 1500003 | ~1500 |

这三个值正是 load 周期表的第 [29]、[13]、[0] 档。

**adaptive**：写 `pebs_adaptive=1`、两个 period 都为 0，回退到周期表**起始档**（load=199、
store=100003），随后 `hermit_pebsd_main` 每 2 秒（`window=2000ms`）调一次
`hermit_pebs_adapt`（`hermit_pebs.c:327-341`），把每窗口样本数控制在 1000–50000 内：

```c
if (window_samples > 50000 && idx < 29) { idx++; ... }   // 太密 → 周期+1（变稀）
else if (window_samples < 1000 && idx > 0) { idx--; ... } // 太稀 → 周期-1（变密）
hermit_pebs_update_periods();                             // 应用到每个 per-CPU 事件
```

两个易误读点：

1. **samples/s 不随 load 周期线性变化**：store 周期固定 1500003 贡献恒定基线，且采样率随策略
   改变后的访存行为变化，所以 load 周期 19997→199 差 100 倍，samples/s 只从 ~820→~1500。
2. **adaptive 的 ~12800/s 远高于 high 的 ~1500/s，主因是 store 周期不同**：两者 load 周期都是 199，
   但 adaptive 的 store=100003（比固定档 1500003 密 15 倍），store 样本占大头。这正是第 3.2 节
   “adaptive 采样率 8 倍于 high 但收益不放大”能比较、却不可直接归因于 load 采样频率的原因。
