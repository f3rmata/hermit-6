# RDMA 测试工具

本目录集中保存 RDMA 基准、PEBS 实验、离线分析、数据集与结果。以下命令均从仓库根目录执行。

| 位置 | 用途 |
|---|---|
| 根目录 `run_*.sh`、`memcached*.sh` | Hermit page sweep 入口（anon/memcached/redis/xgboost/ycsb）与 Memcached 控制/负载脚本 |
| `common.sh`、`create_cgroup.sh` | 共享配置、结果根目录与 cgroup 支持 |
| `anon_seq_workset.c`、`redis_bench.py`、`xgboost_train.py` | 负载程序，供基准脚本调用 |
| [pebs/](pebs/) | PEBS 开关、频率矩阵与正式结果汇总 |
| [baseline/](baseline/) | native local/cgroup-linux sweep、PEBS 前基线、续跑与恢复检查 |
| [tests/](tests/) | 无需 RDMA 服务器的源码回归测试 |
| [results/](results/README.md) | 历次测量、构建日志、图表和归档 |

## PEBS

当前设计、实测结论和操作步骤见 [PEBS 设计](../../docs/pebs/design.md)

- Memcached 采样开销：`pebs/run_memcached_sampling_cost.py`，固定并发，比较同页大小的采样开关。
- 多负载矩阵：`pebs/run_workload_matrix.py`，默认 1323 个样本（去掉 XGBoost 为 1134），PEBS 按工作负载 pid 启用。`--pages` 表示每轮**最大分配页大小**，保留全部较小 THP 档位。
- 页面选择微基准：`pebs/run_page_selection_microbench.py`。

旧版 Memcached 矩阵 `pebs_perf_matrix.py` 保留用于复现 2026-10-06 的条件；当前实验优先使用上面的入口。

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

运行前应准备好 Hermit、RDMA 和 swap。脚本保留 `remote_order_mask`、`pebs_force_order`、学习阈值、自适应开关以及回收/批处理参数，只记录其值；启用强制 order 或后端缺少上限以内的档位会在启动检查中报错。例如完整 4KiB–2MiB 矩阵要求 `effective_order_mask` 包含 `0x3fd`，旧实验恢复的 `0x1` 不满足条件。脚本不会替换这个掩码。退出时恢复本次修改的 THP 设置及采样模式/周期。

匿名负载的逻辑访问区域固定为 2MiB（单独运行时可设 `ACCESS_REGION_KB`），`chunk64k` 始终访问每个区域的 64KiB；访问量不再随页上限变化。16GiB 工作集的覆盖量固定为 512MiB。历史字段 `pages_per_folio` 表示逻辑区域的页数，不代表实际分配 folio。

新结果标记 `page_policy=max-allocation-v1`，包含：

- `summary.csv`：每轮 `max_page_kb`、应用吞吐、计时及有效性。匿名吞吐按访问页面覆盖量计算，每页实际读取一个字节；Redis 为 GET/s、YCSB 为 ops/s、XGBoost 为 rounds/s。
- `granularity.csv`：按准备阶段/应用阶段、order 记录 folio 请求数、实际 RDMA WR 数/字节数/读字节占比、PEBS 决策数和内核提供的实际换入计数。旧内核缺少 `actual_order_N` 时留空；off 的 PEBS 计数不表示全部换入流量。
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
- `test_pebs_policy.py`：执行生产窗口、fault-PC 足迹表和代价模型，验证时间边界、跨区域学习、隔离、淘汰和失效。
- `test_pebs.py`：PEBS 作用域、mm 生命周期、Memcached 开销、THP、矩阵驱动、page-selection 微基准与绘图流程。

QEMU 构建及回归仍位于 [tools/qemu-dram](../qemu-dram/)，其临时输出位于 `_work/`。

## 输出约定

通用基准默认写入 `tools/rdma/results/`，可用 `RESULT_ROOT` / `RESULT_DIR` 覆盖；PEBS 矩阵与旧基线驱动要求显式输出目录。数据集使用 `tools/rdma/data/`，通过 `XGB_DATA_FILE` 指定解压后的输入文件。大型输入、原始结果和预览文件由 Git 忽略，入口索引保留可跟踪状态。
