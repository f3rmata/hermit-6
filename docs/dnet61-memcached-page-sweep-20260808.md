# dnet-61 memcached 测试

- Linux 6.18.38-hermit 运行 Memcached 和 Mutilate，测试 `local`、`cgroup-local` 与 `cgroup-Hermit` 三种模式；
- 数据集为 3200 万条记录
- Memcached 使用 0–7 核，Mutilate 使用 8–15 核，每个负载重复3次并取中位数。
- mTHP页大小 4 KiB–2 MiB，本地和远端内存比例设为70%
- 暂时只记录swap-in的测试结果，swap-out仍为4KiB大小

![dnet-61 最新完整页大小吞吐对比](../tools/rdma/results/dnet-61/20260808-140944-hermit-6.18-native-hermit/latest-mode-throughput-comparison.png)

![dnet-61 在 500 KQPS 下的 read p99 对比](../tools/rdma/results/dnet-61/20260808-140944-hermit-6.18-native-hermit/latest-read-p99-comparison-500k.png)

在相同的 500 KQPS offered load 下，local 的 read p99 约为 59 µs，
cgroup-Hermit 约为 1.7–2.1 ms，而严重换页抖动的 cgroup-local 达到约 0.22–0.65 s。

## 历史结果

下表只统计实测数据；“峰值”是每种页大小在所有 offered load 中达到的最大achieved QPS。

| 测试时间   | 模式与覆盖范围               | 平均峰值（KQPS） | 页大小峰值范围（KQPS） | 说明                                                          |
| ---------- | ---------------------------- | ---------------: | ---------------------: | ------------------------------------------------------------- |
| 2026-07-27 | local，4 KiB–2 MiB           |            928.2 |            925.4–931.9 | 第一轮完整 native 基线                                        |
| 2026-07-27 | cgroup-local，4 KiB–2 MiB    |              5.5 |                4.5–6.5 | 70% 内存比例下发生严重换页抖动                                |
| 2026-07-30 | cgroup-Hermit，4 KiB–2 MiB   |            672.3 |            659.1–687.9 | 使用普通 swapfile，高阶 folio 在 swap-out 前回退为 4 KiB      |
| 2026-08-03 | cgroup-Hermit，4 KiB–2 MiB   |            713.6 |            705.3–728.1 | 2 MiB 高负载为 `mixed`；只统计 `stable` 时其峰值为 492.4 KQPS |
| 2026-08-04 | cgroup-Hermit，仅 2 MiB 复测 |            713.3 |                  713.3 | 最高点仍为 `mixed`，稳定峰值为 493.7 KQPS                     |
| 2026-08-08 | local，4 KiB–2 MiB           |            943.0 |            940.4–945.9 | 最新完整 local 基线                                           |
| 2026-08-08 | cgroup-local，4 KiB–2 MiB    |              4.9 |                3.6–6.5 | 仍处于严重 thrashing 区间                                     |
| 2026-08-08 | cgroup-Hermit，4 KiB–2 MiB   |            710.4 |            701.3–716.5 | `/dev/sdb6` 块设备；大页 RDMA store 生效且无 fallback         |
| 2026-08-09 | local，4 KiB–2 MiB           |            941.8 |            938.4–944.4 | 与 8 月 8 日相比变化约在 ±0.6% 内                             |
| 2026-08-09 | cgroup-local，4 KiB–2 MiB    |              5.8 |                4.5–7.1 | 低吞吐抖动状态重复出现                                        |

### 2026-07-30：早期 Hermit 完整 sweep

![2026-07-30 Hermit 实测吞吐与 p99](../tools/rdma/results/dnet-61/20260730-184935-hermit-6.18-page-sweep/measured-throughput-by-page.png)

该轮 Hermit 峰值约为 659–688 KQPS，但 debugfs 中只有 order 0 的传输计数；
原因是普通 swapfile 无法分配高阶 swap entry，大 folio 在进入 RDMA backend 前已拆分为 4 KiB。

### 2026-08-03：Hermit 完整 sweep

![2026-08-03 Hermit stable 与 mixed 峰值](../tools/rdma/results/dnet-61/20260803-231615-hermit-6.18-page-sweep/cgroup-hermit-throughput-by-page-measured.png)

4 KiB–1 MiB 的负载点均为 `stable`；2 MiB 在 750 KQPS 以上达到约 705 KQPS，
但稳定等待状态为 `mixed`。

### 2026-08-04：2 MiB 单独复测

![2026-08-04 2 MiB 复测叠加](../tools/rdma/results/dnet-61/20260804-195156-hermit-6.18-page-sweep/cgroup-hermit-throughput-by-page-measured-with-rerun.png)

2 MiB 复测峰值由约 705.3 KQPS 提高到 713.3 KQPS，峰值 p99 从约 4.58 ms
降低到 4.51 ms；高负载仍为 `mixed`

原始汇总数据：

- [2026-07-27 Native](../tools/rdma/results/dnet-61/20260727-235121-hermit-6.18-native-hermit/native/page-sweep-summary.csv)
- [2026-07-30 Hermit](../tools/rdma/results/dnet-61/20260730-184935-hermit-6.18-page-sweep/page-sweep-summary.csv)
- [2026-08-03 Hermit](../tools/rdma/results/dnet-61/20260803-231615-hermit-6.18-page-sweep/page-sweep-summary.csv)
- [2026-08-04 2 MiB Hermit 复测](../tools/rdma/results/dnet-61/20260804-195156-hermit-6.18-page-sweep/page-sweep-summary.csv)
- [Native page-sweep-summary.csv](../tools/rdma/results/dnet-61/20260808-140944-hermit-6.18-native-hermit/native/page-sweep-summary.csv)
- [Hermit page-sweep-summary.csv](../tools/rdma/results/dnet-61/20260808-140944-hermit-6.18-native-hermit/hermit/page-sweep-summary.csv)
- [2026-08-09 Native](../tools/rdma/results/dnet-61/20260809-114645-hermit-6.18-native-hermit/native/page-sweep-summary.csv)
