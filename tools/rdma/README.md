# RDMA 测试工具

本目录集中保存 RDMA 基准、PEBS 实验、离线分析、数据集与结果。以下命令均从仓库根目录执行。

| 位置 | 用途 |
|---|---|
| 根目录 `run_*.sh`、`memcached*.sh` | Hermit page sweep 入口（anon/memcached/redis/xgboost/ycsb）与 Memcached 控制/负载脚本 |
| `common.sh`、`create_cgroup.sh` | 共享配置、结果根目录与 cgroup 支持 |
| `anon_seq_workset.c`、`redis_bench.py`、`xgboost_train.py` | 负载程序，供基准脚本调用 |
| [pebs/](pebs/) | PEBS 开关、频率矩阵与正式结果汇总 |
| [bench/](bench/) | 换入粒度微基准（S 页段、逐 order 缺页成本）与驱动脚本 |
| [baseline/](baseline/) | native local/cgroup-linux sweep、PEBS 前基线、续跑与恢复检查 |
| [tests/](tests/) | 无需 RDMA 服务器的源码回归测试 |
| [results/](results/README.md) | 历次测量、构建日志、图表和归档 |

## PEBS

当前设计和操作步骤见 [策略内核集成](../../docs/pebs/kernel-policy-integration.org)。
旧 mTHP 成本选阶和强制 order 接口已移除；[旧设计](../../docs/pebs/design.md)仅用于解释历史实验。

- Memcached 采样开销：`pebs/run_memcached_sampling_cost.py`，固定并发，比较同页大小的采样开关。
- 多负载矩阵：`pebs/run_workload_matrix.py`，默认 1323 个样本（去掉 XGBoost 为 1134），PEBS 按工作负载 pid 启用。`--pages` 表示每轮**最大分配页大小**，保留全部较小 THP 档位。
- 页面选择微基准：`pebs/run_page_selection_microbench.py`。
- 换入粒度微基准：`bench/run_swapin_granularity.sh`，逐 trial 报告 S 页段的墙钟、缺页和校验，逐单元差分 `swapin_cost_stats`；`--policy-features` 接受 `off` 及消融预设。

两个矩阵入口都支持 `--policy-features`：给 policy 单元指定 `policy_features` 消融预设
`default`（`0x1bf`）、`no-pebs`（`0x1bd`）、`no-batch`（`0x19f`）、
`medium-on`（`0x3bf`）、`fitstart-on`（`0x5bf`）、`ptebatch-on`（`0x9bf`）、
`fitpte`（默认 + FITSTART + PTEBATCH，`0xdbf`）、`fitpte-medium`（再加 MEDIUM，
`0xfbf`）或原始掩码（上限 `0x3fff`）。11 月 19 日粒度实验的 `0xcbf` 关闭了 MGLRU 位。
预设在同一矩阵内各自成为一个带标签的 case，与同一块的 `off`/`static` 共享 baseline；
不传该参数时保持原行为（不改写 `policy_features`）。详见
[策略内核集成](../../docs/pebs/kernel-policy-integration.org)。

```sh
python3 tools/rdma/pebs/run_workload_matrix.py --dry-run --suites anon-1t-full \
  --pages 64 2048 --periods 1999 --repeats 1 --policy-features default no-pebs no-batch
```

Memcached 使用 `pebs/pebs_perf_matrix.py`；加 `--pages` 后按相同的最大分配页口径扫描。
默认每档为 `off` 加三个周期的 static/policy，`off` 仅关闭 PEBS，所有 case 保持同一后端掩码。
旧 case 名 `off-same-pages` / `off-original-mask` 仍可指定，但现在与 `off` 使用相同掩码，
不再复现旧脚本中 `off` 强制 `0x1` 的条件。

```sh
python3 tools/rdma/pebs/pebs_perf_matrix.py --help
```

旧版 `pebs_perf_matrix.py` 用 root 执行并通过 `--user` 指定负载用户。当前多负载矩阵用普通用户执行，预先运行 `sudo -v`，并使用新的输出目录：

