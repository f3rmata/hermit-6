# Intel PEBS：原理、采集数据与用法

> 本文整理 Intel PEBS（Precise Event-Based Sampling，精确事件采样）的工作
> 原理、它具体采集哪些数据、以及在 Linux 上如何用 `perf` 采集与解读这些
> 数据。最后结合本项目（Hermit / RDMA swap / 内存分层）讨论 PEBS 如何用于
> 页面热度测量，从而指导 swap-in / swap-out 的传输粒度选择。

## 1. 为什么需要 PEBS

传统基于 PMU（Performance Monitoring Unit）的采样是「中断式采样」：配置一个
性能计数器去数某个事件（例如 cache miss），计数器溢出时硬件产生一个中断
（NMI），分析工具在中断处理函数里记录「当前」的指令指针（IP）和其他状态。

问题在于 **skid（滑移）**：从计数器溢出到中断真正被处理，中间隔着乱序执行、
中断延迟、以及处理器停下流水线并打标签的时间。中断里读到的 IP 往往不是真正
触发事件的那条指令，而是之后若干条指令。在高性能乱序处理器上，这个偏移可以
达到**几百条指令**。这会让性能分析者把 `load3` 错当成 `load1` 的罪魁祸首。

PEBS 就是为了消除 skid 而设计的：**由硬件自己在事件发生的那一刻，把精确的
指令指针（连同大量架构状态）直接写进内存缓冲区**，而不是靠软件在中断里事后
猜测。

## 2. 原理

### 2.1 工作机制

PEBS 首次出现在 NetBurst 微架构，本质是把「采样」这件事从软件中断转移到
硬件自动完成：

```text
传统中断采样：
  counter overflow -> NMI 中断 -> 软件在 ISR 里记录 IP（有 skid，每次都要中断）

PEBS：
  counter overflow -> 武装 PEBS 机制（不中断）
                    -> 硬件等待下一次目标事件发生
                    -> 硬件把 PEBS record 写进专用的 PEBS buffer（Debug Store）
                    -> 硬件自动清溢出状态、重新装载计数器初值
                    -> 只有 buffer 写满时才产生一次中断，批量 flush 到内存
```

关键点：

1. **精确**：`PEBS record` 里的 `EventingIP` 字段由硬件记录，就是真正触发事件
   的那条指令的地址，skid 近似为零。
2. **低开销**：一次 counter overflow 不再对应一次中断，而是攒满一个 buffer 才
   中断一次，中断次数大幅减少，采样对程序的扰动也更小。
3. **非全量**：它不会追踪每一次 load/store（否则开销不可接受），而是**采样**
   ——例如每 100000 次访问只采一次。样本量足够大时就能得到准确的统计图景。

### 2.2 Precise Events 与精确等级

不是所有 PMU 事件都能被精确采样。只有硬件支持 PEBS 的事件子集（称为
**Precise Events**）才能消除 skid。以 Skylake 为例，可精确的事件族包括：

```
INST_RETIRED.*          OTHER_ASSISTS.*      BR_INST_RETIRED.*
BR_MISP_RETIRED.*       FRONTEND_RETIRED.*   HLE_RETIRED.*
RTM_RETIRED.*           MEM_INST_RETIRED.*   MEM_LOAD_RETIRED.*
MEM_LOAD_L3_HIT_RETIRED.*
```

（`.*` 表示该事件族内的所有子事件都能配置成 precise。）

Linux `perf` 里用事件后缀请求精确等级：

```bash
perf record -e cycles:pp -- ./a.out
```

- `:p` —— 请求精确采样（等级 1）；
- `:pp` —— 请求用 PEBS（等级 2），是最常用的「精确」等级；
- `:ppp` —— 更细的指令退役精确分布（如 `INST_RETIRED.PREC_DIST`，供 TMA
  自顶向下分析把瓶颈归到前端/后端）。

TMA（Top-Down Microarchitecture Analysis）方法论大量依赖 precise events 来
把低效执行定位到具体源码行。

### 2.3 PEBS 的演进

