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
- 多负载矩阵：`pebs/run_workload_matrix.py`，1323 个样本，PEBS 按工作负载 pid 启用。
- 页面选择微基准：`pebs/run_page_selection_microbench.py`。
- 离线汇总：`pebs/summarize_workload_matrix.py`。

旧版 Memcached 矩阵 `pebs_perf_matrix.py` 保留用于复现 2026-10-06 的条件；当前实验优先使用上面的入口。

```sh
python3 tools/rdma/pebs/pebs_perf_matrix.py --help
for phase in pressure resident saturation; do
  python3 tools/rdma/pebs/plot_pebs_perf.py tools/rdma/results/pebs/20261006/pebs-campaign-20261006/$phase
done
python3 tools/rdma/pebs/summarize_pebs_campaign.py tools/rdma/results/pebs/20261006/pebs-campaign-20261006
```

矩阵程序用于已准备好内核、RDMA 后端和 swap 的专用测试机，需要 root 执行，并以 `--user` 指定的普通用户运行负载；必须指定新的 `--output` 目录。离线汇总与绘图不需要 sudo。`plot_pebs_perf.py` 读取单次 campaign 的 manifest/summary；`plot_pebs_workloads.py` 读取多负载矩阵的 per-case CSV；`plot_page_selection_microbench.py` 读取 page-selection 运行目录。

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
