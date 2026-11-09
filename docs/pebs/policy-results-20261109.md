# Hermit 4 KiB 策略在 MGLRU 下的测试结果（内核 #20）

> 更新：2026-11-09。全部测试运行于 dnet-61，内核 `6.18.38-hermit-pebs #20`（2026-11-08 构建）。
> 上一轮结果见 [11-05 完整版](policy-results-20261105.md)。代码修改见
> [内核集成说明](kernel-policy-integration.org) 的“2026-11-08 MGLRU 下的流与批次修正”。
> 不同内核之间的比较只作方向参考；本文的主要结论都来自同一内核、同一块内的 off/policy 配对。
>
> #20 的测试代码早于 2026-11-09 删除旧 mTHP 选阶的清理。清理只删除了 `SWAPIN` 关闭时才会走到的旧模型，
> 本文的 policy 单元都开启 `SWAPIN`，结论不受影响，但清理后的内核还没有重测。

## 摘要

- **MGLRU 是收益的前提。** 同一份策略代码，在 MGLRU（`policy_features=0x1bf`）下 GAPBS 与 Memcached
  各档都快于 off；在传统 LRU（`0xbf`）下，GAPBS BFS 慢 25%–58%，Memcached 4 KiB 档慢 39%。
  差别主要在 CPU：Memcached 4 KiB 档 policy 每次请求的服务端 CPU 在 MGLRU 下为 24.8 µs，在传统 LRU 下为 44.6 µs。
- **运行长度封顶显著减少了多读。** 连续访问的三个密度负载上，policy 读入字节与实际访问字节之比在九档都接近 4 KiB off
  （d50 0.60、d25 0.40、d6.25 0.32），#19 为 0.61–1.36。policy 绝对吞吐比 #19 提高 38%–80%。
- **与“最佳档位的 off”比较**（每个负载取 off 九档中最快的一档作分母）：

  | 负载 | 结果 |
  |---|---|
  | 分散访问 d25/d6.25、d1p | +60% 到 +71% |
  | GAPBS PageRank | +22.5% |
  | Memcached | +15.4%（客户端 CPU 接近满载，瓶颈尚未单独验证） |
  | GAPBS BFS | +14.4% |
  | 8 线程全扫描 | +11.8% |
  | d6.25 连续 | +1.7%（连续访问负载第一次不输这条基线） |
  | YCSB | −4.0% |
  | Redis | −7.0% |
  | d25 连续、d50 连续 | −13.1%、−20.5% |

- **剩余的差距**集中在 16–128 KiB 档的连续访问：这些档位 off 的 mTHP 正好覆盖访问段，读请求比 policy 少。
- **传统 LRU 矩阵未跑完。** 第 46 个单元（GAPBS BFS、2048 KiB、static）在验证阶段被 memcg OOM 杀掉，剩余 206 个单元没有运行。

## 测试范围

| 组 | LRU / 掩码 | 负载 | 上限 KiB | 单元 | 状态 |
|---|---|---|---|---:|---|
| density-high | MGLRU / `0x1bf` | d50、d25、d6.25 连续 | 九档 | 243 | 完成 |
| density-low | 同上 | d25、d6.25 分散，d1p | 九档 | 243 | 完成 |
| anon8t | 同上 | 8 线程全扫描 | 九档 | 81 | 完成 |
| gapbs | 同上 | BFS、PageRank | 九档 | 162 | 完成 |
| redis、ycsb | 同上 | Redis、YCSB | 九档 | 81 + 81 | 完成 |
| memcached | 同上 | Memcached | 九档 | 54 | 完成 |
| classic | 传统 LRU / `0xbf` | d25 连续、d6.25 分散、8 线程全扫描、GAPBS BFS 等 7 个 | 4、64、256、2048 | 45 / 252 | OOM 中断 |
| classic-mc | 同上 | Memcached | 4、2048 | 12 | 完成 |

MGLRU 各组每个负载、每档上限、每次重复是一个块：off、static-1999、policy-1999 各一个单元（Memcached 只有 off 和 policy-medium，
即 load 周期 1999），块内配对，共 3 次重复。传统 LRU 组计划同样 3 次重复，中断前只完成了第 1 次重复的 45 个单元，
所以传统 LRU 的匿名与 GAPBS 数字是单次结果。成功单元共 1002 个：MGLRU 945 个（普通矩阵 891 + Memcached 54），
传统 LRU 57 个（普通矩阵 45 + Memcached 12）；失败的 OOM 单元未纳入图表。

| 项目 | 设置 |
|---|---|
| 内存预算 | `fixed-max-v1`：本地内存为工作集的 70%（`LOCAL_RATIO_PCT=70`）；Memcached 为 `memory.high=640 MiB`、`memory.max=2048 MiB` |
| 页面上限 | `max-allocation-v1`，4–2048 KiB 九档 |
| 后端 | RDMA，`remote_order_mask=0x3fd`、`bypass_swapcache=Y` |
| 普通矩阵回收 / I/O | 匿名、GAPBS、Redis、YCSB：`apt_reclaim=N`、`speculative_io=N`、`reclaim_mode=0` |
| Memcached 回收 / I/O | `apt_reclaim=Y`、`speculative_io=Y`、`reclaim_mode=1`；headroom 16384 页（64 MiB），4 个回收 worker |
| 采样 | load 周期 1999、store 周期 1500003 |
| MGLRU | `/sys/kernel/mm/lru_gen/enabled=0x0007`；传统 LRU 组为 `0x0000` |

这里的页面上限为 **4、16、32、64、128、256、512、1024、2048 KiB**。开启某个上限时，所有更小的合法匿名 folio
也保持开启；它不是强制分配大小，更不是 RDMA 请求大小。普通矩阵与 Memcached 的控制项分别由逐单元 `before.json`
和 `config.json` 核实，同组 off/policy 保持一致，但不能把两类矩阵的回收配置混为一谈。

### CPU、超线程与策略执行位置

**机器开启了超线程，负载绑核没有同时使用同一物理核的两个 SMT 线程。** 原始 `lscpu.txt` 和 Memcached
`cpu-topology.txt` 显示：两颗 Intel Xeon Silver 4216，每颗 16 个物理核、每核 2 个线程，共 32 核 / 64 个在线逻辑 CPU。
逻辑 CPU `i` 与 `i+32`（`i=0…31`）属于同一物理核：

