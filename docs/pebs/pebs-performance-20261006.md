# PEBS 实机性能评估 20261006

修复版 `6.18.38-hermit-pebs #9` 已在 dnet-61 启动并完成三阶段 **91/91 条有效正式测量**。测试时间为 2026-10-06 15:56:37–18:04:11（UTC+8），18:05:35 独立收尾验收通过。此前零样本修复见 [采样修复与验证](pebs-sampling-fix-20261006.md)；旧内核零样本试跑、装载 OOM 和短时预检均未混入正式统计。

当前策略在本次 Memcached 压力负载下获得了明确的端到端收益：中频策略相对关闭采样、静态全 order mask 的五轮配对吞吐提升中位数 **123.76%**，范围 **117.39%–144.35%**；相对静态 4 KiB 为 **223.10%**，范围 **204.44%–250.32%**。收益同时伴随 RDMA 读流量/请求下降，而不是只改变了配置开关。

采样成本取决于场景：无换页时三档纯采样的 CPU/请求配对增量中位数约 **0.52%–0.79%**；饱和换页负载下，中频纯采样的吞吐配对变化中位数为 **−6.20%**，范围 **−20.07% 至 +3.49%**。因此不能将当前实现概括为“所有场景开销低于 1%”。中频策略在固定 30000 QPS 下仍有明显尾延迟波动，不能宣称各项性能都稳定改善。

[完整三阶段统计](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/SUMMARY.md) · [总览 PDF](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/overview.pdf) · [采样率图 PDF](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/sampling-cost.pdf)

![性能总览](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/overview.png)

## 采样本身的开销

resident 全部 21 次测量的 RDMA 读写字节与换页策略决策均为零，开启采样组都有有效样本。以下 static 对比 off，固定 store 周期为 1500003，每档三次重复。数值为中位数，方括号为最小–最大范围。

| load 周期 | 实测 samples/s | CPU/请求配对变化 | read p99 配对变化 | read p99 µs |
|---:|---:|---:|---:|---:|
| 19997 | 821.37 | +0.73% [+0.66%, +0.78%] | +5.98% [+4.72%, +6.49%] | 24.6 [24.4, 24.8] |
| 1999 | 867.56 | +0.52% [+0.41%, +0.70%] | +6.01% [+5.98%, +8.23%] | 24.8 [24.7, 25.0] |
| 199 | 1323.84 | +0.79% [+0.48%, +0.92%] | +7.26% [+6.87%, +7.36%] | 24.9 [24.8, 25.1] |

关闭采样时 CPU/请求中位数为 142.028 µs、read p99 为 23.3 µs。纯采样的 read p99 中位数绝对增加约 1.3–1.6 µs。这里的 CPU 成本包含硬件采样、ring 消费与区域维护，采样线程 CPU 只是其中一部分；固定 QPS 达成不能证明最大吞吐没有损失。

在 pressure 固定负载中，static 相对 off 的单位请求系统 CPU 配对变化如下。该场景包含回收和 RDMA 行为波动，不能把全部变化归因于硬件 PEBS 指令成本。

| 纯采样配置 | CPU/请求配对变化 |
|---|---:|
| static-low | +0.03% [-8.04%, +0.52%] |
| static-medium | +4.26% [-3.03%, +6.69%] |
| static-high | +0.71% [-3.48%, +4.72%] |
| static-adaptive | +1.09% [+0.58%, +4.07%] |

## 策略的端到端收益

### 不限速吞吐（saturation）

保持相同客户端线程与连接参数，取消 QPS 限制，每组五次重复。这是该并发配置下的可达吞吐，未做连接数扫描，不能当作硬件的绝对吞吐上限。

| 配置 | QPS 中位数 [范围] | read p99 µs | 系统 CPU µs/请求 | RDMA read KiB/请求 |
|---|---:|---:|---:|---:|
| off-original-mask | 44492.0 [42009.3, 47738.9] | 12848.1 | 168.88 | 49.64 |
| off | 63423.6 [60227.7, 65582.1] | 10156.0 | 118.41 | 43.58 |
| static-medium | 59489.3 [52372.5, 64387.6] | 10773.1 | 122.88 | 44.04 |
| policy-medium | 145338.4 [137874.0, 152345.3] | 5444.1 | 48.90 | 3.59 |