| 阶段 | 能力 |
| --- | --- |
| Basic PEBS（NetBurst 起） | 记录 EventingIP + 通用寄存器状态 |
| PEBS Load Latency（PEBS-LL） | 增加 Data Linear Address、Latency Value、Data Source，用于内存访问分析 |
| Skylake 起的扩展 PEBS | record 重构为 Basic / Memory / GPR / XMM / LBR 分组，可按需选择组以减小 record 体积 |
| Adaptive PEBS（Ice Lake / Sapphire Rapids 起） | 采样更灵活，可一次采样多个事件、捕获更多字段 |

## 3. PEBS 具体采集到哪些数据

PEBS record 的布局随微架构不同而变（见 Intel SDM Vol.3B Chapter 20）。
Skylake 及之后按组组织，默认只含 Basic 组：

### 3.1 Basic 组

| 字段 | 含义 |
| --- | --- |
| `EventingIP` / `IP` | 触发事件的精确指令指针（消除 skid 的核心） |
| `Data Linear Address`（DLA） | 被采样 load/store 访问的**线性地址**（Data Address Profiling） |
| `Latency Value` | 该内存访问的延迟（PEBS-LL，配合 `ldlat` 阈值） |
| `Data Source` | 数据从哪里来：命中哪级 cache / 本地 DRAM / 远端 / 是否被 snoop 等（见 §3.3） |
| `TSC` | 时间戳 |
| Applicable Counters | 相关的计数器状态 |

### 3.2 其他组

- **GPR 组**：`RFLAGS`、`RIP`、`RAX`…`R15` 全部通用寄存器，即采样瞬间的
  完整整数寄存器状态。
- **XMM 组**：XMM 寄存器（Skylake 起）。
- **LBR 组**：Last Branch Record 栈（Skylake 起）。

