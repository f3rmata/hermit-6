# 测试结果

本目录统一保存 RDMA 实测结果、构建证据与离线图表。输入数据集见 [data/](../data/README.md)，程序入口见 [测试工具索引](../README.md)。

| 目录 | 内容 |
|---|---|
| `pebs/20261007-233523-workloads/` | 1323 个多负载矩阵样本；`analysis/` 保存可复现统计及 PNG/PDF 图表 |
| `pebs/20261007-223941-workloads/` | 独立的 3 样本前置检查，不混入完整矩阵 |
| `pebs/20261006/` | PEBS 采样修复、预检和 91 条正式测量 |
| `pebs/20261011-185459-pressure-fixed-load/` | Memcached pressure 固定并发对照 |
| `pebs/20261011-214215-memcached-natural-pages/` | 自然页分配校准与 resident |
| `pebs/20261012-131002-natural-pages-pressure-c32/` | 自然页分配 pressure |
| `pebs/20261012-150657-thp-all/` | 全 THP/mTHP resident |
| `pebs/20261012-170017-thp-all-pressure/` | 全 THP/mTHP pressure |
| `baselines/` | 2026-09-24 添加 PEBS 前的多负载基线及续跑 |
| `builds/` | 内核构建、安装、验收日志及当时的产物校验和 |
| `dnet-61/` 及已有 CSV | 早期 Memcached、匿名内存、Redis、XGBoost 等实测结果 |

[前 PEBS 基线报告](../../../docs/archive/rdma/prepebs-baseline-20260924.md) · [历史页大小实验](../../../docs/archive/rdma/dnet61-memcached-page-sweep-20260808.md)

[PEBS 评估](../../../docs/pebs/evaluation.md) · [实验手册](../../../docs/pebs/runbook.md) · [多负载审计](pebs/20261007-233523-workloads/analysis/audit.json)

## PEBS 2026-10-06

修复版 `6.18.38-hermit-pebs #9` 在 dnet-61 的测试结果，按日期保存在 `pebs/20261006/`。

- [PEBS 评估](../../../docs/pebs/evaluation.md)
- [三阶段统计](pebs/20261006/pebs-campaign-20261006/SUMMARY.md)：pressure 50、resident 21、saturation 20，共 91 条有效正式测量。
- [性能总览图](pebs/20261006/pebs-campaign-20261006/overview.png) · [采样频率图](pebs/20261006/pebs-campaign-20261006/sampling-cost.png)
- [完整归档](pebs/20261006/pebs-performance-20261006.tar.gz) · [归档 SHA256](pebs/20261006/pebs-performance-20261006.tar.gz.sha256)

| 目录 | 内容 |
|---|---|
| `pebs/20261006/pebs-campaign-20261006/` | 正式测试原始数据、CSV/JSON 统计、PNG/PDF 图表、执行脚本与恢复验收记录 |
| `pebs/20261006/pebs-fixed-preflight-20261006/` | 修复后的短时预检，独立于正式统计 |
| `pebs/20261006/sampling-fix/` | 采样修复补丁、构建与 QEMU 验证日志 |
| `pebs/20261006/diagnostic/` | 硬件采样诊断源码、构建产物与结果 |
| `pebs/20261006/fix-candidate/` | 修复前的源码快照 |

2026-10-07 将仓库根目录的测试 `data/` 与 `results/` 合并到本目录。原始测量、执行快照和其中记录的远端路径保持原样；文档引用、复现命令及压缩归档使用新的 `tools/rdma/results/` 路径。远端运行目录仍为 `/home/xwz/hermit-baselines/pebs-campaign-20261006`。

历史原始日志、执行快照与构建产物校验记录保留原文中的服务器路径。`SHA256SUMS` 若引用服务器上未复制的内核镜像，只是构建证据，不代表镜像包含在本地目录中。归档文件的 SHA256 随目录结构重新打包更新。