| NUMA node / socket | 每核选取的一个线程 | 对应 SMT sibling |
|---|---|---|
| 0 / 0 | CPU 0–15 | CPU 32–47 |
| 1 / 1 | CPU 16–31 | CPU 48–63 |

下面是**允许调度的 CPU 集合**，不是每条线程始终停留的精确 CPU。普通矩阵来自每个单元的 `case.json` 和随运行保存的驱动；
Memcached 另外保存了 `/proc/PID/status`、sampler affinity 的前后快照。

| 负载 / 组件 | CPU 集合与线程配置 | 证据与边界 |
|---|---|---|
| 单线程密度扫描 | CPU 0，1 个工作线程 | `BENCH_CPUS=0`、`BENCH_THREADS=1` |
| 8 线程全扫描 | CPU 0–7，8 个工作线程 | `taskset` 限定进程；没有保存逐线程调度轨迹 |
| GAPBS BFS / PageRank | CPU 0–7，8 个 OpenMP 线程 | `OMP_NUM_THREADS=8`、`OMP_PROC_BIND=true`、`OMP_PLACES=cores` |
| Redis | 服务端 CPU 0–7；扫描客户端 CPU 8–15 | 1 个实例、1 个扫描客户端；CPU 集合大小不等于工作线程数 |
| YCSB | Redis 服务端 CPU 0–7；YCSB 客户端 CPU 8–15 | 记录了进程 CPU 集合，没有逐线程运行时间 |
| Memcached | 服务端 CPU 0–7，8 个 worker；mutilate 客户端 CPU 16–31，16 个线程 | server/client 不共享物理核，位于不同 socket |
| Memcached 的 `hermit_pebsd` | CPU 0–7 | 与服务端共享 8 个物理核，没有专用采样核；所有 policy 单元前后 affinity 都核实一致 |
| 其他矩阵的 `hermit_pebsd` | 驱动未显式绑核 | 未保存该线程 affinity/运行时间，无法还原它实际在哪些 CPU 上执行 |

PEBS 硬件事件覆盖所有在线 CPU；`hermit_pebsd` 的绑核只约束读取 perf ring、维护窗口的内核线程，不限制硬件采样范围。
换入预测、`fit` 更新、大 folio 判断在触发缺页的应用线程上下文执行，因此计入应用的内核态 CPU 时间；回收与批量写出在
执行回收的线程中发生。Memcached 的主动回收 worker 使用 `system_unbound_wq`，这轮没有保存 worker 的独立 CPU 时间或调度轨迹。
所以不能把“应用只选每核一个线程”写成“整机关闭 SMT”，也不能保证所有策略后台工作都没有用到 SMT sibling。

CPU 拓扑和配置摘要保存在 [cpu-context.json](assets/20261109-k20-matrix/cpu-context.json)。
[cpu-activity.csv](assets/20261109-k20-matrix/cpu-activity.csv) 还保留了每个单元、每个逻辑 CPU 的 busy/user/system 比例。
普通矩阵的这个区间是 **整个单元，包括准备、填充和应用阶段**；Memcached 则是测量区间。它们是整颗逻辑 CPU 的活动量，
包含系统和其他线程，不能直接当作应用进程或策略的独立开销。作为调度范围的辅助检查，2 MiB 上限下结果如下：

| 负载 | CPU 0–7 平均 busy%，off → policy | 客户端 CPU 集合平均 busy%，off → policy | 计时范围 |
|---|---:|---:|---|
| 8 线程全扫描 | 46.91 → 44.70 | — | 整个单元 |
| GAPBS BFS | 19.90 → 21.69 | — | 整个单元，含建图与验证 |
| GAPBS PageRank | 61.84 → 59.01 | — | 整个单元，含建图与验证 |
| Redis | 6.01 → 5.72 | 4.22 → 4.51（CPU 8–15） | 整个单元 |
| YCSB | 2.65 → 2.77 | 9.91 → 9.83（CPU 8–15） | 整个单元 |
| Memcached | 96.19 → 84.02 | 99.84 → 99.84（CPU 16–31） | 120 s 测量区间 |

每个值先在该 CPU 集合内取平均，再取 3 次重复的中位数。CPU 0–7 平均 20% 不等于应用总共只用了一个核的 20%；
这个口径的 100% 代表 8 个逻辑 CPU 全忙。Memcached 的**进程级**和采样线程级开销见第三节。

新加入的负载：

| 负载 | 定义 |
|---|---|
| `anon-1t-d{50,25,6.25}-high` | 单线程，16 GiB 匿名工作集，随机顺序访问 2 MiB 区域，每个区域访问 50%/25%/6.25% 的页，连成一段（256/128/32 页） |
| `anon-1t-d{25,6.25}-low` | 同上，但每区域的访问页按互质步长分散 |
| `anon-1t-d1p-high` | 每个 2 MiB 区域只访问 1 页 |
| `gapbs-bfs` / `gapbs-pr` | GAPBS 1.5，Kronecker 图 scale 24、平均度 16，8 线程；BFS 16 次、PageRank 4 次 trial，每次 trial 都验证。吞吐为 trials/s，只计 trial 时间 |

密度负载的吞吐单位是“页面覆盖率 GiB/s”，即每个 4 KiB 页读 1 字节。GAPBS 的建图在 trial 之前完成，
建图后再把 `memory.max` 降到预算。GAPBS 吞吐只计 kernel trial 时间，但 I/O 与策略计数的快照区间也包含 trial 验证；
这些字节不能全部归因于图算法 kernel，也不能直接除以 trial 时间当作 kernel 的 RDMA 带宽。

## 一、运行长度封顶（密度负载）

### 修改内容

#19 的流在同一段连续访问内按 1、2、4…页翻倍，到段尾时最后一批会越过访问段，读入从未访问的页。
#20 记录每条流上一个访问段的实际长度（`fit`），下一段的预测批次不超过 `fit` 减去当前位置；
访问段比 `fit` 长时，走过 `fit` 后继续翻倍，段结束后更新 `fit`。新增两个计数：
`capped_batches`（被封顶的预测次数）和 `batch_overruns`（访问段在预测批次中途结束的次数）。
实现见[内核集成说明](kernel-policy-integration.org)。

### 读入量与请求数

![连续访问的读入量与读请求数](assets/20261109-k20-matrix/density-reads.png)

