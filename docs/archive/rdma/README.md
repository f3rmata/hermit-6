# RDMA 历史实验

本目录保存早期页大小、Redis 和 PEBS 前 baseline 的详细报告。当前部署与复现入口见：

- [dnet-58/dnet-61 部署与测试手册](../../rdma/dnet58-dnet61-memcached-rdma.md)
- [Redis 大 value 测试](../../rdma/redis-hermit-6.18.md)
- [XGBoost 测试](../../rdma/xgboost-hermit-6.18.md)
- [PEBS 评估](../../pebs/evaluation.md)

| 文件 | 内容 |
|---|---|
| `dnet61-memcached-page-sweep-20260808.md` | 2026-08 的 Memcached/Redis 页大小 sweep |
| `redis-chunk64k-20260831.md` | Redis 2 MiB value + 64 KiB chunk 早期分析 |
| `prepebs-baseline-20260924.md` | 添加 PEBS 前的多负载 baseline |

这些结果保留了当时的机器、内核和 cgroup 配置；当前内核与 PEBS 实现已经变化，跨轮比较前需要重新核对基线。
