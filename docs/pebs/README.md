# Hermit PEBS 页面策略

本目录保存 Hermit 在 swap-out/swap-in 路径上使用 Intel PEBS 采样选择传输与读回粒度的设计、实测和运行手册。

| 文档 | 内容 |
|---|---|
| [background.md](background.md) | Intel PEBS 原理、采集字段与 `perf mem` 用法 |
| [design.md](design.md) | 当前内核设计、数据路径、决策模型、控制面与限制 |
| [evaluation.md](evaluation.md) | 采样开销、Memcached 与多负载实测、对照口径与结论 |
| [runbook.md](runbook.md) | 复现实验、PEBS 启用方式、质量检查与结果目录 |

## 当前状态（2026-10-18）

- PEBS 历史归属 **mm（地址空间）**，不再依赖 memory cgroup；`CONFIG_HERMIT` 不要求 `CONFIG_MEMCG`。
- `memory.hermit_pebs` 已移除。启用方式为 `hermit_pebs_start(pid, mode)`（syscall 473）或 debugfs `pebs_all`。
- cgroup 仍可用于实验的 `memory.max/high`、`memory.swap.max` 和进程隔离，但不是 PEBS/策略的必需条件。
- QEMU DRAM 回归通过（store/load、checksum、`pswpin/pswpout`、本地 swap 写扇区不变）。

## 一句话结论

PEBS 采样本身开销很低；策略的收益取决于**访问密度和时间局部性**。它对“同一 2 MiB region 被反复、稀疏访问”的负载有效；对一次性流式负载，决策时没有历史样本，会退回静态页档。报告中的大幅提升必须结合对照基线阅读，不能外推为普遍加速。

历史详细报告与原始结论保存在 [PEBS 历史报告](../archive/pebs/)；原始测量数据在 [PEBS 原始数据](../../tools/rdma/results/pebs/)。