“读入/访问”是 RDMA 读入字节除以负载实际访问的字节。测量开始时部分访问页仍在本地，所以 4 KiB off 的比值低于 1；
它就是不多读时的下限。

| 负载 | 4 KiB off | 2048 KiB off | policy（九档） | #19 policy | policy 读请求/访问页 |
|---|---:|---:|---:|---:|---:|
| d50 连续 | 0.60 | 2.00 | **0.60** | 0.61–1.36 | 0.050 |
| d25 连续 | 0.40 | 4.00 | **0.40** | 0.64–0.67 | 0.041 |
| d6.25 连续 | 0.32 | 16.00 | **0.32** | 0.65–0.66 | 0.070 |

- policy 在九档上的读入量都接近 4 KiB off；例如 d50 为约 0.604，4 KiB off 约 0.602，不能仅凭两位小数相同断言零多读。
  读请求数是 4 KiB off 的 1/12 到 1/5。
- `capped_batches` 每个单元 2,638–4,948 次，`batch_overruns` 最多 1 次，`refault_4k` 和 `stream_evictions` 都是 0。
- d50 走大 folio 的换入字节从 #19 的 27%–47% 降到 0.02% 以下。封顶后，`hermit_policy_large()` 只在预计剩余长度足够时才切到大 folio，
  d50 每段 256 页，到段尾前都留在被封顶的 4 KiB 批次上。
- 分散访问和 d1p 的每段只有 1 页，没有预测（`capped_batches=0`），读请求/访问页与 4 KiB off 相同。

### 吞吐

![密度负载绝对吞吐随最大开启页面大小变化](assets/20261109-k20-matrix/density-throughput.png)

横轴为最大开启页面大小，纵轴为页面覆盖率 GiB/s，使用线性刻度；每条线为 3 次重复中位数，阴影为最小值到最大值。
off、static、policy 的绝对吞吐画在同一张子图，下面保留同档相对变化图用于对照。

![密度负载 policy 相对同档 off 的吞吐变化](assets/20261109-k20-matrix/density-change.png)

| 上限 KiB | 4 | 16 | 32 | 64 | 128 | 256 | 512 | 1024 | 2048 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| d50 连续 | +116% | −3% | −17% | −27% | −27% | −8% | +27% | +64% | +64% |
| d25 连续 | +106% | −4% | −17% | −25% | −23% | −9% | +40% | +186% | +374% |
| d6.25 连续 | +83% | −7% | −15% | −15% | +1% | +40% | +152% | +537% | +1897% |
| d25 分散 | +30% | +355% | +316% | +228% | +183% | +161% | +149% | +142% | +144% |
| d6.25 分散 | +29% | +153% | +449% | +1279% | +1184% | +1078% | +1015% | +988% | +1034% |
| d1p | +35% | +94% | +187% | +332% | +624% | +1285% | +2817% | +8000% | +31510% |

三次重复的中位数。off 在三次之间的差距大多不到 1%；例外是 d50 1024 KiB（29%）和分散访问 256–1024 KiB（10%–30%）。

- 连续访问的三个负载比 #19 改善了 0–60 个百分点（图中灰色虚线是 #19）。policy 绝对吞吐提高 38%–80%，
  只有 d50 在 16–128 KiB 档与 #19 持平（−3% 到 +1%）。
- 16–128 KiB 档仍慢于 off。这一段 off 的 mTHP 大小不超过访问段长度，读入量几乎不变，读请求却比 policy 少
  （d50 64 KiB 档 off 为 0.042/页，policy 为 0.050/页）。policy 的一个访问段要经过 1、2、4、8、16 页几次缺页才能达到 SG 批次上限。
- 分散访问和 d1p 上 policy 不预测，收益完全来自不跟随 mTHP 整块读入。4 KiB 档没有整块读入，仍快 +29% 到 +35%；
  这一档 policy 的写请求只有 off 的 1/16 到 1/13（例如 d25 分散 26,400 对 420,750），推测来自 MGLRU 候选的 SG 合并写出，
  但没有用关闭 `BATCH` 位的消融验证。
- **测量时长。** d1p 的 policy 单元只有 0.02–0.03 s，d6.25 的 policy 单元为 0.4–0.8 s。这几行的百分比只看方向，
  尤其 d1p 2048 KiB 的 +31510%；读入字节和请求数不受计时精度影响。
- static 相对 off 的中位数在 −1.6% 到 −0.5% 之间，个别档位最低 −10.5%（d25 分散 128 KiB）。

### 最大开启大小与实际换入大小

![密度负载在各上限下实际换入的 folio 大小分布](assets/20261109-k20-matrix/density-folio-sizes.png)

横轴是配置上限，纵轴是实际换入 folio 大小；颜色和格内数字是该大小占换入字节的百分比。
先逐次运行归一化，再对 3 次分布取算术平均，每列合计 100%；低于 0.5% 的格子不标数字。
这些是后端成功读取所承接的 folio，包含预取，不是网络请求长度，也不是全部驻留内存的页面普查。
密度负载的 policy 几乎全部使用 4 KiB 换入；即使允许 2 MiB，也没有强制按上限读取。

## 二、8 线程全扫描

| 上限 KiB | 4 | 16 | 32 | 64 | 128 | 256 | 512 | 1024 | 2048 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| policy vs off | +77% | +29% | +22% | +15% | +15% | +17% | +11% | +10% | +13% |
| off 三次差距 | 4% | 4% | 1% | 1% | 2% | 4% | 20% | 19% | 58% |

- 九档全部快于同档 off；与最佳档位的 off（512 KiB）比较为 +11.8%。
- 流表改为 8 组 × 8 路后，`stream_evictions` 在所有单元都是 0。#17 上约三分之一的单元有 2 个线程停在 4 KiB 批次，本轮没有再出现。
- 512–2048 KiB 三档 off 自身差距 19%–58%，2048 KiB 档 policy 三次分别为 −36%、+61%、+14%，这三档不作结论。
- policy 在 16–256 KiB 档的读请求是 off 的 1.4–4.6 倍，读入字节相同，吞吐仍然领先，说明这些档位 off 的瓶颈不在读请求数。
  policy 的写请求更少（64 KiB 档 154,423 对 220,290；4 KiB 档 160,233 对 3,330,716），但各部分的贡献还没有拆分。

## 三、应用负载

