# 项目文档

从 [RDMA 测试工具](../tools/rdma/README.md) 运行基准，从 [结果索引](../tools/rdma/results/README.md) 查找原始数据。设计、部署说明和历次实测分别归类；历史记录保留原有测试条件与结论。

- [Linux 6.18 迁移](migration-6.18.md)
- [研究动机与传输代价](motivation.md)

## PEBS 设计与实测

- [Intel PEBS：原理、采集数据与用法](pebs/pebs-analysis.md)
- [PEBS 页面大小策略：内核修改与 QEMU 测试总结](pebs/pebs-implementation-summary.md)
- [Hermit PEBS 页面大小策略（pebs-order-policy）](pebs/pebs-order-policy.md)
- [PEBS 实机性能评估 20261006](pebs/pebs-performance-20261006.md)
- [PEBS 性能评估 · 简明重析（2026-10-06）](pebs/pebs-performance-summary-20261006.md)
- [PEBS 采样记录修复与验证](pebs/pebs-sampling-fix-20261006.md)

## RDMA 部署与基准报告

- [dnet-58/dnet-61 Hermit RDMA 与 memcached 测试手册](rdma/dnet58-dnet61-memcached-rdma.md)
- [dnet-61 hermit 测试](rdma/dnet61-memcached-page-sweep-20260808.md)
- [PEBS 前 Hermit baseline（2026-09-24）](rdma/prepebs-baseline-20260924.md)
- [Redis 2 MiB value + 64 KiB chunk 最新测试分析（20260831-001724）](rdma/redis-chunk64k-20260831.md)
- [Redis 大 value 大页 RDMA 交换测试](rdma/redis-hermit-6.18.md)
- [XGBoost 大页 RDMA 交换测试](rdma/xgboost-hermit-6.18.md)

## 内核构建与安装记录

- [dnet-61 PEBS 内核构建（2026-09-25）](builds/kernel-build-dnet61-20260925.md)
- [dnet-61 RDMA 修复与 PEBS 内核安装（2026-10-06）](builds/rdma-pebs-install-20261006.md)

## 修复与回归验证

- [Hermit 修复与验证（2026-09-24）](validation/fix-validation-20260924.md)

## 论文与相关工具

- [Blowfish 论文分析](research/blowfish-paper-analysis.md)
- [MEMTIS 算法分析：基于 PEBS 的动态页面分类与页面大小判定](research/memtis-algorithm-analysis.md)
- [Numamma](research/numamma.md)

## 历史迁移资料

[历史资料使用说明](archive/README.md)

- [迁移 Hermit 到 Linux 6.6](archive/patch-6.org)
- [Hermit Linux 6.15 第一阶段适配说明](archive/plan/stage1-adaptation-6.15.md)
- [Hermit Linux 6.15 第二阶段适配说明](archive/plan/stage2-adaptation-6.15.md)
- [Hermit Linux 6.6 第三阶段适配说明](archive/plan/stage3-adaptation-6.6.md)
- [Hermit Linux 6.6 第四阶段适配说明：DRAM backend 运行验证](archive/plan/stage4-adaptation-6.6-dram.md)
- [Hermit Linux 6.6 第五阶段适配说明：5.14 profiling 点迁移](archive/plan/stage5-adaptation-6.6-profiling.md)
- [Hermit Linux 6.6 第六阶段适配说明：vaddr/vpage directed reclaim](archive/plan/stage6-adaptation-6.6-vaddr-vpage.md)
- [Hermit Linux 6.6 第七阶段适配说明：RDMA backend 迁移](archive/plan/stage7-adaptation-6.6-rdma.md)

`assets/` 保存报告配图；`.previews/` 保存生成的 HTML 预览并由 Git 忽略。`archive/plan/` 为旧版本适配方案，当前实现以迁移说明、源码和对应日期的验证记录为准。
