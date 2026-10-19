# 历史资料

本目录保存已被当前文档取代、但仍用于追溯设计和原始证据的材料。当前构建与接口见 [Linux 6.18 迁移说明](../migration-6.18.md)，当前 PEBS 设计见 [PEBS 设计](../pebs/design.md)。

| 目录/文件 | 内容 |
|---|---|
| `plan/` | Linux 6.6、6.15 阶段的适配方案 |
| `patch-6.org` | 早期 6.6 迁移补丁说明 |
| `fix-validation-20260924.md` | 2026-09-24 修复与 QEMU 验证记录 |
| `pebs/` | PEBS 旧版设计、详细评估与运行手册 |
| `rdma/` | 早期页大小 sweep、Redis 和 PEBS 前 baseline |

旧方案中引用的机器绝对路径、旧内核 checkout 和行号保留原文。例如 stage2 中的 `/home/fermata/Development/repos/fastswap/hermit/linux-6.15/` 当前并不存在；这些链接不是当前仓库的源码入口，不应替换为版本不同的文件来冒充原始证据。
