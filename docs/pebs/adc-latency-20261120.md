# Hermit ADC 换页延迟测量，2026-11-20

dnet-61 的同步 4 KiB 路径，应用随机触碰一个已换出页面的中位耗时为 **6.69 µs**，P95 为 **7.78 µs**。240 次冷访问产生了 240 次 major fault 和 240 个 4 KiB RDMA 读 WR。单独跟踪 32 次缺页，`do_swap_page` 平均 6.64 µs，其中后端读取占 3.75 µs，后端内部的完成轮询占 2.93 µs。[应用样本](../../tools/rdma/results/20261120-hermit-adc/native-python.log) · [函数跟踪](../../tools/rdma/results/20261120-hermit-adc/native-functions.csv)

## 真机从填充到取回

dnet-61 运行原有的 `6.18.38-hermit-pebs`。测试固定在 CPU 3，每轮填充 64 MiB 私有匿名内存，禁用该映射的大页，调用 `MADV_PAGEOUT`，在 80 个随机、不重复的区域各读一个片段，再读一次相同地址作为驻留对照。每种片段长度独立重复三轮。应用计时覆盖 Python 循环、每页一个 64 位载入和数据校验。PEBS 关闭，`bypass_swapcache=Y`，`speculative_io=N`。[负载源码](../../tools/rdma/results/20261120-hermit-adc/native_bench.py) · [环境](../../tools/rdma/results/20261120-hermit-adc/environment.txt)

| 应用操作 | 样本数 | P50 | P95 | major fault |
|---|---:|---:|---:|---:|
| 填充 64 MiB | 6 | 29.28 ms | — | — |
| 换出 64 MiB | 6 | 82.51 ms | — | — |
| 冷读 1 页 | 240 | 6.69 µs | 7.78 µs | 240 |
| 再读同一页 | 240 | 0.51 µs | 0.56 µs | 0 |
| 连续冷读 16 页 | 240 | 93.54 µs | 95.24 µs | 3,840 |
| 再读同一段 16 页 | 240 | 4.05 µs | 4.55 µs | 0 |

每轮换出发出 16,384 个 4 KiB 写 WR。16 页冷读每次触发 16 个读 WR。共 960 次冷热访问均通过数据校验。[应用统计](../../tools/rdma/results/20261120-hermit-adc/native-app.csv)

另一次实验用独立 tracefs 实例，在同一 CPU 仅跟踪测试 PID，换出 128 页并随机取回 32 页。两个阶段均记录到零 overrun 和零丢失事件。下表的平均值均按该函数完成的调用数计算。父函数包含子函数。轮询计时覆盖客户端进入轮询直到观察到完成，链路飞行时间、网卡处理时间和远端内存访问时间需要设备侧测量才能进一步区分。[跟踪源码](../../tools/rdma/results/20261120-hermit-adc/native_trace.py) · [原始轨迹](../../tools/rdma/results/20261120-hermit-adc/native-trace.log)

| 阶段 | 函数 | 调用数 | 均值 |
|---|---|---:|---:|
| 回收整批 | `shrink_folio_list` | 1 | 783.70 µs |
| 分配 swap 空间 | `folio_alloc_swap` | 128 | 0.42 µs |
| 解除映射 | `try_to_unmap` | 128 | 0.38 µs |
| 远端写入 | `hermit_backend_store` | 128 | 3.51 µs |
| 写入中的完成轮询 | `rswap_io_poll` | 128 | 2.80 µs |
| 处理换入缺页 | `do_swap_page` | 32 | 6.64 µs |
| 查找 swapcache | `swap_cache_get_folio` | 32 | 0.19 µs |
| memcg 计费 | `mem_cgroup_swapin_charge_folio` | 32 | 0.23 µs |
| 预留 swapcache 槽位 | `swapcache_prepare` | 32 | 0.14 µs |
| 远端读取 | `hermit_backend_load` | 32 | 3.75 µs |
| 读取中的完成轮询 | `rswap_io_poll` | 32 | 2.93 µs |

## 新内核 ADC 的实测

在 Linux `4abab0c89e` 上移植了旧 Hermit ADC 的区间计时、major/minor 分类和阶段累计，并为 6.18 的 SG 路径增加提交、轮询、两次 PTE 扫描及完成处理。新接口 `/sys/kernel/debug/hermit/adc_enable` 默认关闭。`adc_stats` 导出 31 行，记录调用次数和累计单调时钟纳秒数。对稳定的恒定速率 TSC 另记录样本数和有序读取的参考时钟 ticks。纳秒使用内核时钟；该 TCG 客户机的 TSC 样本数为零。各行是全系统累计的包含式时间，按测试阶段取差值；`user_addr_fault` 也包含用户地址范围内的普通缺页。用户态端到端计时包含异常入口和返回开销，二者没有独立的 ADC 行。[内核补丁](../../tools/rdma/results/20261120-hermit-adc/linux-6.18-adc.patch) · [ADC 实现](../../linux-stable/include/linux/hermit_adc.h)