```sh
python3 tools/rdma/pebs/run_workload_matrix.py \
  --suites anon-1t-full anon-1t-chunk64k anon-8t-full anon-8t-chunk64k redis ycsb \
  --pages 4 16 32 64 128 256 512 1024 2048 \
  --output tools/rdma/results/pebs/matrix-max-pages --dry-run
```

移除 `--dry-run` 才执行负载。每轮只设置 THP 分配上限，例如 64KiB 轮允许 4/16/32/64KiB，2MiB 轮允许所有较小档位；实际分配及预取粒度由内核和 Hermit 决定。off/static/policy 和采样周期仍按原来的配对、随机顺序、重复次数运行。

`--suites` 还可选两组不在默认集合中的负载：

- 访问密度：`anon-1t-d50-high`、`anon-1t-d25-high`、`anon-1t-d6.25-high`（每个 2 MiB 区域访问一段连续页）、`anon-1t-d25-low`、`anon-1t-d6.25-low`（按互质步长分散）和 `anon-1t-d1p-high`（每区域 1 页），均为单线程、随机区域顺序。
- 图负载：`gapbs-bfs`（16 次 trial）和 `gapbs-pr`（4 次 trial），8 线程，每次 trial 都验证。需要 `--gapbs` 指向 GAPBS 1.5 源码目录（默认 `~/pebs-apps-20261031/gapbs-1.5`）和 g++；`--gapbs-scale` 默认 24。`run_gapbs_page_sweep.sh` 以 `-include tools/rdma/gapbs_hermit_hook.h` 编译，建图完成后等待 SIGUSR2，降低 `memory.max` 后再开始计时。

内核 #20 的测试用法和结果见 [11-09 报告](../../docs/pebs/policy-results-20261109.md)：`--periods 1999 --repeats 3`，九档上限。传统 LRU 下 GAPBS BFS 在 2048 KiB 上限、`LOCAL_RATIO_PCT=70` 时会触发 memcg OOM；`memory-events-after.txt` 早于 trial，判断 OOM 要看 `dmesg-after.txt`。

Redis 及 YCSB 的 Redis 服务统一使用 `--disable-thp no`，启动完成后、装载数据前保存
`redis-*-status-before.txt` / `server-status-before.txt`，要求 `/proc/PID/status` 的
`THP_enabled: 1`。Redis 7.4.1 默认 `disable-thp yes`：发现顶层 THP 为 `always`
就会通过 `PR_SET_THP_DISABLE` 禁止整个进程使用大页，使 2MiB 轮次退化为 4KiB，
禁止大 folio 路径；仅记录 sysfs 不能发现这种进程级限制。4KiB 轮次仍由 THP 上限
禁止分配大页。已有的零决策校验继续保留。

运行前应准备好 Hermit、RDMA 和 swap。多负载脚本保留 `remote_order_mask`、学习阈值以及回收/批处理参数，只记录其值；固定周期 case 设置 `pebs_adaptive=0`。后端缺少上限以内的档位会在启动检查中报错。例如完整 4KiB–2MiB 矩阵要求 `effective_order_mask` 包含 `0x3fd`，旧实验恢复的 `0x1` 不满足条件。脚本不会替换这个掩码。退出时恢复本次修改的 THP 设置及采样模式/周期/自适应开关。

矩阵为保持 off/static 的直接换入条件，启动前要求 `bypass_swapcache=Y`。新内核的 policy 组使用独立的 4 KiB 预测及批次换入路径。采样量非零不能证明策略参与换页。先在空闲测试机上完成以下一次性准备，off/static/policy 全程使用相同设置：

```sh
sudo -v
printf '0x3fd\n' | sudo tee /sys/kernel/debug/hermit/remote_order_mask
printf 'Y\n' | sudo tee /sys/kernel/debug/hermit/bypass_swapcache
sudo cat /sys/kernel/debug/hermit/effective_order_mask \
  /sys/kernel/debug/hermit/bypass_swapcache \
  /sys/kernel/debug/hermit/pebs_enabled
```

