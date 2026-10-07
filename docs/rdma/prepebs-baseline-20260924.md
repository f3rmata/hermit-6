# PEBS 前 Hermit baseline（2026-09-24）

**已完成：2026-09-24 23:47:16（北京时间）结束最后一档测试，系统恢复核验通过。**
正式性能矩阵包含 270 个样本：anon 108、Redis 27、YCSB 27、XGBoost 27、
历史推荐回收配置 Memcached 81。每个性能条件均重复三次。

**稳定性存在失败：默认回收配置的 Memcached 九档均发生 cgroup OOM，
另一次受 SSH 中断影响的推荐配置运行也发生 OOM，全程共 10 次。**
默认配置保留 20 个完成样本作为诊断数据；中断运行的六个样本整档排除；
另有六个 smoke 样本，均不混入上述 270 个正式性能样本。
其余八个默认 Memcached 页档于 19:02:35–19:55:32 分别运行，失败隔离记录在
`/home/xwz/hermit-baselines/20260924-prepebs-memcached-continuation`。

默认配置的 Memcached 结果用于稳定性记录。另按
[历史 70% hard-limit 配置](../migration-6.18.md#memcg-reclaim)补测
`reclaim_mode=1`、`reclaim_headroom_pages=65536`、`sthd_cnt=16`，
其余负载参数不变，结果单独保存为 `20260924-prepebs-memcached-tuned`。
推荐配置于 19:57:08 启动；4–64 KiB 的 36 个样本完整通过，随后跳板 SSH 连接
中断，首轮于 21:34:03 退出。128 KiB 已写出的六个样本整档排除，不纳入正式
汇总。恢复原始控制项后，于 21:57:33 从 128 KiB 开始重跑剩余五档，结果目录为
`20260924-prepebs-memcached-tuned-resume`，两段 SSH 均启用 15 秒保活。
续测五档共 45 个样本完整通过，exit_code=0、无新增 OOM；与首轮完整的
4–64 KiB 合并为九档 81 个样本。恢复时还发现旧 128 KiB
进程 PID 883470 的一次 cgroup OOM（内核时间 2248221.726737，按主机 btime
估算约 21:37:01）。该受损运行同时存在 SSH 中断、sudo 清理失败和 OOM，不能
剥离其因果关系，更不能把推荐配置称为已经解决 OOM 根因。原始 completion.txt、
部分 CSV、日志以及标明来源的恢复快照均保留，不改写为成功。
该配置仍为同一个 PEBS 前内核；不能把两个回收配置的差异解释为 PEBS 收益。

## 内核与环境

- 主机：dnet-61；源码 HEAD `330b5850d223`，内核 `6.18.38-hermit #3`，
  构建于 2026-08-29，源码未包含 `mm/hermit_pebs.c`，debugfs 无 pebs 控制项。
- `/boot/vmlinuz-6.18.38-hermit` 与源码树 bzImage 的 SHA-256 相同：
  `763dec7f263a1d58eaa987e83fda00f0932eec8bb5a6452cff4411faa5cf9cf4`。
- 已加载 RDMA client 与磁盘模块的 srcversion 相同：`7A97897C64CE82118F92A4D`。
- 远端内存端点 `172.16.0.58`，客户端 mlx5_0 位于 NUMA 0，端口 ACTIVE；
  48 GiB `/dev/sdb6` 块设备 swap，预检前 used=0。
- 保留现有内核、模块、swap 和 memory server；未安装本地 PEBS 修复版。
- 应用版本：Redis 7.4.1（版本输出 malloc=jemalloc-5.3.0）、Memcached 1.6.42、
  XGBoost 3.4.1、YCSB 0.17.0；二进制 SHA-256 见 `state/final-audit.txt`。
- 双路 Xeon Silver 4216，64 个逻辑 CPU，125 GiB 内存；
  NUMA 0 的主线程 CPU 为 0–15，SMT sibling 为 32–47。

## 运行参数

全套结果目录：`/home/xwz/hermit-baselines/20260924-prepebs-full`。
该目录中的 source/ 为实际执行脚本快照，source-sha256.txt 记录校验和。
远端原有脚本修改保持不动；仅在快照中覆盖 stats helper 查找函数，使用
正确的 long* ABI 用户程序。它不会改变内核数据路径。

首轮默认配置参数：九档 `4 16 32 64 128 256 512 1024 2048` KiB，每档三次；
本地内存比例 70%；bypass_swapcache=Y、speculative_io=Y、lazy_poll=N、
apt_reclaim=Y、reclaim_mode=0、reclaim_headroom_pages=2048、sthd_cnt=16。
测试结束恢复 Hermit 控制项和 THP 配置。

| 组 | 配置 | 计划样本数 |
|---|---|---:|
| anon-1t | 16 GiB，CPU 0，parallel-fault，全扫/chunk64k，sequential/high | 54 |
| anon-8t | 同上，8 线程，CPU 0–7 | 54 |
| redis-chunk64k | 16 GiB，2 MiB value，64 KiB GETRANGE，单实例/单客户端，CRC32 | 27 |
| ycsb-redis-full | 8192 keys × 2 MiB，8192 次整值读取，uniform | 27 |
| xgboost-higgs | 1100 万行 HIGGS，hist，30 轮，depth=8，4 线程，CPU 0–3 | 27 |
| memcached | 3200 万条 fb_key/fb_value，100k/250k/500k 请求负载，40 秒 | 81 |

服务端 CPU 0–7、客户端 CPU 8–15。XGBoost 原历史配置将 4 个线程限制在 CPU 0，
本轮使用 CPU 0–3；anon 历史部分实验使用 NUMA 1 的 CPU 16–23，本轮统一到
网卡所在 NUMA 0。因此旧结果用于量级和趋势核对，后续 PEBS 比较应复用本轮配置。
Memcached 各页档重载数据后重复测量三个 offered loads；这些重复并非独立重载。
页档按升序执行，各条件连续重复三次，未随机化执行顺序；范围和标准差用于展示
本轮波动，不据此声称相邻页档具有统计显著差异。
anon、Redis、YCSB 和 XGBoost 用 70% 限额制造换出，随后解除 memory.max
再测换入/应用运行，目的是隔离换入成本；并非全程保持 70% 内存的稳态压力测试。
Memcached 则保留限额，结果包含持续的换入换出与排队影响。

## 预检

`20260924-prepebs-smoke` 已完成：1 GiB，4/64/2048 KiB，全扫/chunk64k，
每项一次，共六个样本。checksum_errors、fallback、backend errors 均为 0。
全扫的换入带宽分别约 0.681 / 2.625 / 6.239 GiB/s；2 MiB chunk64k 的
读放大约 32，证明大粒度路径确实生效。预检不纳入正式 baseline 中位数。

## 结果解释和验收

- 同时检查成功/失败状态、逐档样本数、checksum、OOM、backend errors、
  实际 swap-in/out 和大 folio 占比；不能只看请求 mask。
- `order_stats` 是 folio 统计，不是逐 WR 观测；baseline 内核无新增 wr_stats。
- 老脚本的写出带宽可能受 1 秒轮询和快速写出影响，不作为主要结论。
- Memcached 脚本在旧版 backend debugfs 计数器不存在时填 0。因此 CSV 中
  `backend_loads_delta=0` 等字段本身不能证明没有 RDMA 活动，也不能独立
  证明零错误；实际活动和错误核验使用 vmstat、Hermit `order_stats` 边界快照
  及内核日志。逐样本 cgroup OOM 采用前后增量，不能把前轮累计值当作本轮新增。
- XGBoost 协议换入带宽以整段训练时间为分母，不能当作网卡峰值。
- 采用 `tools/rdma/baseline/summarize_prepebs_baseline.py` 对结果目录生成中位数、
  均值、标准差、范围与自动校验结果。
- 运行进度见 progress.log/stages.tsv。单次运行以 completion.txt 和样本矩阵
  验收，失败退出码永久保留。推荐配置遇到 SSH 中断后，按页档合并完整的成功
  阶段：每档必须 exit_code=0、三个负载各三次，且不得重复计数；中断页整档
  排除并重跑。最终独立核验已确认环境恢复；不能把矩阵完成等同于所有运行均无故障。

## anon、Redis、YCSB 与 XGBoost 结果

anon 两组各 54 个样本、Redis 和 YCSB 各 27 个样本，
共 162 个样本；四个阶段退出码均为 0。以下为每条件三次的中位数。

| 页档 KiB | anon 1t 全扫 GiB/s | anon 1t chunk64k GiB/s | Redis GET/s | YCSB ops/s | YCSB read p99 μs |
|---:|---:|---:|---:|---:|---:|
| 4 | 2.121 | 2.155 | 8009 | 346.4 | 6819 |
| 16 | 4.864 | 4.890 | 10109 | 435.8 | 4179 |
| 32 | 6.533 | 6.498 | 10515 | 478.3 | 3571 |
| 64 | 8.547 | 8.576 | 10764 | 488.6 | 3355 |
| 128 | 9.712 | 5.977 | 11119 | 496.7 | 3237 |
| 256 | 10.433 | 3.719 | 10044 | 487.9 | 3239 |
| 512 | 10.854 | 2.136 | 8420 | 500.0 | 3193 |
| 1024 | 11.058 | 1.145 | 6422 | 501.7 | 3293 |
| 2048 | 18.672 | 0.613 | 8074 | 380.2 | 5527 |

anon 的带宽分子为实际访问字节数。8 线程全扫在 4/64/2048 KiB 下分别为
11.977/25.975/34.293 GiB/s，chunk64k 分别为 12.108/25.971/1.072 GiB/s。
anon 的 108 个样本 checksum、backend errors 和 fallback 均为 0；
大于 4 KiB 的档位实际大 folio 占比接近 100%。chunk64k 在页档超过 64 KiB 后
读放大依次为 2/4/8/16/32，实际访问带宽随之下降。

Redis 的最佳中位数在 128 KiB，较 4 KiB 高约 38.8%；更大页档下
64 KiB GETRANGE 的读放大增加。所有 CRC32 检查通过。
YCSB 的整值读取在 64–1024 KiB 进入吞吐平台，各样本均完成 8192 次读取。
1024 KiB 的中位数较 4 KiB 高约 44.8%，但不能据此断言其显著优于邻近档位。
**Redis 和 YCSB 的 2048 KiB 档 large_load_pct 为 0，未形成大 folio，
因此该档不能作为 2 MiB 传输性能结论。**

XGBoost HIGGS 于 18:43:43 完成，27 个样本全部通过，训练集 AUC 均为
0.820776，各页档训练时间中位数如下。训练集 AUC 用于结果一致性检查，
不是独立测试集泛化精度。

| 页档 KiB | 训练秒数 | 大 folio 换入占比 % | 协议换入 GiB |
|---:|---:|---:|---:|
| 4 | 69.546 | 0 | 0.766 |
| 16 | 67.942 | 99.960 | 0.723 |
| 32 | 67.525 | 99.933 | 0.801 |
| 64 | 67.383 | 99.887 | 0.805 |
| 128 | 67.255 | 99.775 | 0.796 |
| 256 | 67.322 | 99.585 | 0.728 |
| 512 | 67.291 | 99.238 | 0.729 |
| 1024 | 66.985 | 98.748 | 0.730 |
| 2048 | 64.159 | 99.711 | 1.150 |

2 MiB 档训练中位数较 4 KiB 低约 7.75%，但换入量也不同，且 THP 分配策略
同时改变；该结果是整个应用配置的差异，不能单独归因于 RDMA 传输大小。

### Memcached 4 KiB 失败现场

100k offered QPS 三次的吞吐中位数为 99,774.7 QPS、read p99 为 38.7 μs；
250k offered QPS 分别为 248,501.0 QPS、50.6 μs。这六个样本全部 stable，
miss rate、skipped requests、evictions、backend errors、folio fallback 均为 0。

进入 `before-500000-r1` 静稳等待时检测到 OOM kill，未开始 500k 压测；
该阶段结束于 **18:59:45**。
内核日志记录 `CONSTRAINT_MEMCG`、cgroup `/hermit-baseline-mc`、
被杀进程 memcached PID 805079；内存 usage=limit=7,304,192 KiB（7133 MiB），
swap usage=3,131,908 KiB，swap limit 未耗尽。这是本轮持续内存压力下的稳定性
失败，不能归因为已经开始的 500k 请求负载，也不能仅凭该日志确定内核根因。
原始证据位于 full 目录的 `memcached.log`、`state/dmesg-after.txt`、
`results/memcached/4k/cgroup-hermit/swap-wait.csv` 和 `completion.txt`。

### 默认回收配置的完整稳定性矩阵

| 页档 KiB | 成功样本数 / 9 | 失败位置 | 阶段大 folio 换入占比 % |
|---:|---:|---|---:|
| 4 | 6 | 首次 500k 前静稳等待，OOM | 0.000 |
| 16 | 2 | 第三次 100k 前静稳等待，OOM | 97.061 |
| 32 | 1 | 第二次 100k 前静稳等待，OOM | 96.132 |
| 64 | 0 | 首次 100k 压测中 OOM，客户端 connection reset | 93.379 |
| 128 | 0 | 首次 100k 压测中 OOM，客户端 connection reset | 85.290 |
| 256 | 0 | 首次 100k 压测中 OOM，客户端 connection reset | 64.368 |
| 512 | 3 | 首次 250k 压测中 OOM，客户端 connection reset | 25.381 |
| 1024 | 4 | 第二次 250k 前静稳等待，OOM | 0.681 |
| 2048 | 4 | 第二次 250k 前静稳等待，OOM | 1.184 |

每档均重启进程并重新加载数据；失败后不继续该档剩余样本，故 81 个计划样本中
仅 20 个完成。阶段占比由相邻页档的 order_stats 边界快照差值计算，最后一档使用
续测结束快照；包含该档加载、等待、成功压测及失败过程，不能视为某次有效样本
的独立传输比例。所有页阶的 errors 增量均为 0。

20 个已完成样本均为 stable，未出现 miss、skipped、eviction 或 backend error；
这不代表长时间运行稳定，随后发生的九次 OOM 是该配置的重要 baseline 结果。
默认配置的结果也表明，较大配置页档并不保证实际大 folio 形成率高。

## Memcached 推荐回收配置的性能基线

`reclaim_mode=1`、headroom=65536 pages（256 MiB）、sthd_cnt=16，
保持 70% hard limit。下表每个吞吐／延迟值都是三个样本的中位数。

| 页档 KiB | 100k 负载 QPS | 250k 负载 QPS | 500k 负载 QPS | 500k read p99 μs | 阶段大 folio 换入占比 % |
|---:|---:|---:|---:|---:|---:|
| 4 | 99552.0 | 248270.1 | 491067.9 | 2206.5 | 0.000 |
| 16 | 99737.6 | 248287.7 | 489938.5 | 4414.4 | 97.840 |
| 32 | 99701.2 | 248131.2 | 383998.3 | 5463.7 | 96.870 |
| 64 | 99787.9 | 248042.4 | 246659.2 | 7003.9 | 94.684 |
| 128 | 99480.2 | 211988.5 | 185487.2 | 9534.6 | 91.754 |
| 256 | 99825.4 | 245290.6 | 237780.9 | 8244.7 | 81.575 |
| 512 | 99742.1 | 247424.2 | 493073.2 | 3841.4 | 15.437 |
| 1024 | 99784.5 | 248961.5 | 492850.2 | 2030.6 | 0.355 |
| 2048 | 99679.0 | 248478.0 | 492472.0 | 2150.2 | 0.748 |

81 个接纳样本均为 stable，miss、skipped requests、evictions、逐样本 OOM 增量
和可观测的 Hermit 错误／fallback 增量均为 0。完整页档的 `order_stats` errors
增量也均为 0。阶段大 folio 占比按换入字节计算，来自各档 page-state-before/after
快照，包含装载、静稳等待及全部压测；不是单次压测的独立统计。

500k 负载下，4 KiB 吞吐约 491k QPS，128 KiB 降到约 185k QPS，p99 从
约 2.21 ms 增到 9.53 ms；512–2048 KiB 配置下吞吐恢复到约 492k QPS，
但实际大 folio 换入占比分别仅约 15.44%、0.36%、0.75%。
**这种回升不能解释为超大粒度传输更优：配置页档与实际换入粒度已经明显不同。**
本轮给出可复现的静态配置基线；上述现象的因果拆分需要独立实验。

## 最终验收与归档

最后一次 2 MiB / 500k 测量前等待 212 秒，最终状态为 stable，无等待超时。
最终独立核验 `state/final-audit.txt` 的退出码为 0，确认 14 个 Hermit 控制项、
THP 总开关及八个页档开关均恢复原值；无 Memcached、mutilate、Redis 残留进程，
benchmark cgroup 为空，`/dev/sdb6` used=0。内核及磁盘模块 SHA-256、已加载模块
srcversion 与测试前一致，全部执行脚本快照校验通过。旧版 rswap backend
独立 debugfs 计数器确实不存在，其 CSV 零值不能作为独立验收依据。

`validation.json` 保留 **26 条问题记录**，对应十次 OOM、默认 Memcached
缺失条件／重复以及首轮失败状态；并非 26 个独立故障。推荐配置九档的完整矩阵
校验通过，中断 128 KiB 的六行明确列于 excluded_files，没有改写原始失败退出码。
没有新发现的 checksum 错误、Hermit order errors、内核 BUG/Oops/WARNING/soft lockup；
这不替代已记录的 OOM 稳定性问题。

本地归档位于 `tools/rdma/results/baselines/`（该目录被 Git 忽略），远端原始目录位于
`/home/xwz/hermit-baselines/`。保留五个目录：

- `20260924-prepebs-smoke`：预检六个样本。
- `20260924-prepebs-full`：五组完整测试、默认 Memcached 首档失败、最终汇总及恢复核验。
- `20260924-prepebs-memcached-continuation`：默认配置剩余八档失败现场。
- `20260924-prepebs-memcached-tuned`：推荐配置 4–64 KiB 完整结果和受损 128 KiB 运行。
- `20260924-prepebs-memcached-tuned-resume`：推荐配置 128–2048 KiB 完整续测。

受损运行的 `state/dmesg-after-recovered.txt` 来自续测的 before 快照，
`state/recovery.json` 记录恢复来源；这是本地补存证据，不伪装成原运行成功清理。
各原始运行的 driver.sh／source 快照用于追溯当时实际执行版本。

[汇总图](../../tools/rdma/results/baselines/20260924-prepebs-full/baseline.png) ·
[PDF](../../tools/rdma/results/baselines/20260924-prepebs-full/baseline.pdf) ·
[聚合 CSV](../../tools/rdma/results/baselines/20260924-prepebs-full/aggregate.csv) ·
[验收 JSON](../../tools/rdma/results/baselines/20260924-prepebs-full/validation.json) ·
[Memcached folio 与 OOM 核验](../../tools/rdma/results/baselines/20260924-prepebs-full/memcached-tuned-audit.json) ·
[环境恢复记录](../../tools/rdma/results/baselines/20260924-prepebs-full/state/final-audit.txt) ·
[完整归档](../../tools/rdma/results/baselines/prepebs-baseline-20260924.tar.gz)

从项目根目录重新生成统计和图表：

```bash
python3 tools/rdma/baseline/summarize_prepebs_baseline.py tools/rdma/results/baselines/20260924-prepebs-full \
  --extra-results tools/rdma/results/baselines/20260924-prepebs-memcached-continuation/results \
  --extra-results tools/rdma/results/baselines/20260924-prepebs-memcached-tuned/results \
  --extra-results tools/rdma/results/baselines/20260924-prepebs-memcached-tuned-resume/results
python3 tools/rdma/baseline/plot_prepebs_baseline.py tools/rdma/results/baselines/20260924-prepebs-full
```

后续 PEBS 对照应固定本轮工作负载、CPU/NUMA、回收配置、内存限制阶段、重复顺序
与应用二进制，并同时检查实际 folio 构成。默认配置的 OOM 另作稳定性回归项，
不能用已通过的推荐配置性能矩阵代替。当前结果不包含 native Linux 或 PEBS 开关对照。
