# MEMTIS 算法分析：基于 PEBS 的动态页面分类与页面大小判定

> 本文分析 [MEMTIS](https://github.com/memtis/memtis)（SOSP'23，
> *"MEMTIS: Efficient Memory Tiering with Dynamic Page Classification and
> Page Size Determination"*）的算法，重点是它如何用 PEBS 做**页面级热度
> 采样**、如何据此做**热/冷分类**，以及如何判定**大页该整页迁移还是拆分**。
> 源码依据来自 `~/Development/repos/memtis/linux/` 下的
> `mm/htmm_sampler.c`、`mm/htmm_core.c`、`mm/htmm_migrater.c` 与
> `include/linux/htmm.h`。
>
> 本文与 [pebs-analysis.md](../pebs/pebs-analysis.md) 配套：那篇讲 PEBS 机制本身，
> 这篇讲一个把它落地到内存分层的完整实例，并讨论它对本项目（Hermit / RDMA
> swap）的借鉴意义。

## 1. 背景与目标

MEMTIS 面向两层内存：**快速层 DRAM** + **慢速层**（Intel Optane DCPMM，或
CXL 模拟的远端 DRAM）。它要解决两个问题：

1. **哪些页应该放快层？**（页面分类 / 热度测量）
2. **以什么粒度迁移？**（整块 2 MiB THP，还是拆成 4 KiB base page）

传统方案用 PTE access bit 或 LRU 做分类，精度粗、无延迟/来源信息；MEMTIS
改用 **PEBS** 硬件采样，拿到「每页访问频率 + 访问发生在哪一层」的精确信号。
页面大小方面，MEMTIS 的洞察是：**大页该不该整页留在 DRAM，取决于页内访问的
均匀程度（skewness）**——访问均匀则整页迁移划算，访问集中在少数子页则应拆开，
只把热的 4 KiB 留在快层。

## 2. 系统结构：三个内核线程/组件

| 组件 | 文件 | 职责 |
| --- | --- | --- |
| `ksamplingd` | `htmm_sampler.c` | 每 CPU 打开 PEBS 事件，读 perf ring buffer，把样本转成页面访问计数 |
| `pginfo` 分类逻辑 | `htmm_core.c` | 维护每页/每子页访问计数、热度直方图、冷却、阈值自适应、拆分判定 |
| `kmigraterd` | `htmm_migrater.c` | 按阈值做降级（demotion）与提升（promotion）迁移 |

三者通过每 memcg 的 `access_lock`、直方图与阈值（`active_threshold` /
`warm_threshold` / `bp_active_threshold` / `split_threshold`）通信。

## 3. 采样层：`ksamplingd`（PEBS 页面热度采样）

### 3.1 采样哪些事件

`include/linux/htmm.h` 定义了 7 个 PEBS 事件（raw event code）：

```c
#define DRAM_LLC_LOAD_MISS        0x1d3   /* LLC load miss，命中本地 DRAM */
#define REMOTE_DRAM_LLC_LOAD_MISS 0x2d3   /* LLC load miss，命中远端 DRAM（CXL）*/
#define NVM_LLC_LOAD_MISS         0x80d1  /* LLC load miss，命中 NVM/Optane */
#define ALL_STORES                0x82d0  /* 所有 store */
#define ALL_LOADS                 0x81d0  /* 所有 load */
#define STLB_MISS_STORES          0x12d0  /* STLB miss 的 store */
#define STLB_MISS_LOADS           0x11d0  /* STLB miss 的 load */
```

关键设计：**用 LLC load miss 且带 `0x1d3/0x2d3/0x80d1` 这类数据来源区分的事件，
直接区分「这次 load 的数据来自 DRAM / NVM / 远端」**。也就是说 MEMTIS 不靠
事后推断，而是让硬件在采样时就把命中层级写进 Data Source 字段。这也正是
`pebs-analysis.md` §3.3 里 `mem_lvl` 的 `Local RAM` / `Remote RAM` 语义的内核级
用法。

每个样本只取三个字段（`htmm_sampler.c` 的 `htmm_event`）：

```c
struct htmm_event {
    struct perf_event_header header;
    __u64 ip;
    __u32 pid, tid;
    __u64 addr;        /* Data Linear Address（PEBS DLA）*/
};
```

perf 事件配置（`__perf_event_open`）：

```c
attr.sample_type = PERF_SAMPLE_IP | PERF_SAMPLE_TID | PERF_SAMPLE_ADDR;
attr.exclude_kernel = 1;   /* 只采样用户态 */
attr.precise_ip = 1;       /* 请求精确采样（PEBS）*/
attr.sample_period = ...;  /* 动态周期，见 3.3 */
```

### 3.2 从样本到页面

`ksamplingd` 主循环（`ksamplingd()`）遍历每 CPU × 每事件的 perf ring buffer，
对每条 `PERF_RECORD_SAMPLE` 做：

1. `valid_va(addr)` 过滤非法地址；
2. `update_pginfo(pid, addr, event)` 更新页面访问计数。

`update_pginfo`（`htmm_core.c`）再 `find_vma(mm, addr)` 找到线性地址所属 VMA，
走页表（PGD→PUD→PMD→PTE）定位到具体页，然后：

- 若是 **2 MiB THP**（`pmd_trans_huge`）→ `update_huge_page()`；
- 若是 **base page** → `update_base_page()`。

返回 1 表示「该页当前在快层（DRAM）」，返回 2 表示「在慢层（NVM/远端）」，
据此累计 `nr_dram_sampled`（见 §4.4 的命中率）。

### 3.3 采样率自适应（控制 CPU 开销）

PEBS 虽比中断采样便宜，但样本多了仍有开销。MEMTIS 用**自适应采样周期**：
`ksamplingd` 每 15s 算一次自己的 CPU 占用（`cputime`），与软配额
`ksampled_soft_cpu_quota` 比较，超了就把采样周期沿一张**质数表**上调、低了就
下调：

```c
static const unsigned int pebs_period_list[pcount] = {
    199, 293, 401, 499, 599, 701, 797, 907, 997, 1201, 1399, 1601,
    1801, 1999, 2503, 3001, 3499, 4001, 4507, 4999, 6007, 7001,
    7993, 9001, 10007, 12007, 13999, 16001, 17989, 19997 };
```

质数周期是为了避免与程序的固定访问模式同步共振。store 指令用另一张更粗的表
（10 万～150 万指令/样本），因为 store 比 load 更频繁、单样本价值更低。

## 4. 页面分类：`pginfo` 与热度直方图

### 4.1 每页的访问计数

- 每个 base page 有一个 `pginfo_t`，存 `total_accesses` 与 `nr_accesses`。
- 每个 2 MiB THP 有 512 个子页，各自的计数复用 compound page 的 tail page
  结构体（`page[4 + i/4].compound_pginfo[i%4]`，见 `get_compound_pginfo`），
  外加一个「整页」计数 `meta_page->total_accesses`。

计数尺度：base page 每命中一次 `total_accesses += HPAGE_PMD_NR`（+512）；
THP 子页也是 +512，整页 `meta_page->total_accesses++`（+1）。这样 base 页与
大页各用自己一致的尺度，分别进入两套直方图。

### 4.2 对数分桶 `get_idx`

把访问次数映射到 16 个热度桶（0–15），本质是 `floor(log2(n+1))`、封顶 15：

```c
unsigned int get_idx(unsigned long num) {   /* htmm_core.c */
    unsigned int cnt = 0;
    num++;
    while (1) {
        num >>= 1;
        if (num) cnt++;
        else return cnt;
        if (cnt == 15) break;
    }
    return cnt;
}
```

桶 `i` 大致覆盖 `[2^i - 1, 2^(i+1) - 2]` 次访问，用「数量级」而非精确计数，
能容忍采样噪声。

### 4.3 冷却（指数衰减）

访问计数不能只增不减，否则历史热度会盖过当前热度。MEMTIS 用**冷却**
（`check_base_cooling` / `check_transhuge_cooling`）：每个 memcg 维护一个
`cooling_clock`，每次冷却 +1；当某页的 `cooling_clock` 落后时，把它的访问
计数按落后周期数**右移（折半）**：

```c
for (j = 0; j < diff; j++)
    pginfo->total_accesses >>= 1;
```

这等价于对访问计数做指数衰减，让分类反映的是「最近」的访问热度。冷却由
采样数触发：`memcg->nr_sampled % htmm_cooling_period == 0`（默认 200 万样本）
或内存用量上涨超过阈值时。

### 4.4 两套直方图与热阈值自适应

MEMTIS 维护两套直方图：

- `hotness_hg[16]`：**实际大页**的热度分布（每个大页按整页计数入桶）；
- `ebp_hotness_hg[16]`：**预估 base 页**的热度分布（每个大页按 512 个子页分别
  入桶），即「如果拆成大页会是什么分布」。

`__adjust_active_threshold`（`htmm_core.c`）从最热桶（15）往下累加页数，直到
页数填满 DRAM 预算 `max_nr_dram_pages`，得到**热阈值** `active_threshold`：
`idx >= active_threshold` 的页是「热页」（应留在 DRAM）。同理对 `ebp` 直方图算
出 `bp_active_threshold`。还有一个 `warm_threshold = active_threshold - 1` 的
「温带」，用于避免边界抖动（见 §6）。

同时记录两个命中率：

- `nr_dram_sampled` → 实际 DRAM 命中率（rHR）：采样落在快层页的比例；
- `max_dram_sampled` → 理想 DRAM 命中率（eHR）：若每个子页都能按 base 页
  精度理想放置，能命中的比例（即 `may_hot` 子页的采样）。

两者都做指数平滑（右移折半 + 累加），得到 `prev_dram_sampled`（rHR）与
`prev_max_dram_sampled`（eHR）。**eHR − rHR 的差就是「拆分大页能挽回多少
命中率」的估计**，直接驱动 §5 的拆分判定。

## 5. 页面大小判定：skewness 与拆分

这是 MEMTIS 最有借鉴价值的部分——它把「该用大页还是小页」变成一个可计算的
判据。

### 5.1 页内访问偏斜度 skewness

对每个 2 MiB 大页，`check_transhuge_cooling` 计算：

```c
skewness += (pginfo->total_accesses * pginfo->total_accesses); /* Σ H_ij² */
...
skewness /= 11;                      /* 缩小 */
skewness = skewness / hot_utils;
skewness = skewness / hot_utils;     /* 再除以 hot_utils 两次 */
skewness = get_skew_idx(skewness);   /* 映射到 0..20 的偏斜桶 */
```

其中 `hot_utils` 是「热子页」个数（`idx >= bp_hot_thres` 的子页数）。这个
`ΣH_ij² / hot_utils²` 本质是页内访问的**集中度**（类似变异系数/参与度）：

- **访问均匀**（512 个子页都差不多热）→ skewness 低 → 整页迁移划算；
- **访问集中**（只有少数子页热）→ skewness 高 → 整页留在 DRAM 会浪费容量，
  应**拆成 base page**，只把热子页留快层。

特例：`meta->idx >= 13` 的「极热页」强制 skewness = 0（整体都热，不拆）。

### 5.2 拆多少：`nr_split`

`set_memcg_nr_split`（`htmm_core.c`）按命中率差 + 延迟差算出该拆多少页：

```c
ehr = prev_max_dram_sampled * 95 / 100;   /* 理想命中率 */
rhr = prev_dram_sampled;                  /* 实际命中率 */
if (ehr <= rhr) return;                   /* 拆分无收益就不拆 */

avg_accesses_hp = sum_util / num_util;    /* 每大页平均访问数 */
nr_records = (cooling_period << 1) - (cooling_period >> (cooling_clock - 1));

nr_split  = (ehr - rhr) * sum_util / nr_records;   /* 命中率差 × 样本量 */
nr_split /= avg_accesses_hp;                        /* 折算成页数 */
nr_split *= (captier_lat - DRAM_ACCESS_LATENCY);    /* × 延迟差 */
nr_split /= DRAM_ACCESS_LATENCY;
nr_split *= HPAGE_PMD_NR;                           /* × 大页页数粒度 */
nr_split *= htmm_gamma; nr_split /= 10;             /* × 缩放因子 0.4 */
```

直觉：**理想命中率比实际命中率高越多、慢层延迟越贵，就越值得把更多大页拆开
做细粒度放置**。延迟常量在头文件里：DRAM = 80、NVM = 270、CXL = 170 cycles。

### 5.3 拆哪些：`split_threshold`

`set_memcg_split_thres` 从最偏斜的桶（20）往下数，累计页数直到凑够
`nr_split`，得到 `split_threshold`。运行时 `check_split_huge_page` 判断一个大页
是否进 deferred split 队列：`meta->skewness_idx >= split_threshold - 1` 即拆分。
即**优先拆那些「页内访问最集中」的大页**。

拆分用 Linux 已有的 deferred split 机制（`deferred_split_huge_page_for_htmm`），
拆完后子页各自按 base page 的 `bp_active_threshold` 分类。

## 6. 迁移层：`kmigraterd`（demotion / promotion）

`htmm_migrater.c` 的 `kmigraterd` 是每 NUMA node 的迁移线程：

- **降级（demotion）**：快层用量超过水位（`need_toptier_demotion`）时，扫描
  LRU 找冷页迁到慢层。判据：`idx < warm_threshold` 才可降级——热页（含温带）
  跳过，避免把刚变热的页误降。
- **提升（promotion）**：慢层页被再次访问、冷却后仍热（`still_hot`），迁回
  快层；用 `active_threshold` 判「仍热」。
- **水位**：demotion 水位 = DRAM 预算的 2%，promotion 水位 = 3%（`get_memcg_*_watermark`），
  形成滞回，避免频繁抖动。
- **迁移限速**：`MAX_MIGRATION_RATE_IN_MBPS = 2048`（2 GB/s），防止迁移风暴
  干扰前台负载。

`warm_threshold`（温带）的作用：demotion 只迁 `idx < warm_threshold`（比热阈值
再低一档）的页，promotion 迁 `idx >= active_threshold` 的页，两者之间是缓冲带，
减少边界页在快/慢层间反复横跳。

## 7. 关键参数汇总（源码默认值）

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `htmm_cooling_period` | 2,000,000 | 每多少样本触发一次冷却（折半） |
| `htmm_adaptation_period` | 100,000 | 每多少样本自适应一次热阈值 |
| `htmm_split_period` | 2 | 拆分判定时把 memcg 工作集右移的位数（≥ WSS/4 样本才判定） |
| `htmm_thres_split` | 2 | 拆分开关（0 关闭） |
| `htmm_gamma` | 4 | `nr_split` 缩放因子（/10 即 0.4） |
| `htmm_thres_hot` | 1 | 热阈值下限（idx 至少 1，即 ≥2 次访问） |
| `BUFFER_SIZE` | 32 | perf ring buffer 页数（32×4 KiB） |
| `MAX_MIGRATION_RATE_IN_MBPS` | 2048 | 迁移限速 |
| DRAM/NVM/CXL 延迟 | 80/270/170 cycles | 拆分收益权重 |

这些都可经 `/sys/.../htmm/`（`mempolicy.c` 里注册的 kobject 属性）或内核参数
调整，但默认值已经体现了论文的选择。

## 8. 与 PEBS 文档的衔接

MEMTIS 是 `pebs-analysis.md` §6 设想的**现成实现**，对应关系：

| PEBS 文档的概念 | MEMTIS 的实现 |
| --- | --- |
| Data Linear Address（DLA） | `htmm_event.addr` → `find_vma` + 页表定位到页 |
| Data Source 的 `Local/Remote RAM` | `DRAM_LLC_LOAD_MISS` / `NVM_LLC_LOAD_MISS` / `REMOTE_DRAM_LLC_LOAD_MISS` 三个事件区分命中层级 |
| 页面热度 heatmap | `pginfo->total_accesses` + 对数分桶 + 指数冷却 |
| 低开销采样 | 自适应采样周期（质数表 + CPU 软配额） |
| 指导「大页 vs 小页」 | §5 的 skewness → 拆分判定 |

这印证了 `pebs-analysis.md` 的核心判断：**PEBS 的价值不止「热不热」，而是
「访问频率 + 命中层级」，足以支撑页面粒度的放置决策**。

## 9. 对 Hermit / RDMA swap 的借鉴

MEMTIS 与 Hermit 是两种机制（NUMA 迁移 vs swap），但「传输/放置粒度」的决策
问题同构，可以逐条映射：

| MEMTIS | Hermit 可对应 |
| --- | --- |
| 2 MiB THP 整页 vs 拆 4 KiB base page | `remote_order_mask` 的 order 0–9 传输粒度 |
| 页内 skewness（访问是否均匀） | 每个 folio 内「被访问子页比例 `f`」——正是 page-sweep 里 `1/f` 读放大的实测来源 |
| eHR − rHR 判断拆分收益 | 「大粒度换入的协议带宽提升」vs「overfetch 代价」的净收益 |
| 冷却折半 → 反映最近热度 | 为每个远端 extent 维护衰减的访问计数，供回收/换出决策 |
| 迁移限速 2 GB/s | RDMA swap 的回收/换入限速，避免与前台抢带宽 |

具体三点可落地：

1. **用 PEBS DLA 在线测 `f`**：当前 page-sweep 靠离线 `read_amp` / `large_load_%`
   估算读放大，改用 PEBS 采样可对每个 folio 直接统计「被访问子页比例」，把
   倒 U 拐点从事后拟合变成在线可观测（详见 `pebs-analysis.md` §6.3）。
2. **把 `remote_order_mask` 从「全局位图」变成「按热度选择」**：对访问均匀
   （skewness 低、`f≈1`）的 extent 用大 order，对访问稀疏的用 4 KiB，正是
   MEMTIS「整页 vs 拆分」的 swap 版。
3. **复用冷却与指数衰减**：Hermit 目前对远端 entry 只有存在性追踪
   （`hermit_backend.c` 的 XArray extent），没有热度信息；可加一层衰减计数，
   为未来的「冷远端 extent 优先回收/主动换出」提供输入。

## 10. 局限与差异（照搬前的注意点）

1. **机制不同**：MEMTIS 是**内存迁移**（页仍在物理内存，只是搬到慢 NUMA
   node），Hermit 是 **swap**（远端无本地副本、按 swap entry 换入换出）。MEMTIS
   的「命中层级事件」直接对应物理内存层级；Hermit 换出的页不在本地，采样语义
   需要重新定义（换出后 load 会 page fault，PEBS 采不到普通 load）。
2. **THP 粒度固定 2 MiB**：MEMTIS 只有「整 2 MiB vs 4 KiB」两档；Hermit 有
   order 0–9（4 KiB–2 MiB）多档，skewness 判据要推广到多粒度。
3. **MEMTIS 采样的是在内存里的页**：`update_pginfo` 走 `find_vma` + 页表，
   且要求页 `pte_present`；Hermit 的远端页在换出后 PTE 是 swap entry，需换一条
   采样/统计路径（例如在 swap-in fault 时记录，而非靠 PEBS load 采样）。
4. **硬件依赖**：MEMTIS 依赖 Optane/CXL 与 `CONFIG_HTMM`；PEBS 采样部分依赖
   裸金属 Intel（QEMU 里不可用），且 `0x1d3/0x2d3/0x80d1` 等 raw event 是特定
   微架构的，移植到别的 CPU 要重查 SDM。
5. **样本稀疏**：MEMTIS 用「数量级分桶 + 指数冷却 + 200 万样本才冷却」来对抗
   稀疏，逐页精确计数不可行；移植到 Hermit 也应保持「区域/folio 粒度聚合」的
   思路，而非逐 4 KiB 页精确计数。

## 11. 参考

- MEMTIS 论文（SOSP'23）：Lee et al., *MEMTIS: Efficient Memory Tiering with
  Dynamic Page Classification and Page Size Determination*,
  doi:10.1145/3600006.3613167
- [MEMTIS 源码](https://github.com/memtis/memtis)（本地 `~/Development/repos/memtis`）
  - `linux/mm/htmm_sampler.c` —— PEBS 采样线程
  - `linux/mm/htmm_core.c` —— 页面分类 / 冷却 / 阈值自适应 / 拆分判定
  - `linux/mm/htmm_migrater.c` —— 降级 / 提升迁移
  - `linux/include/linux/htmm.h` —— 事件码、周期表、延迟常量
- 本项目配套：[pebs-analysis.md](../pebs/pebs-analysis.md)（PEBS 原理与用法）