![应用负载吞吐随最大页面大小变化](assets/20261109-k20-matrix/apps-throughput.png)

横轴为最大开启页面大小（KiB，按 2 的幂排列），纵轴为各应用的**绝对吞吐，线性刻度从零开始**。
折线为 3 次重复的中位数，阴影为最小值到最大值，不是置信区间。Memcached 没有 static 单元，因此只画 off/policy。
左上角同时给出 8 线程全扫描；它的单位是页面覆盖率 GiB/s，不能与应用的 ops/s、QPS 或 trials/s 横向比较。

policy 相对同档 off 的变化（三次重复的中位数）：

| 上限 KiB | 4 | 16 | 32 | 64 | 128 | 256 | 512 | 1024 | 2048 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| GAPBS BFS | +14% | +76% | +86% | +100% | +108% | +98% | +101% | +104% | +109% |
| GAPBS PageRank | +133% | +39% | +26% | +25% | +22% | +26% | +25% | +24% | +21% |
| Memcached | +9% | +61% | +146% | +396% | +808% | +1410% | +1873% | +1839% | +1842% |
| Redis | +9% | −17% | −3% | −22% | −22% | −20% | +8% | +46% | +176% |
| YCSB | +26% | −3% | −7% | −10% | −9% | −8% | −8% | −8% | −2% |

static 相对 off 的中位数在 −1.2% 到 −0.5% 之间；单档最低为 Redis −8.3%，Redis 每个单元只测 0.8–2.6 s。

### 页面上限与实际使用的 folio 大小

![应用在各最大开启大小下实际换入的 folio 大小分布](assets/20261109-k20-matrix/apps-folio-sizes.png)

与密度图采用相同口径：用 `order_stats` 的成功 load 数乘以对应 folio 大小，计算**按换入字节加权的分布**。
普通矩阵读取 `measurement.json` 的 application 阶段，Memcached 使用测量前后的 `order_stats` 差值。
`read_wrs` 或 `read_bytes / read_wrs` 没有用于推断 folio 大小：16 个 4 KiB folio 可以组成一个 64 KiB RDMA READ。
static 的完整分布也保存在 [folio-sizes.csv](assets/20261109-k20-matrix/folio-sizes.csv)，主图为便于比较只画 off/policy。
folio 与 RDMA 字节计数分别读取，少数单元存在快照时差；逐单元求和核对后，差异均小于 0.007%。
图中按 folio 计数自身的字节总量归一化，不用另一组计数强行补齐。

以 **2 MiB 上限**为例，3 次运行的字节占比分布平均值为：

| 负载 | off 实际换入 | policy 实际换入 | 含义 |
|---|---|---|---|
| 8 线程全扫描 | 接近 100% 为 2 MiB | 99.87% 为 2 MiB | 足够长的顺序流确实回到了大 folio 路径 |
| GAPBS BFS | 4 KiB 占 71.80%，2 MiB 仅 1.91% | 4 KiB 占 98.65% | off 本身也不是固定 2 MiB；policy 更偏向基本页 |
| GAPBS PageRank | 4 KiB 占 27.46%，其余分布于各级 mTHP | 4 KiB 占 79.58% | 仍保留一部分中间大小的 folio |
| Redis | 2 MiB 占 78.46% | 几乎 100% 为 4 KiB | 64 KiB 局部读取没有促使整个 2 MiB 块换入 |
| YCSB | 2 MiB 占 90.94% | 4 KiB / 1 MiB / 2 MiB 分别占 29.86% / 25.80% / 24.67% | 同一上限下混用多种大小 |
| Memcached | 512 KiB / 256 KiB 分别占 43.29% / 33.70% | 几乎 100% 为 4 KiB | 允许 2 MiB 不代表实际换入主要是 2 MiB |

**换入分布不能替代整个应用的驻留页分布。** 一直驻留、没有发生远程读取的大 folio 不会出现在上述计数中。
本轮只有 Memcached 保存了完整 `pagemap + kpageflags` folio 普查，因此再单独画出它的实际驻留映射：

![Memcached 加载后、预热后、测量后的实际驻留 folio 大小](assets/20261109-k20-matrix/memcached-resident-folios.png)

每列仍是一个页面上限，颜色表示**该时刻驻留映射字节**在各大小上的占比，包含服务端的库和共享映射；
没有给已换出或尚未驻留的页猜测大小。`unknown` 单列保留普查无法确定的部分，测量后最大占比约 0.0049%。
普查不是原子快照，并发回收/拆分可能造成少量不一致；配置上限也不会把已有或共享 folio 强制拆小。

2 MiB 上限的 policy 测量后，约 **21.56%** 驻留映射字节位于 4 KiB folio，**48.55%** 位于 512 KiB，
**21.50%** 位于 256 KiB。它与“测量期间几乎全部按 4 KiB 换入”同时成立：换入流量与驻留存量是两个统计对象。
其他应用只保存了 `smaps_rollup` 等汇总，不能据此重建全部 mTHP 驻留分布；本报告没有把它们的换入图冒充驻留普查。

### GAPBS

- **BFS。** off 在 4 KiB 档最快（1.66 trials/s），上限越大越慢，2048 KiB 档为 0.79。policy 在各档为 1.58–1.90，
  2048 KiB 档读入 60 GiB，off 为 116 GiB。与最佳档位的 off 比较为 +14.4%。
- **PageRank。** 与 BFS 相反，off 在 4 KiB 档最慢（0.086 trials/s），2048 KiB 档最快（0.173）。
  policy 在 4 KiB 档的读请求是 off 的 1/6（2,284,946 对 13,173,772），读入字节相近，吞吐为 off 的 2.3 倍；
  其余档位领先 21%–39%。与最佳档位的 off 比较为 +22.5%。
- 两个负载上预测都在工作：BFS 每个单元 365–433 万个预测页、`capped_batches` 28–39 万；PageRank 1,041–1,847 万个预测页。
- BFS 每个单元有 142–222 万次 `refault_4k`、942–1,277 次 `stream_evictions`；PageRank 为 9–42 万次和 36–107 次。
  GAPBS 是本轮唯一持续出现流表淘汰的负载：8 个线程在多个故障指令上同时访问，部分组的活动流超过 8 路。
  这些计数说明发生了活跃流竞争，但相对于数百万次换入很少，尚不足以判定流表容量是主要瓶颈。
