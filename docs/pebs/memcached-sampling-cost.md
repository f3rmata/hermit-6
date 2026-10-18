# Memcached 固定并发下的 PEBS 采样开销测试

当前脚本所有模式均保留运行前的系统 THP/mTHP 配置，不写入页面大小设置，也不再支持 `--page-kb`。应用的实际驻留 folio 大小由系统和访问行为决定，policy 仍可改变换入决策。

**off 不再表示纯 4 KiB baseline。** off 保留 RDMA mask=0x1（4 KiB WR），off-same-pages、static、policy 保留 mask=0x3fd；WR 大小不等于分配页大小。采样开销仍使用 static 对比 off-same-pages，两者分配配置、传输 mask 和内存预算相同。历史结果维持原含义，不与本轮直接混合；当前版本不再运行或复用校准。

| 配置 | 页分配配置 | PEBS | load period | store period |
|---|---|---|---:|---:|
| off / off-same-pages | 保留系统配置 | 关闭 | — | — |
| static-low / policy-low | 保留系统配置 | mode=0 / 1 | 19997 | 1500003 |
| static-medium / policy-medium | 保留系统配置 | mode=0 / 1 | 1999 | 1500003 |
| static-high / policy-high | 保留系统配置 | mode=0 / 1 | 199 | 1500003 |

每轮在装载完成、预热结束、正式测量结束时分别生成 `pages-loaded/`、`pages-before/`、`pages-after/`，保存 `smaps`、`maps` 和 `page-sizes.json`。JSON 使用 pagemap 与 kpageflags 的 compound head/tail 统计实际驻留 folio 大小（KiB）、各档映射字节数和观察到的 folio 数；另列已换出、未驻留及无法识别的字节数。覆盖 Memcached 的全部映射，包含库和共享页，不等同于对象/slab 大小。已换出的页面不推断原 folio 大小。

这些是非原子快照，并发回收或拆分可能影响结果；统计不主动触页，也不纳入正式计时，但读取元数据可能扰动缓存及采样状态。不能只凭 `smaps` 的 KernelPageSize=4 KiB 或 THP enabled 判断没有 mTHP。观察实际分布，不能预先假定各模式页大小相同。检测到外部 THP 配置改变会停止；退出时不改写 THP。

各 static/policy 组固定 adaptive=0。period 越小采样越密，但不是 Hz；结果同时记录实际 samples/s。静态组的开销包括硬件采样、ring 消费和区域历史跟踪，不是单独的 PMU 中断成本。

## 启动

在 dnet-61 的 tmux 会话中，以普通用户执行：

```sh
cd ~/hermit-6
sudo python3 tools/rdma/pebs/run_memcached_sampling_cost.py \
  --user "$(id -un)" \
  --output "$PWD/tools/rdma/results/pebs/$(date +%Y%m%d-%H%M%S)-memcached-sampling-cost"
```

默认运行两个阶段：

- `resident`：800k × 1 KiB 对象，memory.high=2048 MiB。检查实际 RDMA 流量，以确认测量阶段没有换页；出现流量时汇总会标记，不能当成纯驻留场景。
- `pressure`：相同对象集，memory.high=640 MiB，测量换页压力下的采样净成本。memory.max 为 2048 MiB；memory.high 不是硬上限，需要一起检查实际驻留量。

只运行驻留场景可加 `--phases resident`，只运行换页场景可加 `--phases pressure`。所有模式均使用当前系统页分配配置；如手动修改系统配置，应新建输出目录重新测试。

不需要 sudo 的计划预览：

```sh
python3 tools/rdma/pebs/run_memcached_sampling_cost.py \
  --user "$(id -un)" --output /tmp/memcached-cost-plan --dry-run
```

## 固定并发与 CPU 配置

默认服务器 CPU 为 `0-7`，Memcached 8 个线程；客户端 CPU 为 `16-31`，Mutilate 16 个线程。根据 dnet-61 拓扑，这两组是不同插槽上的不同物理核，不共享 SMT 核；Memcached 内存绑定 node 0。可用 `--server-cpus` 和 `--client-cpus` 修改，底层脚本会拒绝物理核重叠。`hermit_pebsd` 默认绑定到与 Memcached 完全相同的 CPU 集合，直接竞争这些核心；Hermit 换页线程仍未隔离到固定保留核。采样线程绑核在装载数据前完成，并在正式测量后再次核验；配置与前后快照保存 PID 和实际亲和性。

所有模式直接使用 `--connections` 指定的同一个连接参数，默认 32，`qps=0`。不扫描并发、不寻找峰值，也不根据 CPU 利用率或吞吐平台决定是否开始正式测量。结果标记为 `fixed-concurrency-no-calibration`。客户端高占用仅作为分析提示；OOM、应用错误、无有效采样等有效性检查仍保留。

`--connections` 现在只接受一个正整数。旧的 `--pressure-selection`、`--reuse-calibration` 参数已移除，旧命令需删除这些参数。固定并发下的对照不自动证明最大吞吐或 CPU 满载。

## 正式测量与分析

每阶段 8 种配置 × 5 次重复，预热 30 秒、测量 120 秒。配置在每次重复内打乱顺序，每轮创建新进程/cgroup，并记录实际页大小分布。默认两阶段只有 80 个正式样本；仅预热与测量时间合计约 200 分钟，另需装载、恢复等时间。

