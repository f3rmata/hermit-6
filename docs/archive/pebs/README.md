# PEBS 历史报告

本目录保存 2026-09/10 的详细报告、旧版设计说明和当时的运行手册，用于追溯原始条件与结论。**当前设计、评估和操作步骤以 [../../pebs/](../../pebs/) 为准。**

| 文件 | 说明 |
|---|---|
| `pebs-order-policy.md` | 旧版策略设计（memcg 作用域），已被 `pebs/design.md` 取代 |
| `pebs-implementation-summary.md` | 旧版内核修改与 QEMU 总结，保留实现细节 |
| `pebs-conclusion.md` | 旧版结论汇总 |
| `pebs-performance-20261006.md` | Memcached 2026-10-06 完整评估 |
| `pebs-performance-summary-20261006.md` | 同一批数据的简明重析 |
| `pebs-workloads-summary-20261007.md` | 多负载矩阵 2026-10-07 验收与结论 |
| `pebs-workloads-20261008.md` | 多负载矩阵 2026-10-08 完整评估 |
| `memcached-sampling-cost.md` | 旧版 Memcached 固定并发采样开销手册 |
| `pebs-workload-matrix.md` | 旧版多负载矩阵运行说明 |
| `new-policy.org` | 页面选择/前瞻策略的设计草稿 |

这些报告中的“memcg 归属”“memory.hermit_pebs”等描述对应旧实现；当前实现已改为 mm 作用域，并移除了 `memory.hermit_pebs`。历史数字仍对应其原始内核、基线和工作负载，不能直接与当前 kernel 的 off 组混合。

原始测量保存在 [../../../tools/rdma/results/pebs/](../../../tools/rdma/results/pebs/)。
