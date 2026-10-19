# 项目文档

从 [RDMA 测试工具](../tools/rdma/README.md) 运行基准，从 [结果索引](../tools/rdma/results/README.md) 查找原始数据。

## 设计

- [Linux 6.18 迁移与数据路径](migration-6.18.md)
- [研究动机与传输代价](motivation.md)

## PEBS

- [PEBS 文档入口](pebs/README.md)
- [Intel PEBS 原理与采集](pebs/background.md)
- [当前设计与实现](pebs/design.md)
- [实测评估与适用边界](pebs/evaluation.md)
- [实验手册](pebs/runbook.md)

## RDMA 部署与工作负载

- [dnet-58/dnet-61 Hermit RDMA 与 memcached 部署](rdma/dnet58-dnet61-memcached-rdma.md)
- [Redis 大 value 大页测试](rdma/redis-hermit-6.18.md)
- [XGBoost 大页测试](rdma/xgboost-hermit-6.18.md)

## 背景研究

- [MEMTIS：基于 PEBS 的页面分类与大小判定](research/memtis-algorithm-analysis.md)

## 历史资料

- [历史适配与设计记录](archive/README.md)
- [PEBS 详细报告](archive/pebs/README.md)
- [RDMA 历史实验](archive/rdma/README.md)
