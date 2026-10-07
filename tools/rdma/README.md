# RDMA 测试工具

本目录集中保存 RDMA 基准、PEBS 实验、离线分析、数据集与结果。以下命令均从仓库根目录执行。

| 位置 | 用途 |
|---|---|
| 根目录 `run_*.sh`、`memcached*.sh` | 通用匿名内存、Memcached、Redis、XGBoost、YCSB 基准入口，保留已有调用路径 |
| `common.sh`、`create_cgroup.sh` | 共享配置、结果根目录与 cgroup 支持 |
| `anon_seq_workset.c`、`redis_bench.py`、`xgboost_train.py` | 负载程序，供基准脚本调用 |
| [pebs/](pebs/) | PEBS 开关、频率矩阵与正式结果汇总 |
| [baseline/](baseline/) | 添加 PEBS 前的基线运行、续跑、恢复检查及分析 |
| [plots/](plots/) | 其他基准的离线绘图与分析，通常从参数读取 CSV |
| [tests/](tests/) | 无需 RDMA 服务器的源码回归测试 |
| [notebooks/](notebooks/) | 历史 Memcached 交互分析 |
| [data/](data/README.md) | HIGGS 等输入数据集与下载日志 |
| [results/](results/README.md) | 历次测量、构建日志、图表和归档 |

## PEBS

[完整方法与结果](../../docs/pebs/pebs-performance-20261006.md) · [简明重析](../../docs/pebs/pebs-performance-summary-20261006.md)

```sh
python3 tools/rdma/pebs/pebs_perf_matrix.py --help
for phase in pressure resident saturation; do
  python3 tools/rdma/pebs/plot_pebs_perf.py tools/rdma/results/pebs/20261006/pebs-campaign-20261006/$phase
done
python3 tools/rdma/pebs/summarize_pebs_campaign.py tools/rdma/results/pebs/20261006/pebs-campaign-20261006
```

矩阵程序用于已准备好内核、RDMA 后端和 swap 的专用测试机，需要 root 执行，并以 `--user` 指定的普通用户运行负载；必须指定新的 `--output` 目录。离线汇总与绘图不需要 sudo。`plot_pebs_clean.py` 是 2026-10-06 的固定数值展示脚本，可用 `--output-dir` 指定输出；新测量应使用读取原始数据的正式汇总工具。

## 其他基准和基线

- [Memcached 与匿名内存部署及测试](../../docs/rdma/dnet58-dnet61-memcached-rdma.md)
- [Redis](../../docs/rdma/redis-hermit-6.18.md) · [XGBoost 与 HIGGS](../../docs/rdma/xgboost-hermit-6.18.md)
- [添加 PEBS 前的基线记录](../../docs/rdma/prepebs-baseline-20260924.md)

`baseline/` 的运行及续跑脚本对应特定旧内核、原始输出目录和源码快照，保留其版本检查；它们不是当前 PEBS 内核的通用测试入口。已归档的 `driver.py`、`source/` 和内核补丁保留实验时内容，不随当前工具目录改写。

## 本地回归

```sh
python3 tools/rdma/tests/test_rdma_submission.py
python3 tools/rdma/tests/test_remote_read.py
```

两项测试提取生产源码并编译宿主机 C harness，覆盖提交边界、失败排空与远端读取错误路径。QEMU 构建及回归仍位于 [tools/qemu-dram](../qemu-dram/)，其临时输出位于 `_work/`。

## 输出约定

通用基准默认写入 `tools/rdma/results/`，可用 `RESULT_ROOT` / `RESULT_DIR` 覆盖；PEBS 矩阵与旧基线驱动要求显式输出目录。数据集使用 `tools/rdma/data/`，通过 `XGB_DATA_FILE` 指定解压后的输入文件。大型输入、原始结果和预览文件由 Git 忽略，入口索引保留可跟踪状态。