QEMU 使用 TCG 和 Hermit DRAM 后端。下表的微秒数含指令模拟与软件后端的开销，真机的 RDMA 延迟见前节。4 KiB 测试覆盖同步直接读取、异步直接读取、默认策略 `0x1bf`，各测 1 页和 16 页，在开关 ADC 两种状态下重复三轮。另测两个允许 64 KiB folio 的直接换入组合，共 38 组、3,040 次访问，数据校验、后端错误和读取未命中均为零。所有 ADC 关闭的窗口内计数增量均为零。开关对照的延迟随组合波动，未得到可移用于真机的计时开销系数。[逐次数据](../../tools/rdma/results/20261120-hermit-adc/qemu-trials.csv) · [开关对照](../../tools/rdma/results/20261120-hermit-adc/qemu-app.csv)

| QEMU 策略单页路径，ADC 开启 | 次数 | 平均每次 |
|---|---:|---:|
| 整个 `do_swap_page` | 240 | 201.50 µs |
| 其中 `hermit_swapin_pages` | 240 | 128.01 µs |
| 建立或查找异步 swapcache 页 | 240 | 38.09 µs |
| SG 后端提交 | 240 | 23.51 µs |
| SG 后端轮询 | 240 | 2.43 µs |
| SG 提交前和完成后的 PTE 扫描 | 480 | 4.21 µs |
| SG 完成处理 | 240 | 4.73 µs |
| 安装需求页 PTE | 240 | 1.03 µs |

4 KiB 策略单页访问的 240 次缺页只读了 240 个基页，却执行了 240 次批提交和 480 次 PTE 扫描。这给出了单页路径中能检验的固定工作。策略连续读 16 页时，240 个片段有 1,440 次 major fault 和 2,400 次 minor fault。DRAM 后端共读入 3,885 个基页。启用 64 KiB folio 后，每种访问长度各 80 次都只触发 80 次 major fault，但都取回 1,280 个基页。后端的 80 次 order-4 读取均记录了 4 KiB 传输回退。这组数据能区分 folio 数、传输粒度和实际触碰的页数。[ADC 分段](../../tools/rdma/results/20261120-hermit-adc/qemu-adc.csv) · [后端计数](../../tools/rdma/results/20261120-hermit-adc/qemu-backend.csv)

### 内存压力造成的自然回收

另在隔离客户机中将已填充 64 MiB 映射的 cgroup 上限降至 16 MiB。直接路径回收了 13,086 个基页，执行 13,086 次引用检查和 13,086 次单 folio 后端写入。策略路径写出 13,087 个基页，执行 829 次批提交和 829 次批轮询，平均每批 15.8 页。ADC 捕获的批提交平均 119.16 µs、批轮询平均 0.88 µs；DRAM 后端在提交期间完成内存复制。两组均执行 205 次回收扫描；各自随后随机触碰 80 个位置，其中 59 次产生 major fault，全部通过校验。[内存压力分段](../../tools/rdma/results/20261120-hermit-adc/qemu-cgroup-adc.csv) · [原始记录](../../tools/rdma/results/20261120-hermit-adc/guest-cgroup.log)

## 与此前单页劣化的关系

11 月 19 日 dnet-61 的 8 GiB、10% 本地内存实验也区分了这两条路径。筛选单页试次中确实发生 major fault 的样本，每格 43 次。关闭策略时，4 KiB 和 64 KiB 分配上限的中位数分别为 9 和 24 µs；默认策略下分别为 8 和 9 µs。64 KiB 上限且策略关闭时，跨所有片段长度共有 771 次 order-4 换入；默认策略下记录的是 order-0。历史计数按整格汇总，不能把 771 次逐一归属到单页试次。此次真机同步基线测出了完成等待的成本，隔离实验也验证了大 folio 读放大和单页 SG 准备工作。各自对历史 24 µs 的贡献仍缺同配置真机分段数据。[历史逐次数据](../../tools/rdma/results/dnet-61/20261120/swapin-granularity-20261119-170532/swapin-granularity.csv) · [历史分阶计数](../../tools/rdma/results/dnet-61/20261120/swapin-granularity-20261119-170532/swapin-cost.csv)

在启动带此补丁的内核后，已有的粒度基准可加 `--adc`。每个配置目录生成原始快照和 `adc.csv`。cgroup 触发方式分别记录填充、降低内存上限后的回收、以及读回窗口。使用 `MADV_PAGEOUT` 时，外部驱动取得的第一个窗口同时包括填充和换出。驱动结束时恢复 ADC 原始开关。[基准驱动](../../tools/rdma/bench/run_swapin_granularity.sh)

新内核和 DRAM 后端模块编译通过。关闭 `CONFIG_HERMIT` 后，受影响的通用内存管理和 x86 缺页对象也编译通过。RDMA 测试套件通过 84 项测试及 31 项子测试；沙箱的 ptrace 与 LeakSanitizer 不兼容，测试时关闭了泄漏检测，保留地址和未定义行为检查。