- 与 #18 相比，两个负载的 policy/off 变化在 ±10 个百分点内（#18 原始目录未保留，按当时记录的数字比较）。
  refault 修复与运行长度封顶对 GAPBS 影响不大。

### Redis 与 YCSB

负载定义同 [11-03 报告](policy-results-20261103.md)：Redis 对每个 2 MiB value 读 64 KiB，YCSB 均匀读取整条 2 MiB 记录。

- **Redis。** 1–2 MiB 档 off 整块读入（2048 KiB 档读 5.7 GiB，policy 0.2 GiB），policy 领先 46% 和 176%。
  16–256 KiB 档 off 的 mTHP 恰好覆盖 64 KiB，读请求只有 policy 的 1/13 到 1/7，policy 慢 17%–22%。
  与最佳档位的 off（128 KiB）比较为 −7.0%。1024 KiB 档 off 三次差距 39%。
- **YCSB。** 4 KiB 档 policy 读请求是 off 的 1/10，快 26%；其余档位慢 2%–10%，与最佳档位的 off 比较为 −4.0%。
  #17 上九档为 −18% 到 −25%（[11-05 报告](policy-results-20261105.md)），本轮缩小到个位数。
- Redis 的 `refault_4k` 为 0；YCSB 每个单元最多 383 次 `refault_4k`、6 次 `stream_evictions`，相对换入量可以忽略。Redis 每段 16 页，`capped_batches` 为 4,157–10,367；
  每条记录开头仍要经过 1、2、4、8 页的增长，这是 16–256 KiB 档读请求多的原因。

### Memcached

![Memcached 吞吐、每次请求的服务端 CPU 和 GET p99](assets/20261109-k20-matrix/memcached.png)

配置同 11-04 消融：80 万条 1 KiB value，90% GET，预热 30 s、测量 120 s，服务端 CPU 0–7，客户端 16–31，
`memory.high=640 MiB`。区别是本轮开启 MGLRU、掩码 `0x1bf`，并测了九档。

| 上限 KiB | off QPS | policy QPS | 变化 | off CPU/请求 µs | policy CPU/请求 µs | GET p99 变化 |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 240,287 | 261,942 | +9% | 26.9 | 24.8 | +4% |
| 16 | 162,116 | 260,775 | +61% | 44.5 | 24.9 | −1% |
| 64 | 53,763 | 266,336 | +396% | 135.5 | 24.8 | −64% |
| 256 | 17,971 | 275,192 | +1410% | 424.8 | 24.3 | −87% |
| 2048 | 14,108 | 273,513 | +1842% | 546.1 | 24.5 | −90% |

- policy 在九档上都是 26–28 万 QPS、24.2–24.9 µs/请求；4 KiB 档已低于 off 的 26.9 µs。
  11-04 在传统 LRU 上的 `0xbf` 为 42.2 µs，关闭冷页观察的 `0xb7` 为 31.4 µs。
- 4 KiB 档 GET p99 比 off 高 4%（6,992 对 6,739 µs）；从 64 KiB 档起 policy 的 p99 低 64%–91%。
- **客户端接近满载。** 各单元的客户端 CPU 约 99.8%–99.9%，可能存在客户端限制，但低吞吐 off 单元也接近满载，
  其中还可能包含轮询或等待开销。仅凭 CPU 数字不能证明瓶颈，更不能把 +15.4% 写成收益下限；需要增加客户端资源后复测。

#### 应用进程与策略采样线程的 CPU 开销

![Memcached 服务端、客户端与 PEBS 采样线程的 CPU 使用率](assets/20261109-k20-matrix/memcached-cpu.png)

| 最大开启大小 KiB | 服务端 CPU%，off | 服务端 CPU%，policy | 客户端 CPU%，policy | `hermit_pebsd` CPU%，policy |
|---:|---:|---:|---:|---:|
| 4 | 80.65 | 81.22 | 99.87 | 0.266 |
| 16 | 89.96 | 81.32 | 99.87 | 0.275 |
| 32 | 89.95 | 81.77 | 99.83 | 0.266 |
| 64 | 90.76 | 82.37 | 99.87 | 0.283 |
| 128 | 94.90 | 83.11 | 99.87 | 0.283 |
| 256 | 95.27 | 83.62 | 99.87 | 0.291 |
| 512 | 95.99 | 83.71 | 99.87 | 0.300 |
| 1024 | 95.85 | 83.98 | 99.87 | 0.300 |
| 2048 | 95.98 | 83.41 | 99.87 | 0.291 |

3 次重复中位数。服务端是进程 CPU 秒 / 测量秒 / **8 个 CPU**；客户端是客户端 CPU 秒 / 客户端墙钟秒 / **16 个 CPU**；
sampler 是线程 CPU 秒 / 测量秒，**100% 表示占满一个 CPU**。例如服务端 81.22% 约为 6.50 个 CPU 的运行时间，
sampler 0.266% 仅约 0.00266 个 CPU；分母不同，不能把这些百分比直接相加。

`hermit_pebsd` 在 120 s 测量中消耗约 0.32–0.36 CPU 秒，但这只是**采样消费线程**的开销，
不包括应用缺页路径里的预测、PTE 安装、回收 worker、PEBS 中断等。服务端 CPU/请求包含进程上下文中的内核工作，
但也不是整套系统的 CPU/请求。普通矩阵没有保存可单独归因给 `hermit_pebsd` 和应用进程的同阶段 CPU 时间，不能从全机 busy 差值补造。

## 四、与“最佳档位的 off”比较

![policy 与最佳档位 off 的比较](assets/20261109-k20-matrix/best-ceiling.png)

每个负载取 off 九档中位数最高的一档作基线，与 policy 九档中位数最高的一档比较。这条基线假设使用者事先知道每个负载最合适的 mTHP 上限，
比同档配对更严格。括号内是 policy 有几档超过这条基线。