| 中频策略的对照 | QPS 配对变化 | read p99 配对变化 | CPU/请求配对变化 | RDMA read/请求配对变化 |
|---|---:|---:|---:|---:|
| static-medium | +136.11% [+118.74%, +190.89%] | -49.64% [-51.64%, -48.83%] | -59.40% [-63.67%, -57.50%] | -91.73% [-91.88%, -91.23%] |
| off | +123.76% [+117.39%, +144.35%] | -47.00% [-48.88%, -46.04%] | -58.24% [-60.62%, -57.64%] | -91.79% [-92.19%, -90.93%] |
| off-original-mask | +223.10% [+204.44%, +250.32%] | -58.36% [-69.70%, -57.38%] | -70.57% [-72.94%, -69.82%] | -92.58% [-93.34%, -91.94%] |

上述百分比先按同轮次计算 `(实验/对照−1)×100`，再取中位数，**不是两个配置中位数的比值**。范围是五次观察值的极值，不是置信区间。相对同周期 static 的收益包括策略执行成本；相对 off 的结果才是启用采样和策略后的净变化。

### 固定 30000 QPS 的压力负载（pressure）

每组五次重复；CPU 和 RDMA 字节均除以客户端实际完成的 GET+SET 总请求数。大 WR 占比按 RDMA 读取字节统计。

| 配置 | read p99 µs 中位数 [范围] | CPU µs/请求 | RDMA read KiB/请求 | 大 WR 读字节占比 % | samples/s |
|---|---:|---:|---:|---:|---:|
| off-original-mask | 6506.1 [2065.8, 13674.2] | 218.24 | 56.26 | 0.00 | 0.00 |
| off | 4186.2 [428.8, 5832.3] | 190.11 | 50.90 | 98.06 | 0.00 |
| static-low | 5701.0 [643.2, 6831.5] | 189.73 | 48.27 | 98.06 | 825.87 |
| policy-low | 4255.5 [464.9, 4992.9] | 185.02 | 42.01 | 96.57 | 815.35 |
| static-medium | 4558.0 [2917.9, 6310.8] | 198.45 | 55.94 | 98.05 | 953.95 |
| policy-medium | 4383.5 [479.7, 40046.3] | 162.64 | 7.93 | 50.36 | 892.13 |
| static-high | 775.7 [586.2, 7744.5] | 195.25 | 54.35 | 98.05 | 2166.38 |
| policy-high | 612.1 [603.9, 1804.6] | 159.99 | 4.64 | 11.33 | 1485.18 |
| static-adaptive | 5901.0 [635.3, 11325.4] | 192.19 | 50.89 | 98.04 | 13135.23 |
| policy-adaptive | 1899.3 [613.7, 3040.7] | 160.28 | 4.66 | 11.65 | 12795.34 |

高频策略相对 off 的 CPU/请求配对变化中位数为 −15.69%（−20.66% 至 −15.47%），RDMA 读取字节/请求为 −90.90%（−92.22% 至 −90.70%）；相对同周期纯采样分别为 −17.80% 和 −91.46%。这证明本次负载中策略能通过减少读流量获得净成本收益。

中频策略同样稳定减少 CPU/请求和 RDMA 流量，但 read p99 为 4383.5 µs，五轮范围 479.7–40046.3 µs；相对同周期 static 的尾延迟配对变化范围跨过零，不能用前两轮约 480 µs 的结果代表最终效果。高频策略的 read p99 为 612.1 µs、范围 603.9–1804.6 µs，在这次压力矩阵中更稳定；其不限速吞吐尚未测试，不能将中频的吞吐提升直接套用到高频。

## 采样周期与策略效果

![实际采样率与性能成本](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/sampling-cost.png)

固定周期组仅改变 load 阈值，store 保持 1500003；因此 load 周期变化 100 倍，实际可用样本率不必变化 100 倍。统计未按 load/store 事件拆分，不能据此精确推算两种事件各自的成本。

pressure 中 policy-low 的 unknown 占比中位数为 21.90%，大 WR 读字节占比仍有 96.57%；policy-medium/high 的 unknown 约为 0.0169%/0.00117%，大 WR 占比降至 50.36%/11.33%。低频配置对当前策略的信息覆盖明显较弱。这与更稀疏的采样导致策略回退更多的解释一致，但尚未通过 `perf mem` 真值位图对照证明采样准确率。

所有自适应组测量前后快照均为 load=199、store=100003；端点相同不能证明区间内从未改变。policy-adaptive 实测约 12795 samples/s，而固定 high 约 1485 samples/s，前者并未显示对应的额外 CPU/流量收益。两者 store 周期不同，不能将差异全部归因于 load 采样频率。