Linux `perf` **不导出原始 PEBS record**，而是按需抽取字段处理。想看原始
record 需要用 [`pebs-grabber`](https://github.com/andikleen/pmu-tools/tree/master/pebs-grabber)
（需要 root），或 `perf report -D` 看处理后的部分字段。

### 3.3 Data Source 编码（内存访问分析的关键）

Data Source 是一个 64 位编码，Linux 内核在 `arch/x86/events/intel/ds.c` 里
按微架构解码，`perf mem report` 把它翻译成几个可读列：

| 字段 | 含义 | 典型取值 |
| --- | --- | --- |
| `mem_lvl` | 数据命中的存储层级 | `LFB`（Line Fill Buffer，L1 miss 正在填充）、`L1`、`L2`、`L3`、`Local RAM`（本地 DRAM）、`Remote RAM (1 hop)` / `(2 hops)`（远端内存）、`PMEM`（持久内存） |
| `mem_snoop` | 一致性 snoop 结果 | `None`、`Miss`、`Hit`、`HitM`（命中且已修改）、`Clean` |
| `mem_lock` | 是否锁总线访问 | `Locked` / 未锁 |
| `mem_dtlb` | 数据 TLB 命中层级 | L1 dTLB hit、L2 STLB hit、TLB miss、Walker 等 |
| `mem_lvl_num` | 层级数值编码（多位，每位代表一级） | 供脚本化解析 |

其中 `Remote RAM` 对本项目特别有意义：它直接标记「这次 load 是从远端内存节点
取回的」，正是 RDMA swap / 内存分层关心的「远端访问」信号。

## 4. Linux `perf` 用法

### 4.1 检查是否支持

```bash
dmesg | grep -i pebs
# 期望看到类似：
# Performance Events: XSAVE Architectural LBR, PEBS fmt4+-baseline, ...
```

### 4.2 精确事件采样（定位热点指令）

```bash
# 采集某条指令/某行代码触发的精确事件
perf record -e cycles:pp -c 100000 -- ./a.out
perf report
```

### 4.3 `perf mem`：内存访问与延迟分析

`perf mem` 是 PEBS 内存分析的最常用入口，底层用 PEBS-LL 的 load-latency 事件：

```bash
# 采样 load 与 store，记录其 Data Linear Address / Latency / Data Source
perf mem record -- ./a.out
perf mem report --stdio

# 只采样长延迟 load（例如超过 30 个 cycle 的 load）
perf mem record -e mem_load_retired.l3_miss:pp -- ./a.out
perf mem report
```

`perf mem report` 输出里 `mem_lvl` / `mem_snoop` / `mem_dtlb` 列配合
`Data Symbol`（数据地址命中的变量/结构）可以回答：**哪些数据结构、在哪个
cache/内存层级被访问、延迟多少**。

### 4.4 `perf c2c`：真/假共享分析

`perf c2c` 依赖 PEBS（以及 AMD IBS / ARM SPE）的 DLA 能力，匹配不同线程对
同一 cache line 的 load/store 地址，判断是否存在争用（false sharing）：

```bash
perf c2c record -- ./a.out
perf c2c report
```

### 4.5 导出样本字段做后处理

```bash
# 打印每条样本的可读字段（含 addr、latency、data_src 等）
perf script -F comm,pid,tid,ip,addr,sym,latency,data_src,period
```

`data_src` 是 Data Source 的紧凑文本编码，可脚本化解析（例如区分 `Remote RAM`
样本的比例）。

## 5. 与 AMD IBS / ARM SPE 的对照

Intel PEBS 在 AMD 上的对应物是 **IBS（Instruction-Based Sampling）**，在 ARM
上是 **SPE（Statistical Profiling Extension）**：

| | Intel PEBS | AMD IBS | ARM SPE |
| --- | --- | --- | --- |
| 采样方式 | 计数器溢出后硬件写 record | 流水线内选择/标记一条指令，跟踪其执行 | 流水线内统计采样 |
| 结构 | 单一机制 | Fetch / Execute 两路 | 单一机制 |
| 采集内容 | IP + DLA + latency + data source + GPR | 取指/执行阶段的 cache/TLB 命中、地址、延迟 | 指令地址、访存虚拟/物理地址、数据来源、各级延迟 |
| 精确性 | 仅 Precise Events 子集 | 所有样本天然精确 | 所有样本天然精确 |

三者都能支撑内存访问分析，`perf c2c` / `perf mem` 对三者都有部分支持。本文
聚焦 Intel PEBS。

## 6. 对本项目（Hermit / RDMA swap）的启发

`docs/rdma/dnet61-memcached-page-sweep-20260808.md` 里留了一句设想：

> 可以参考 Memory tiering 的 PEBS 等性能监测设施，调节 swap-in/swap-out 的
> 页面大小等参数，在提高吞吐量和避免写放大之间做 tradeoff。

PEBS 恰好能提供这个 tradeoff 决策所需的**页面级热度信号**：

> 一个把「PEBS 页面热度测量 + 页面大小判定」落到内核的完整实例是 MEMTIS
> （SOSP'23），其采样、分类、skewness 拆分算法详见
> [memtis-algorithm-analysis.md](../research/memtis-algorithm-analysis.md)。

### 6.1 用 PEBS 做页面热度测量

PEBS 的 Data Linear Address（DLA）给出被采样 load 的**线性地址**。按页大小
（4 KiB / 64 KiB / 2 MiB）聚合样本：

1. 采样 `mem_load_retired.*`（配合 `ldlat` 阈值过滤长延迟 load），得到
   `(address, latency, data_source)` 样本流；
2. 把地址右移对齐到 page，统计每页的**访问次数**（热度）与**平均延迟**；
3. `data_src` 里的 `Local RAM` vs `Remote RAM` 直接区分「本地命中」与
   「远端（swap-in 后仍未本地化）」的访问；
4. 由此得到一张页面热度 + 远端访问占比的 heatmap。

### 6.2 与现有机制对比

| 机制 | 粒度 | 精度 | 开销 | 额外信息 |
| --- | --- | --- | --- | --- |
| PTE access bit 扫描 | 页 | 粗（bit 翻转） | 低（周期扫描） | 无延迟/来源信息 |
| DAMON | 页区域 | 粗 | 低 | 访问频率 |
| **PEBS DLA** | 采样 load 地址 | 高（地址精确） | 中（采样） | **延迟 + 数据来源（远端/本地）** |

PEBS 的独特价值在于：它不止给「热不热」，还给「这次访问到底等了多久、数据从
哪来」，这正好对应 RDMA swap 最关心的指标——**远端 swap-in 的延迟与频率**。

### 6.3 具体落地方向

- **指导非对称 swap-out / swap-in 策略**：对访问稀疏（热度低）且局部性差的页，
  用 4 KiB 小粒度换出，避免把无用数据拉回；对顺序密集访问（热度高、`f≈1`）
  的页，用 16 KiB–2 MiB 大粒度换入，摊薄单次 RDMA WR 的固定开销。
- **验证倒 U 拐点**：当前 page-sweep 靠离线统计 `read_amp` / `large_load_%`，
  PEBS 可以在线给出每个 folio 内部「被访问子页比例 `f`」的分布，把
  `1/f` 读放大从「事后估算」变成「采样实测」。
- **热页预提升（promotion）**：用 `Remote RAM` + 高延迟样本识别「频繁远端命中」
  的页，优先在本地保留/预取，减少反复 swap-in。

### 6.4 落地时的注意点

- PEBS 给出的是**线性地址**，内核层做页粒度分析需要虚拟→物理转换，用户态做
  则需要先拿到进程地址空间映射（`/proc/<pid>/maps` + pagemap）。
- 采样率很低（例如 1/100000），单页样本可能稀疏，需要足够长采集窗口或对
  区域（folio）聚合，不能期望逐页精确计数。
- 多进程/多线程环境下要按 tid 归属样本，且注意 KPTI / 内核态样本归属。
- 需要 root 或调整 `perf_event_paranoid`，且目标 CPU 必须支持 PEBS-LL。

## 7. 限制与注意事项

1. **统计近似**：采样而非全量追踪，样本足够多才可信；短时/稀疏热点可能漏采。
2. **仅 Precise Events 子集**：不是所有事件都能 PEBS，且不同微架构的 record
   格式与支持事件不同（需查对应 SDM Vol.3B Ch.20）。
3. **perf 不导出原始 record**：`perf` 只暴露处理后的字段，要原始字节需
   `pebs-grabber`。
4. **采样开销仍存在**：虽远低于中断式采样，但 record 写内存 + buffer flush
   仍有代价，超大采样率会干扰被测程序。
5. **权限与虚拟化**：需要 root（或合适的 `perf_event_paranoid`）；虚拟机里
   PEBS 通常不可用或受限，本项目的 QEMU DRAM 验证环境无法直接测 PEBS，需在
   dnet 实机（裸金属 Intel）上采集。
6. **DLA 是线性地址**：只有 load/store 的精确事件才带 DLA，且需页表转换才能
   落到物理页，用于内核内存分层决策时要处理进程生命周期与地址空间变化。

## 8. 参考

- Intel SDM Vol.3B Chapter 20「Performance Monitoring」—— PEBS record 格式、
  Data Source 编码、Precise Events 定义。
- [perf-book: Precise Event Based Sampling (PEBS)](https://github.com/dendibakh/perf-book/blob/master/chapters/6-CPU-Features-For-Performance-Analysis/6-7%20Precise%20Event%20Based%20Sampling%20(PEBS).md)
- [perf-book: Analyzing Memory Accesses（DLA / load latency，同章 §sec_PEBS_DLA）](https://github.com/dendibakh/perf-book/blob/master/chapters/6-CPU-Features-For-Performance-Analysis/6-7%20Precise%20Event%20Based%20Sampling%20(PEBS).md)
- [Red Hat: Profiling memory accesses with perf mem](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/monitoring_and_managing_system_status_and_performance/profiling-memory-accesses-with-perf-mem_monitoring-and-managing-system-status-and-performance)
- [perf-mem(1) manpage](https://man.archlinux.org/man/perf-mem.1.en)
- Linux 内核 `arch/x86/events/intel/ds.c`（PEBS 与 Data Source 解码实现）
- [pebs-grabber（导出原始 PEBS record）](https://github.com/andikleen/pmu-tools/tree/master/pebs-grabber)
- [easyperf: Understanding performance events skid](https://easyperf.net/blog/2018/08/29/Understanding-performance-events-skid)
- 内存分层 + PEBS 的学术背景：页面访问频率分布驱动的内存分层（如 Nimble、
  HeteroMem 等工作中将 PEBS 作为硬件采样基线之一）；MEMTIS（SOSP'23）的完整
  算法分析见本项目 [memtis-algorithm-analysis.md](../research/memtis-algorithm-analysis.md)。
