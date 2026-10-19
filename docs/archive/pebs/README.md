# PEBS 历史报告

本目录只保留两份详细原始报告；当前设计、评估和操作步骤以 [../../pebs/](../../pebs/) 为准。

| 文件 | 内容 |
|---|---|
| `pebs-performance-20261006.md` | Memcached 2026-10-06 完整评估 |
| `pebs-workloads-20261008.md` | 数组、Redis、YCSB、XGBoost 多负载矩阵完整评估 |

旧版设计说明、结论汇总、运行手册和重复的简明重析已经合并到当前文档并删除。历史报告中的 `memory.hermit_pebs`、memcg 归属等描述对应旧实现；数字对应其原始内核、基线和工作负载，不能直接与当前 kernel 的 off 组混合。

原始测量保存在 [../../../tools/rdma/results/pebs/](../../../tools/rdma/results/pebs/)。