| 负载 | off 最佳档位 | policy 最佳 vs off 最佳 | 超过基线的档数 |
|---|---:|---:|---:|
| d50 连续 | 128 KiB | −20.5% | 0/9 |
| d25 连续 | 128 KiB | −13.1% | 0/9 |
| d6.25 连续 | 64 KiB | +1.7% | 1/9 |
| d25 分散 | 4 KiB | +61.4% | 9/9 |
| d6.25 分散 | 4 KiB | +60.2% | 9/9 |
| d1p | 4 KiB | +71.0% | 9/9 |
| 8 线程全扫描 | 512 KiB | +11.8% | 2/9 |
| GAPBS BFS | 4 KiB | +14.4% | 6/9 |
| GAPBS PageRank | 2048 KiB | +22.5% | 9/9 |
| Redis | 128 KiB | −7.0% | 0/9 |
| YCSB | 512 KiB | −4.0% | 0/9 |
| Memcached | 4 KiB | +15.4% | 9/9 |

off 的最佳档位从 4 KiB 到 2048 KiB 都有，没有一个固定上限对所有负载都合适。policy 在 8 个负载上超过最佳档位的 off，
在 4 个负载上落后 4%–21%；其中 d6.25 连续的 +1.7% 很小，不宜视为稳健胜出。
这也是各自九档取最好中位数的描述性比较，不能替代独立验证或逐次配对。

## 五、MGLRU 与传统 LRU

![同一份策略代码在 MGLRU 与传统 LRU 下的效果](assets/20261109-k20-matrix/mglru-vs-classic.png)

| 负载 | LRU | 4 KiB | 64 KiB | 256 KiB | 2048 KiB |
|---|---|---:|---:|---:|---:|
| d25 连续 | MGLRU | +106% | −25% | −9% | +374% |
| | 传统 | 0% | −48% | −38% | +266% |
| d6.25 分散 | MGLRU | +29% | +1279% | +1078% | +1034% |
| | 传统 | −19% | +1078% | +874% | +916% |
| 8 线程全扫描 | MGLRU | +77% | +15% | +17% | +13% |
| | 传统 | −22% | −7% | −8% | −5% |
| GAPBS BFS | MGLRU | +14% | +100% | +98% | +109% |
| | 传统 | −58% | −25% | −25% | OOM |
| Memcached | MGLRU | +9% | | | +1842% |
| | 传统 | −39% | | | +1055% |

传统 LRU 的匿名与 GAPBS 数字是单次运行，Memcached 是 3 次中位数。

- 传统 LRU 下 policy 在 4 KiB 档全部持平或变慢，大档位的收益也比 MGLRU 小。GAPBS BFS 在 MGLRU 下各档领先，在传统 LRU 下各档落后。
- Memcached 4 KiB 档 policy 在传统 LRU 下为 139,401 QPS、44.6 µs/请求、GET p99 10,234 µs，与 11-04 内核 #16 的 `0xbf`
  （145,546 QPS、42.2 µs）一致；MGLRU 下为 261,942 QPS、24.8 µs/请求。
- 两种 LRU 的差别在于 `MGLRU` 位（`0x100`）和运行时 MGLRU 开关：MGLRU 下 Hermit 不做 PTE 冷页观察及其 TLB 刷新、不对 MGLRU 候选排序，
  并让已通过 MGLRU 引用检查的页参与 SG 合并写出。11-04 的消融已显示冷页观察每次请求约花 11 µs、排序约 5 µs，与上面 20 µs 的差距量级一致；
  本轮没有在传统 LRU 下做逐位消融，不能分开各项的份额。
- 结论：当前策略应以 MGLRU 为前提。传统 LRU 路径上的冷页观察和排序没有带来可测的收益。

### 传统 LRU 矩阵为什么中断

第 46 个单元 `gapbs-bfs-max2048k-r1-static-1999` 在第 1 次 trial 的验证阶段被 memcg OOM 杀掉
（`Memory cgroup out of memory: Killed process ... (hermit-gapbs-bf)`）。当时 cgroup 上限 1,519,616 kB，swap 已用约 810 MB，
`anon_thp` 约 499 MB。驱动在这个单元失败后结束了该组，剩余 206 个单元没有运行。

`memory-events-after.txt` 中 `oom_kill 0` 是 trial 开始前的快照，不能据此判断有没有 OOM，要看 `dmesg-after.txt`。
同一单元在 MGLRU 下没有 OOM。传统 LRU 在 2 MiB 上限下回收大 folio 较慢，验证阶段额外分配内存时来不及腾出空间；
这是推测，没有单独复现。补跑时可以把传统 LRU 下 GAPBS 的 `LOCAL_RATIO_PCT` 提高到 80，或降低 `--gapbs-scale`。

## 六、与 Hermit 原生及 Memtis 的设计比较

本节将 **原生 Hermit（NSDI 2023、Linux 5.14）**、**Memtis（SOSP 2023）**和**当前 Linux 6.18 policy**分开讨论。
本轮没有运行前两套系统，只能比较机制。图中的 off 是同一个 #20 内核关闭新策略后的基线，已经包含本项目的 mTHP、
多阶远程存储和 6.18 移植修改；MGLRU 普通矩阵还关闭了主动回收与 speculative I/O。因此，`policy/off`
不能直接写成“比原生 Hermit 快多少”，也不是对原论文性能的复现。

### 三套设计分别优化什么

