# Hermit PEBS 页面大小策略（pebs-order-policy）

> 状态：2026-09-24 修复后四组 QEMU DRAM 回归通过；dnet61 实机采样与端到端评估待做（M4/M5）。详见 `fix-validation-20260924.md`。

## 1. 目标

用 Intel PEBS 硬件采样在线测量每个 memcg 内 2 MiB 虚拟区域（region）的
访问密度与空间局部性，用代价模型在 Hermit swap 路径上自动选择每次 RDMA
传输的 folio 粒度（order 0 / 2–9），替代全局静态的 `remote_order_mask`。

## 2. 决策模型

一次 RDMA WR 的固定开销约 7µs（4 KiB 协议带宽仅 ~0.6 GiB/s）；大 folio
摊薄固定开销（2 MiB 达 10–11 GiB/s），但若 folio 内只有比例 `f` 的子页被
访问，读放大约为 `1/f`（见 `docs/motivation.md`）。对候选 order `o`
（页数 2^o，o ∈ {0, 2..9}，排除非法的 order 1）：

```
c(o) = C_fix / 2^o + (1 / max(f(o), min_f)) * 4096 / BW     [ns per 4KiB page]
```

选 `o* = argmin c(o)`（平手取大）；`f(o)` 为该 folio 对齐子区间内被采样
触及的 4 KiB 子页比例。迟滞：新 order 的代价改善不足
`(1000 - hysteresis)`‰ 时保持当前 order，防止振荡。

| debugfs 参数 | 默认 | 含义 |
|---|---|---|
| `pebs_fixed_cost_ns` | 7000 | 单次 WR 固定开销（ns） |
| `pebs_bw_mibps` | 12288 | 链路带宽（MiB/s） |
| `pebs_min_f` | 50 | f 下限（‰），防 1/ε 爆炸 |
| `pebs_hysteresis` | 900 | 迟滞（‰） |
| `pebs_cooling_period` | 100000 | 冷却周期（样本数/时钟 tick） |
| `pebs_region_max` | 32768 | 每 memcg region 表上限（LRU 淘汰） |
| `pebs_mode` | 0 | 0=static（现行为）/ 1=policy |
| `pebs_enabled` | 0 | 主开关 |
| `pebs_force_order` | 0 | 0=关；2..9 强制传输 order（功能测试用） |

## 3. 内核改动（hermit-6.18 分支）

- `mm/hermit_pebs.c`（新）：`hermit_pebsd` 内核线程，经
  `perf_event_create_kernel_counter` 打开 `MEM_LOAD_RETIRED.L3_MISS`
  （0x1d3，PEBS-LL）与 `MEM_INST_RETIRED.ALL_STORES`（0x82d0），
  `PERF_SAMPLE_IP|TID|ADDR`；质数采样周期表 + 2s 窗口自适应升降
  （移植自 MEMTIS `htmm_sampler.c`）。样本按 TID→task→memcg 归属，
  只记录 `memory.hermit_pebs=enabled` 的 memcg。
- Region 表（每 memcg）：key=`(mm, vaddr>>21)`，512-bit 触及位图 +
  冷却时钟；两拍无访问即清空位图（保守衰减到未知），超上限 LRU 淘汰。
  无 PEBS 环境下采样启动可返回 PMU 不支持错误；QEMU 使用合成位图和
  force_order 验证功能，不宣称硬件采样已覆盖。
- `kernel/events/core.c`：`CONFIG_HERMIT` 下新增
  `hermit_perf_event_init`（rb_alloc/attach，因相关函数为文件私有）、
  `hermit_perf_event_set_period`、`hermit_perf_event_release`
  （detach 自身释放 attachment 引用，不能重复 put）。
- `mm/vmscan.c` / `mm/page_io.c`：在 `try_to_unmap()` 前调用
  `hermit_swapout_order()`，持 anon_vma 读锁查询映射和 region；通过
  `pageout → writeout → swap_writeout_order` 传递结果，不保存裸 mm 指针。
  I/O 层再次约束 order 到 backend 有效 mask。缺少映射/样本时用静态配置。
- RDMA 按 transfer order 分段发送整个 folio，失败后排空并用 4 KiB 重试。
  `wr_stats` 按真实请求大小统计数量和字节，包含重试。
- `mm/memory.c`：`alloc_swap_folio` 对远端 entry 按策略 order 屏蔽
  `candidate_orders`，把单次 fault 的 swap-in 聚合粒度（读放大）限制在
  模型范围内；extent order 仍是 swap-out 时的 folio order（单一 extent，
  无子 extent 拆分）。
- 接口：syscall **473 `hermit_pebs_start(pid, mode)`** /
  **474 `hermit_pebs_end(pid)`**；memcg v2 文件 `memory.hermit_pebs`；
  启动/停止要求 CAP_SYS_ADMIN；debugfs `pebs_order_stats`、
  `pebs_test_eval`（复用生产决策函数）、`pebs_ring_test`（跨页/回绕及 perf 生命周期）。

## 4. 验证矩阵

| 层次 | 用例 | 状态 |
|---|---|---|
| 单元 | `pebs_test_eval`：全扫描→9、chunk64k→4、单页→2（QEMU guest 内断言） | 2026-09-24 通过 |
| 功能 | QEMU DRAM 回归：static 基线 / `pebs_force_order=9`+2MiB / `=4`+64KiB，checksum、backend error=0、ram0 写扇区不变 | 2026-09-24 通过 |
| 对照 | dnet61：`perf mem record` 真值 vs 内核 region 位图（Jaccard ≥90%） | M4 |
| 端到端 | dnet61：memcached/redis sweep policy vs static（QPS ≥95% 最优、读放大 ≤1.2×） | M5 |

## 5. 已知限制

- QEMU/虚拟化内无 PEBS：本地只验证策略与传输正确性（合成位图 + force_order）。
- 常规回收在 unmap 前取得策略；进入回收扫描时已经未映射且无策略快照的
  folio 仍回退 static。
- swap-out 仍是单一 extent（folio order），策略只控制 WR 粒度与 swap-in
  聚合粒度；folio 内异构子 extent 拆分留作扩展。
- 6.18 无 per-memcg mTHP 分配控制；分配侧暂时依赖全局 THP sysfs
  （M6 可选回移 `memory.thp` 系列）。
- 事件码 0x1d3/0x82d0 需在 dnet61 实机 `perf list` 确认（SKL/ICL/SPR 族
  基本一致）。