预期依次为 `0x3fd`、`Y`、`0`。多负载脚本不会自动改写 `bypass_swapcache`，也不会在结束时撤销上述手工准备。以下为可选的 7 次小矩阵（包含一个 4KiB-only 对照，输出目录必须不存在）：

```sh
python3 -u tools/rdma/pebs/run_workload_matrix.py \
  --suites anon-1t-chunk64k --pages 64 2048 --periods 1999 --repeats 1 \
  --output "tools/rdma/results/pebs/policy-smoke-$(date +%Y%m%d-%H%M%S)"
```

新内核按**应用阶段** `policy_stats` 和 batch read WR 的增量判定策略活动。
存在远端 I/O 的 policy case 需要看到冷页观察、预测、回收排序或批次读请求的证据；
单页 batch read 也表示策略路径已执行，不要求一定预取或形成大请求。无远端 I/O 的
常驻测试会标记路径未执行，但不会因此拒绝采样开销结果。旧内核无 `policy_stats` 时，
仍检查大于 4KiB 档位的旧选页计数。准备阶段活动、单纯采样量不能替代应用阶段证据。

在 dnet-61 上，下面两个命令应依次运行，不能与其他矩阵并行。Memcached 为 189 个
样本，Redis/YCSB 为 378 个样本；均为九档页面、三个周期、三次重复。Memcached
保留原有 800000 条 1KiB value、640MiB `memory.high`、2GiB `memory.max` 压力条件；
Redis/YCSB 保留原有 16GiB 数据、70% 驻留内存预算，YCSB 默认 Redis binding。
它们的内存预算口径与新应用 `run_policy_app_matrix.py` 不同，结果应在同一负载内配对。

```sh
# Memcached：root 驱动将应用降权为 xwz；off 只关闭策略
sudo python3 tools/rdma/pebs/pebs_perf_matrix.py \
  --user xwz --server-cpus 0-7 --client-cpus 16-31 --sampler-cpus 0-7 \
  --pages 4 16 32 64 128 256 512 1024 2048 \
  --records 800000 --memory-mb 640 --connections 32 --qps 0 \
  --warmup 30 --duration 120 --repeats 3 --require-fixed \
  --output "$HOME/memcached-policy-$(date +%Y%m%d-%H%M%S)"

# Redis + YCSB：以 xwz 运行，不要在 python3 前加 sudo
sudo -v
python3 -u tools/rdma/pebs/run_workload_matrix.py \
  --suites redis ycsb \
  --redis /home/xwz/redis/src/redis-server \
  --ycsb /home/xwz/ycsb-0.17.0/bin/ycsb \
  --pages 4 16 32 64 128 256 512 1024 2048 \
  --periods 19997 1999 199 --store-period 1500003 --repeats 3 \
  --output "$HOME/redis-ycsb-policy-$(date +%Y%m%d-%H%M%S)"
```

两个入口都支持 `--dry-run`，不要求 sudo，也不创建输出目录。Memcached 的
`run_memcached_sampling_cost.py` 继续用于常驻/压力对照，其默认 baseline 同样为 `off`。

额外明确标记 **`4KiB-only` 对照**：关闭全部 THP/mTHP 和 PEBS，复用每次重复中的
`max_page_kb=4, case=off` 单元，不重复运行相同负载。指定的 `--pages` 不含 4 时，
驱动自动为每个负载、每次重复补一个 4KiB/off 单元。该标记指分配页面大小，
后端能力掩码保持一致，实际 RDMA 请求大小仍独立记录。`paired.csv` 同时输出
相对 `off_same_page` 和相对 `4KiB-only` 的吞吐提升、p99/WR/换页计数下降比例，
严格匹配负载与重复编号；缺失或失败的 baseline 保留标记，不借用其他重复。
不带 `--pages` 的 Memcached 常驻/采样开销测试仍保留系统 THP 设置，不加入该对照。