就本次工作负载，固定 high 是改善压力场景策略覆盖和尾延迟的候选设置；medium 已验证较高的不限速吞吐，但尾延迟风险需要单独处理。不能仅为减少样本而选 low，也不能仅因启用“自适应”就认为开销更小。进一步优化应先补充分事件记录数、无效地址计数和 PMI/消费耗时，再验证固定 high 的饱和吞吐及中频尾延迟来源；这些改进的额外收益本次未测量。


## 内核和测量对象

服务器运行基于 `b40a5cab09`、加入固定周期控制和样本记录头修复的内核，版本为 `6.18.38-hermit-pebs`，构建号 9。并非本地 `83a296eeea` 全部改动的部署。启动镜像 SHA256 为 `aa522176494ee343f90b97a5ca2d90c9094c5a5f1e7bd19edec364e524ff2e5b`。

匹配的 OFED 和 rswap 模块已安装；dnet-61 mlx5_0 及 dnet-58 内存服务器链路 ACTIVE。使用现有 `/dev/sdb6` swap、远端 `172.16.0.58:9400`、48 GiB 后端容量、max_order=9。未重新格式化 swap。

负载为 Memcached 1 KiB value、800000 项，客户端参数为 `--update=0.1 -T 4 -c 64 --iadist=fb_ia`。服务与客户端分别绑定 NUMA 0 的 CPU 0–3 和 4–7，服务内存绑定 NUMA 0。服务 4 线程，客户端 4 线程。每个配置重新启动进程和 cgroup，并重新装载数据；开启采样的配置从装载前开始采样，贯穿预热与测量，使区域历史在本次负载中建立。装载完成后施加 memory.high，避免将装载阶段 OOM 混入稳态测量。

所有配置使用相同的 64 KiB THP 分配设置，其他 THP 页档禁用。bypass_swapcache=Y、speculative_io=Y、lazy_poll=N、apt_reclaim=Y、reclaim_mode=1、headroom=16384 pages、sthd_cnt=4。pressure 和 saturation 的 memory.high=640 MiB、memory.max=2048 MiB；resident 两者均为 2048 MiB。memory.high 是软压力阈值，实际驻留量可暂时超过该值；本实验不等同于此前 70% memory.max 硬限额基线。

## 配置矩阵

| 配置 | 采样 | 决策 | load 周期 | store 周期 |
|---|---|---|---:|---:|
| off-original-mask | 关闭 | 静态 4 KiB 传输 mask | — | — |
| off | 关闭 | 静态全 order mask | — | — |
| static-high 和 policy-high | 开启 | 分别静态和策略 | 199 | 1500003 |
| static-medium 和 policy-medium | 开启 | 分别静态和策略 | 1999 | 1500003 |
| static-low 和 policy-low | 开启 | 分别静态和策略 | 19997 | 1500003 |
| static-adaptive 和 policy-adaptive | 开启 | 分别静态和策略 | 原自适应表 | 原自适应表 |

除 off-original-mask 使用原始 0x1 外，其余配置使用 remote_order_mask=0x3fd。实际 folio 配置仍固定为 64 KiB。固定周期组只改变 load 周期，store 周期不变；adaptive 同时调整两种事件周期，应作为独立方案解读。

周期是硬件事件数阈值，不是 Hz。实测 samples/s 使用 sampled 增量除以快照间隔，统计全机具有可用地址的样本，包含客户端等其他进程；它不是目标 memcg 的独立采样率。当前实现创建全机用户态事件（exclude_kernel=1、exclude_hv=1），再按 memcg 归属更新目标区域。实测采样率也受策略改变后的访存行为影响，因此它是观测量，不能直接作为独立控制变量解释因果。

| 阶段 | 配置 | 重复数 | 预热 | 测量 | offered QPS | 完成行数 |
|---|---|---:|---:|---:|---:|---:|
| pressure | 上述全部 10 组 | 5 | 30 秒 | 60 秒 | 30000 | 50 |
| resident | off 与三档固定周期 static/policy | 3 | 15 秒 | 45 秒 | 30000 | 21 |
| saturation | 两种 off、static-medium、policy-medium | 5 | 20 秒 | 60 秒 | 不限 | 20 |

每轮按固定随机种子打乱配置顺序。吞吐阶段的 medium 周期在查看正式结果前选定，用于避免事后挑选表现最好的周期造成偏差。

## 验收与比较方式

有效采样必须 sampled 增量大于 0；测量还检查 miss、skipped requests、eviction、OOM、Hermit order errors。记录 lost/throttled、各 order 决策、unknown、真实 WR 数和字节、folio 构成、CPU 及内存边界快照。策略是否实际参与由 decisions 判断；实际改变的传输由 WR 分布判断，不能只看配置开关。