脚本自动输出每阶段 `measurement/SAMPLING-COST.md`、`sampling-cost.csv` 和 `sampling-cost.json`。原始数据和完整 CPU/内存/WR/perf 快照保留在每个 `r*` 目录。

吞吐衰减按同一重复配对计算：

`loss = 100 × (1 − QPS_static / QPS_off-same-pages)`

正值为衰减，负值为吞吐增加。policy/off-same-pages 表示策略净效果，不能叫作纯采样开销；报告另列相对自然页分配 off 的吞吐提升，以及 policy 相对同频 static 的提升。汇总提供中位数和 min–max；5 次重复范围不等于置信区间。还需同时看 read p99、服务端和客户端 CPU 使用率、全系统 CPU µs/op、实际采样率和 RDMA 传输量。驻留场景用于观察没有换页时的成本，pressure 反映采样与换页系统交互后的净效果，不可混为一个百分比。

lost、throttled、perf 控制变化或不同样本的 perf 上限不一致都会写入质量提示；脚本不会为了制造更高开销而自动调高 perf 上限。没有质量提示也不能替代对重复波动、逐核瓶颈和实际页分布的检查。高频出现丢样时，应报告“该限流/丢样条件下的观测开销”，不能外推为无丢样时的成本。

输出目录必须是新目录，不覆盖既有结果。每次底层矩阵结束或可处理的中断都会恢复 Hermit 控制，THP 只读取与核验；SIGKILL/断电无法保证恢复。此流程只安装了测试脚本，没有改内核或启停服务器上的实验。

## 离线校验

```sh
python3 tools/rdma/tests/test_pebs_memcached_thp.py
python3 tools/rdma/tests/test_memcached_sampling_cost.py
```

测试覆盖实际 folio 识别、CPU 绑定及连接限额、固定连接参数与非法参数、直接运行两个阶段且不启动校准、配对吞吐汇总。


## 将采样处理线程与 Memcached 放在同一插槽

默认已让 `hermit_pebsd` 与 Memcached 共享 CPU 0–7。若希望占用 dnet-61 第一个插槽的全部 16 个物理核作为服务端测试资源，使用：

```sh
sudo python3 tools/rdma/pebs/run_memcached_sampling_cost.py \
  --user "$(id -un)" \
  --output "$PWD/tools/rdma/results/pebs/$(date +%Y%m%d-%H%M%S)-memcached-socket0-cost" \
  --server-cpus 0-15 --sampler-cpus 0-15 --client-cpus 16-31
```

这里服务端与采样处理线程共享插槽 0 的同一组物理核，客户端使用插槽 1 的物理核；未把 SMT 兄弟线程算成额外物理核。脚本检查服务端与采样线程同插槽，且不与客户端共享物理核。若想比较同插槽专核处理与共享核心的区别，可另外测试 `--server-cpus 0-7 --sampler-cpus 8 --client-cpus 16-31`；这两种 CPU 资源分配不能混为同一组结果。

**硬件采样范围仍然是全机。** 当前 `hermit_pebs.c` 在所有在线 CPU 上创建 per-CPU perf 事件；绑 `hermit_pebsd` 只限制 ring 消费和区域历史处理线程，不改变 PEBS 事件及 PMI 的 CPU 范围。客户端产生的事件也可能进入采样处理，尽管区域跟踪只记录被启用的 mm（脚本通过 syscall 473(pid) 启用目标进程）。因此这里测量的是“全局 PEBS＋处理线程与服务端竞争 CPU”的系统开销，不能称为“只采样 Memcached 所在插槽的开销”。若要求硬件事件也仅在插槽 0 创建，需要增加内核采样 cpumask 接口并重新编译、部署；本次未改内核。


## 高并发连接的进程限额

脚本会将本次测试进程及其子进程的 `RLIMIT_NOFILE` 提高到至少 65536，并显式设置 Memcached `-c 32768`；连接规模更大时按客户端线程数与连接参数增加限额。不会修改系统全局或登录用户的永久 limits 配置。实际值记录在 manifest 的 `connection_limits`，服务端 `/proc/PID/limits` 也随快照保存。

2026-10-11 首轮预热因客户端 `Too many open files` 退出：原软限制为 1024，且服务端默认连接上限也为 1024。该轮未产生正式数据，恢复记录无错误。修复限额后应使用新输出目录重新执行原命令；旧目录保留用于失败诊断。


## 只继续 pressure

2026-10-12 起取消峰值检查。已完成的 resident 数据保留；仅继续换页阶段可执行：

```sh
cd ~/hermit-6
sudo python3 tools/rdma/pebs/run_memcached_sampling_cost.py \
  --user "$(id -un)" \
  --output "$PWD/tools/rdma/results/pebs/$(date +%Y%m%d-%H%M%S)-pressure-fixed-load" \
  --server-cpus 0-7 --sampler-cpus 0-7 --client-cpus 16-31 \
  --connections 32 --phases pressure
```

生成 40 个正式样本，预热和测量合计约 100 分钟，另加装载与页分布记录时间。旧结果目录及其中的校准记录保持不变。

## 测量后 stats 超时

测量后先保存计数器及页大小快照，再读取 Memcached stats。读取最多尝试 3 次，每次总超时 20 秒；各次耗时和错误写入 `stats-collection.json`。这段时间不计入吞吐或 CPU 测量区间。连续失败仍中止并标为不完整，不假定 evictions=0；已完成的 `measure.log` 和快照保留用于排查。成功重试也应结合负载延迟审查，不能据此排除服务端卡顿。