匿名负载的逻辑访问区域固定为 2MiB（单独运行时可设 `ACCESS_REGION_KB`），`chunk64k` 始终访问每个区域的 64KiB；访问量不再随页上限变化。16GiB 工作集的覆盖量固定为 512MiB。历史字段 `pages_per_folio` 表示逻辑区域的页数，不代表实际分配 folio。

新结果标记 `page_policy=max-allocation-v1`，包含：

- `summary.csv`：每轮 `max_page_kb`、应用吞吐、计时及有效性，以及 `application_policy_decisions`、`policy_path_exercised`。匿名吞吐按访问页面覆盖量计算，每页实际读取一个字节；Redis 为 GET/s、YCSB 为 ops/s、XGBoost 为 rounds/s。
- 每轮 `status.json` 的 `policy_activity`：是否要求选页路径生效、应用阶段决策数及路径是否生效；具体失败原因在 `validation_issues`。
- `granularity.csv`：以 `size_kib` 记录实际请求大小，普通 order WR 与同大小的 SG WR 相加；12KiB 等非二次幂请求的 `order` 留空。`batch_*` 是总量的子集，不能再次相加。多负载入口还按准备/应用阶段记录 folio 请求及旧 PEBS 计数；Memcached 记录测量阶段 WR。folio 请求大小与 WR 大小分别计量。
- `summary.csv` 增加应用阶段 `read_wrs/write_wrs/read_bytes/write_bytes`、`pswpin/pswpout` 和 `policy_*` 计数。Memcached、YCSB 保存延迟，Redis 扫描负载没有逐请求延迟分位数；缺失值留空。
- 每轮 `measurement.json` 和原始 `order-*.txt`、`.wr`、`.pebs`、`.thp`：保留性能、粒度分布及实际 THP 设置。原负载 CSV 的 `page_kb` 是上限，`target_*` 只统计该上限 order；分析所有粒度应使用 `granularity.csv`。

后端计数是整机计数，需要独占测试机；folio 请求大小、PEBS 决策和实际 WR 粒度不可互相替代。旧结果的单档限制及匿名访问量与新版本不同，不应直接混合汇总。历史图表与结论见 [PEBS 评估](../../docs/pebs/evaluation.md)，原始数据在 `results/pebs/`。

## 其他基准和基线

- [Memcached 与匿名内存部署及测试](../../docs/rdma/dnet58-dnet61-memcached-rdma.md)
- [Redis](../../docs/rdma/redis-hermit-6.18.md) · [XGBoost 与 HIGGS](../../docs/rdma/xgboost-hermit-6.18.md)
- [添加 PEBS 前的基线记录](../../docs/archive/rdma/prepebs-baseline-20260924.md)

`baseline/` 的运行及续跑脚本对应特定旧内核、原始输出目录和源码快照，保留其版本检查；它们不是当前 PEBS 内核的通用测试入口。已归档的 `driver.py`、`source/` 和内核补丁保留实验时内容，不随当前工具目录改写。

## 本地回归

```sh
python3 -m pytest tools/rdma/tests -q
```

- `test_kernel_paths.py`：提取生产源码并编译宿主机 C harness，覆盖 RDMA 提交边界、失败排空、远端读取错误、换入分配回退和最大粒度换出。
- `test_pebs_policy.py`：执行生产窗口、fault-PC 足迹表和 4 KiB 预测，验证时间边界、跨区域学习、隔离、淘汰和失效。
- `test_pebs.py`：PEBS 作用域、mm 生命周期、Memcached 开销、THP、矩阵驱动、page-selection 微基准与绘图流程。

QEMU 构建及回归仍位于 [tools/qemu-dram](../qemu-dram/)，其临时输出位于 `_work/`。

## 输出约定

通用基准默认写入 `tools/rdma/results/`，可用 `RESULT_ROOT` / `RESULT_DIR` 覆盖；PEBS 矩阵与旧基线驱动要求显式输出目录。数据集使用 `tools/rdma/data/`，通过 `XGB_DATA_FILE` 指定解压后的输入文件。大型输入、原始结果和预览文件由 Git 忽略，入口索引保留可跟踪状态。