每个阶段结束核验参数与 THP 恢复、sampler 停止以及内核日志。失败行和未完成矩阵保留，默认汇总工具拒绝把它们当完整结果。

- 采样开销：static 对比 off，页档和静态 mask 相同。
- 策略作用：同周期 policy 对比 static。
- 策略净收益：policy 对比 off，同时另看静态 4 KiB 对照。
- 固定 30000 QPS 主要比较尾延迟和单位请求 CPU，不能当最大吞吐；吞吐能力另看 saturation。
- 系统忙碌 CPU 包含服务、客户端、采样线程和内核执行；采样线程 CPU 只是一部分，不能当作 PEBS 总成本。
- 汇总报告中位数、最小到最大范围，并按同轮次配对计算百分比；这些重复不支持仅凭点估计声称统计显著。


## 验收结果、限制与最终状态

三个阶段分别完成 50、21、20 条测量，退出码均为 0。全部正式行无 OOM、后端 order error、eviction、Misses 或 Skipped TXs；原始客户端整数计数也已检查。采样开启组均有有效样本，PEBS ring 的 lost/throttled 记录均为零。每阶段恢复记录通过，未新增 BUG、Oops、soft lockup 或匹配的内核 WARNING 记录。

pressure 日志新增 **9 次 `perf: interrupt took too long`**，全局 `kernel.perf_event_max_sample_rate` 从阶段前日志最后记录的 24000 下调至 2750；起始值由日志推断，未直接快照。resident 和 saturation 无新增调整日志。固定事件周期不是 Hz，零 ring throttle 记录也不代表 perf 没有调整全局上限；各轮可能受其历史状态影响。这一限制保留在结果中，没有关闭 perf 的保护机制。

样本仅覆盖这台机器、800000 个 1 KiB value 的 Memcached 工作集及上述参数，未覆盖 Redis、其他容量、并发扫描或所有页档。CPU governor/频率未由本脚本单独固定；少量重复及 min–max 不能视作统计显著性或普遍性能保证。旧的 70% memory.max 基线与本次 memory.high 压力条件不同，不作跨实验百分比归因。

18:05:35 独立验收确认内核/模块 SHA256 与测试前一致；采样关闭，memcached、mutilate、hermit_pebsd 均退出，测试 cgroup 已清理，Hermit 和 THP 参数恢复到本次矩阵开始前设置。RDMA 模块与 swap 保持测试准备后的启用状态，swap 使用量为 4 KiB。perf 自动调整后的全局上限仍为 2750，`perf_cpu_time_max_percent=25`，未将其伪称为“所有系统状态都恢复”。证据见 [final-audit.json](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/final-audit.json)。

## 数据、图表与复现

- [完整汇总及各阶段链接](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/SUMMARY.md)
- [pressure CSV](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/pressure/summary.csv)、[resident CSV](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/resident/summary.csv)、[saturation CSV](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/saturation/summary.csv)
- [实际采样周期与频率](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/sampling-frequencies.csv)
- [执行脚本快照](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/driver.py)、[三阶段入口](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/campaign.sh)、[内核补丁](../../tools/rdma/results/pebs/20261006/pebs-campaign-20261006/kernel.patch)
- [完整归档](../../tools/rdma/results/pebs/20261006/pebs-performance-20261006.tar.gz)、[归档 SHA256](../../tools/rdma/results/pebs/20261006/pebs-performance-20261006.tar.gz.sha256)

远端结果目录为 `/home/xwz/hermit-baselines/pebs-campaign-20261006`。本地每个 case 保留装载/预热/测量日志、before/after 内核与进程计数、memcached stats、配置快照；每阶段保留 manifest、内核日志、恢复记录、汇总 CSV/JSON、PNG/PDF。短时预检独立保留，不进入正式矩阵。

重新生成分析与图表（不运行服务器负载）：

```sh
for phase in pressure resident saturation; do
  python3 tools/rdma/pebs/plot_pebs_perf.py tools/rdma/results/pebs/20261006/pebs-campaign-20261006/$phase
done
python3 tools/rdma/pebs/summarize_pebs_campaign.py tools/rdma/results/pebs/20261006/pebs-campaign-20261006
```

复测需使用新输出目录，先检查独占测试条件，再参考已归档的 campaign.sh；不要覆盖本次原始数据。性能脚本不执行内核安装或重启。