原生 Hermit 的核心是根据反馈决定异步工作的启用、并行程度和调度，将非紧急操作移出或重叠于缺页关键路径，
降低远程 swap 的软件成本，见 [Hermit 论文及摘要](https://www.usenix.org/conference/nsdi23/presentation/qiao)。
Memtis 面向 CPU 可直接访问的快慢内存，联合优化页的热度分类、放置和大小；当前 policy 则主要决定远程缺页时读多少、
采用哪些 folio，以及如何合并传输。三者的反馈信号和决策对象不同。

| 设计问题 | 原生 Hermit | Memtis | 当前 Hermit policy |
|---|---|---|---|
| 大小页空间 | 关键的 bypass 换入路径分配 4 KiB 页；Linux THP 仍可使用，没有本项目的多级 mTHP 选阶 | 主要在 2 MiB THP 与 4 KiB 基本页之间选择 | 允许 4 KiB、16–2048 KiB 多级 folio；配置上限只是候选范围 |
| 初始行为 | 按需读取基本页，可同时预取邻近 swap 页；原仓库建议 THP 使用 `madvise` | 默认利用 THP，再谨慎拆分 | 初始填充仍可分配 mTHP；远程换入通常从 4 KiB 预测批次开始 |
| 观察对象 | VMA 的相邻缺页和预取命中；异步 I/O 失败、回收速度、refault 等运行反馈 | 实际页及 4 KiB 子页的访问热度、分布和偏斜 | 线程/PC/VMA 的连续需求、`fit`，以及按 mm 共享的 fault-PC 采样足迹 |
| 细粒度决策 | 调整 VMA 预取窗口；按需 I/O 完成后可停止继续发预取 | 比较实际快层命中率与模拟基本页命中率，估计拆分收益 | 短流、稀疏流通常换入独立 4 KiB folio，并用 SG 合并传输 |
| 大页维持 / 恢复 | 没有当前的长流门槛或 Memtis 式热度驱动拆并控制器 | 优先拆偏斜大页；全部子页热时才考虑合并 | 长顺序流满足门槛后尝试最大合法 folio；没有完整的驻留页拆并闭环 |
| 批量与后台工作 | 已有批量 TLB 刷新、记账和换出，以及反馈控制的异步回收 | 后台采样、迁移和大小页转换 | 在回收路径合并 SG 写出；后台采样，缺页路径预测、读入和安装 PTE |
| CPU 控制 | 控制后台回收并发和调度；不是 PEBS 采样预算控制器 | 根据采样线程 CPU 开销调节周期，默认目标上限为单核 3% | 本轮固定采样周期；sampler 实测约单核 0.27%–0.30%，不含整个策略成本 |

Memtis 一列依据论文 [§4.1.1、§4.3](https://multics69.github.io/pages/pubs/memtis-lee-sosp23.pdf)，细节见
[Memtis 算法与源码分析](../research/memtis-algorithm-analysis.md)。这些采样 CPU 数字来自不同平台、负载和事件配置，
不能用于宣称当前实现比 Memtis 更高效。原生 Hermit 的实现依据公开仓库提交 `10dfa5c91831`，下面给出具体代码位置。

### 相比原生 Hermit，当前策略改变了什么

**预取的状态和粒度不同。** 原生 `hermit_swap_ra_info()` 使用 VMA 级的上次缺页地址、窗口和预取命中数，
形成向前、向后或居中的窗口，并受 `page_cluster`、VMA 和 PMD 边界限制；它不是按线程/PC 学习访问段长度。
当前 policy 将流区分到线程、PC 和 VMA，使用 `fit` 限制后续预测范围，PEBS 足迹补充预测；每次缺页最多读 64 个
4 KiB 页，每个 SG 批次最多 16 页。原生对应实现见
[swap_state.c](https://github.com/uclasystem/hermit/blob/10dfa5c918310559376069160bec6609a9a41fd4/linux-5.14-rc5/mm/swap_state.c#L952)。

**缺页路径存在不同取舍。** 原生 `hermit_swapin_bypass_swapcache()` 可先发按需 RDMA READ，再做 memcg charge；
`hermit_vma_prefetch()` 在按需读完成时停止继续发预取。当前 4 KiB 策略路径通过 swap cache 准备多个 folio，提交所有批次，
等待完成后处理 PTE；它用本次缺页中更多的准备和映射工作，换取后续缺页与请求数的减少。原生的异步重叠本身不是本项目新增，
原生路径也不能简化为“同步单页读取”。依据
[原生 memory.c](https://github.com/uclasystem/hermit/blob/10dfa5c918310559376069160bec6609a9a41fd4/linux-5.14-rc5/mm/memory.c#L3574)
和当前 [hermit_swapin_pages()](../../linux-stable/mm/swap_state.c)。

**批量回收原本就有；SG 合并是另一层优化。** 原生 `batched_pageout()` 已批量刷新 TLB、提交换出并处理记账，
见 [原生 vmscan.c](https://github.com/uclasystem/hermit/blob/10dfa5c918310559376069160bec6609a9a41fd4/linux-5.14-rc5/mm/vmscan.c#L1676)。
当前策略进一步按 VMA/地址关系组织候选，并在远端布局允许时把多个独立 4 KiB 页装入一个 SG 请求。
“批处理 N 个页”“一个请求传输 N 个页”“分配一个 N 页大小的 folio”是三件事；本轮写请求减少不能全部归因于首次引入批处理。

**后台 CPU 的安排也发生了变化。** 原生默认最多 16 个回收 worker，控制器结合内存余量、换入/换出速度等决定并发量；
初始化时将 worker 的 CPU 设为 `num_online_cpus() - 1 - i`，通过 `schedule_work_on()` 指定执行位置。
见 [原生 CPU 初始化](https://github.com/uclasystem/hermit/blob/10dfa5c918310559376069160bec6609a9a41fd4/linux-5.14-rc5/mm/hermit.c#L114)
和 [回收控制器](https://github.com/uclasystem/hermit/blob/10dfa5c918310559376069160bec6609a9a41fd4/linux-5.14-rc5/mm/hermit_utils.c#L96)。
这只是原生代码的默认规则，不是本轮实测绑核。当前 Memcached 使用 4 个 unbound 回收 worker、64 MiB headroom，
`hermit_pebsd` 与服务端共享 CPU 0–7；不能套用原生 worker 的 CPU 编号来解释本轮开销。

### 实验结果怎样对应这些差异

1. **大小页选择与传输合并已经分开。** 当前 policy 可以保留 4 KiB 的换入粒度，同时发送多页 RDMA 请求。
   Memtis 侧重驻留页的大小与层间放置；把“大请求”当作“大页”会把两套设计比较错位。
2. **当前 policy 的局部决策尚未统一优化驻留存量。** Memcached 换入几乎全为 4 KiB，驻留存量却主要仍有 256/512 KiB folio；
   这正是本报告同时展示两类图的原因。不能据此声称 Hermit 已经完成了 Memtis 式的全局大小页优化。
3. **减少多读还不够。** d50/d25 连续访问及 Redis 在合适的中间档位仍落后，说明学习到访问长度后，还需要减少慢启动和逐页映射开销。
   可借鉴 Memtis 对细粒度收益与大页收益的联合考虑，但 RDMA 的请求固定成本、远端布局约束和缺页路径成本需要单独建模，
   不能直接套用快慢内存命中率与访问延迟差。
4. **原生 Hermit 对缺页关键路径的关注仍然适用。** 当前策略降低总 I/O，并不保证每次缺页或每个请求的尾延迟都降低；
   Memcached 4 KiB 档吞吐上升而 GET p99 仍增加约 4%。批次等待和预测成本值得拆分测量，但本轮没有证据把这 4% 归因到某一个函数。
   若要比较原生 Hermit 的性能，需要单独运行其 5.14 内核，并对齐内存预算、THP、回收设置、绑核和客户端能力。

## 结论边界

- **重复次数。** MGLRU 各组 3 次重复，大多数档位 off 三次差距小于 5%。差距大的档位（8 线程全扫描 512–2048 KiB 为 19%–58%，
  Redis 1024 KiB 为 39%，分散访问 256–1024 KiB 为 10%–30%）只看方向。传统 LRU 的匿名和 GAPBS 只有 1 次。
- **测量时长。** Redis 每单元 0.8–2.6 s，d1p 的 policy 单元 0.02–0.03 s，d6.25 的 policy 单元 0.4–0.8 s；这些吞吐只看方向。
  GAPBS（8–47 s）、YCSB（16–30 s）、Memcached（120 s）计时可靠。
- **CPU 归因。** SMT 开启，但应用没有在同一核上绑定两个 sibling；后台线程并未全部隔离。只有 Memcached 保存了
  同测量阶段的进程与 sampler CPU 时间。客户端接近满载是否限制吞吐仍需验证，见第三节。
- **实际页大小。** 换入 folio 分布是按成功读取字节统计，包含预取与重复读取；完整驻留普查仅有 Memcached。
  GAPBS I/O 与页大小计数包含验证阶段，吞吐则只使用 kernel trial 时间。
- **内核间比较。** #19 的密度结果和 #18 的 GAPBS 结果没有保留原始目录，只有当时的汇总数字。
- **收益来源没有逐项拆分。** 本轮没有做 MGLRU 下的逐位消融。4 KiB 档的收益和写请求的减少一致，但没有关闭 `BATCH` 位验证；
  PEBS 投票的贡献也没有单独测（之前各轮中 `predict_extended` 都很少）。

## 下一步

1. **连续访问的 16–128 KiB 档。** policy 每段要经过几次增长才形成较大的批次（每个 SG 批次最多 16 页，每次缺页最多 64 页）。可以评估用 `fit` 直接给出段首批次大小，
   而不是从 1 页开始翻倍；验收标准是 d50/d25 在 64 KiB 档的读请求/页不高于 off，并且分散访问不变慢。
2. **补跑传统 LRU。** 调整 GAPBS 的本地比例后补跑剩余 206 个单元；或只保留 Memcached 和匿名负载，作为 MGLRU 依赖性的对照。
3. **Memcached 客户端。** 增加客户端 CPU 或线程，确认 policy 的吞吐上限。
4. **MGLRU 下的消融。** 至少测 `0x1bf`、关闭 `BATCH`（`0x19f`）、关闭 `PEBS`（`0x1bd`），区分换出合并、换入预测和 PEBS 投票的贡献。
5. **GAPBS 的流表容量。** BFS 每单元约 1,000 次 `stream_evictions`。可以测 16 路或更多组，看 refault 和吞吐是否变化。
6. **清理后的内核。** 2026-11-09 删除旧 mTHP 选阶后重新构建，至少重跑 density-high 和 Memcached，确认结果不变。
7. **补齐 CPU 与驻留页观测。** 为非 Memcached 负载保存应用阶段的进程/线程 CPU、`hermit_pebsd` affinity 与运行时间，
   记录回收 worker 的调度范围；对目标匿名映射做分阶段 folio 普查，避免把库/共享页混入负载工作集。

## 数据与图表复现

原始结果目录在 dnet-61 `/home/xwz/`：

| 组 | 目录 |
|---|---|
| density-high / density-low | `k18-k20-density-high-20261108-144414`、`k18-k20-density-low-20261108-155817` |
| anon8t / gapbs | `k18-k20-anon8t-20261108-171635`、`k18-k20-gapbs-20261108-174117` |
| redis / ycsb | `k18-k20-redis-20261109-035557`、`k18-k20-ycsb-20261109-042747` |
| memcached | `k18-k20-memcached-20261109-063745` |
| classic / classic-mc | `k18-k20-classic-20261109-085852`（未完成）、`k18-k20-classic-mc-20261109-100021` |

随文数据在 [assets/20261109-k20-matrix/](assets/20261109-k20-matrix/)：

- [samples.csv](assets/20261109-k20-matrix/samples.csv)：936 个工作负载单元，含读写请求、读入/访问比值和应用阶段的策略计数；
- [memcached.csv](assets/20261109-k20-matrix/memcached.csv)：66 个 Memcached 单元，包含服务端、客户端与 sampler CPU 秒；
- [folio-sizes.csv](assets/20261109-k20-matrix/folio-sizes.csv)：全部单元的 folio 读取/写出大小分布；普通矩阵区分 populate/application；
- [resident-folios.csv](assets/20261109-k20-matrix/resident-folios.csv)：Memcached 加载后、预热后、测量后的驻留映射大小分布，size=0 为 unknown；
- [cpu-activity.csv](assets/20261109-k20-matrix/cpu-activity.csv)：每单元 64 个逻辑 CPU 的活动比例，`scope` 明确计量区间；
- [cpu-context.json](assets/20261109-k20-matrix/cpu-context.json)：逻辑/物理 CPU 对应关系、各负载绑核、回收配置与原始证据路径；
- [source-context.json](assets/20261109-k20-matrix/source-context.json)：内核版本和各目录 `summary.csv`/`plan.json` 的 SHA-256，另保存 Memcached manifest/拓扑文件哈希。

所有 11 张图都有同名 PNG 与 SVG；吞吐图为三次中位数与 min–max 阴影，大小分布图为三次归一化分布的平均值。
图中的上限和 folio 大小统一使用 KiB，不将 1024/2048 简写成容易与 KiB 坐标混淆的 M。
在仓库根目录执行：

```bash
# 从完整原始目录重建 CSV：需要逐单元 measurement/CPU 快照，以及 Memcached 的 pages-* 普查
python3 docs/pebs/assets/20261109-k20-matrix/extract.py /path/to/k20
# 只用随文 CSV 重建图表；--tables 打印本文的表格数据
MPLCONFIGDIR=/tmp/hermit-matplotlib python3 docs/pebs/assets/20261109-k20-matrix/render.py
MPLCONFIGDIR=/tmp/hermit-matplotlib python3 docs/pebs/assets/20261109-k20-matrix/render.py --tables
```
