# Hermit PEBS 实验手册

本手册对应 2026-10 之后的 mm 作用域实现。内核构建与部署见
[../migration-6.18.md](../migration-6.18.md) 和
[../rdma/dnet58-dnet61-memcached-rdma.md](../rdma/dnet58-dnet61-memcached-rdma.md)。

## 1. 前提

- 内核：`CONFIG_HERMIT=y`、`CONFIG_SWAP=y`、`CONFIG_DEBUG_FS=y`；
  `CONFIG_MEMCG` 可选（只影响主动回收）。
- RDMA 后端：`rswap-client.ko` 已加载并注册；`/sys/module/rswap_client/`
  存在 `wr_stats`。
- swap 设备：需要 order>0 传输时使用块设备或 loop 设备，不能用普通 swapfile。
- THP：由系统配置决定；PEBS 不写 THP sysfs，实验脚本只读取并核验。

## 2. 启用 PEBS

### 2.1 按进程启用（推荐）

```sh
sudo python3 tools/rdma/pebs/enable_pebs_pid.py <pid> <mode>
# mode: -1 保持当前模式，0 static，1 policy
```

等价的 syscall：

```c
syscall(473, pid, mode);   /* hermit_pebs_start */
syscall(474, pid);         /* hermit_pebs_end */
```

采样已在运行时再次调用 473 只启用/更新目标 mm，不会返回 `EBUSY`。
`pid=-1` 只启动或更新全局采样。

### 2.2 全局启用

```sh
echo 1 | sudo tee /sys/kernel/debug/hermit/pebs_all
```

之后任何产生样本的 mm 都会懒分配区域表；适合没有 cgroup、也不方便逐 pid
启用的场景。

### 2.3 与 cgroup 的关系

cgroup 仍可用于：

- `memory.max` / `memory.high`：制造可重复的换页压力；
- `memory.swap.max`：限制 swap；
- 进程隔离和清理。

但它**不是** PEBS 的启用路径，也不再提供 `memory.hermit_pebs`。PEBS 历史按
mm 保存，线程共享；fork 出的新 mm 默认需要重新启用。

## 3. Memcached 采样开销

入口：

```sh
sudo python3 tools/rdma/pebs/run_memcached_sampling_cost.py \
  --user "$(id -un)" \
  --output "$PWD/tools/rdma/results/pebs/$(date +%Y%m%d-%H%M%S)-memcached-sampling-cost"
```

- 固定并发（默认 32 连接），不做峰值/并发校准；不要用结果声称已达最大吞吐。
- 两个阶段：`resident`（`memory.high=2048 MiB`，确认无换页）和 `pressure`
  （`memory.high=640 MiB`，`memory.max=2048 MiB`）。
- 8 种配置 × 5 次重复，预热 30 s、测量 120 s；配置顺序在重复内随机化。
- `off` 不再等价于纯 4 KiB baseline：它保留 `remote_order_mask=0x1`；
  `off-same-pages`、static、policy 使用 `0x3fd`。比较采样开销用
  `static` vs `off-same-pages`。
- 每轮保存 `pages-loaded/`、`pages-before/`、`pages-after/` 的 smaps、maps 和
  pagemap/kpageflags folio 统计；这些是非原子快照，可能受并发回收影响。
- 输出：`measurement/SAMPLING-COST.md`、`sampling-cost.csv/json` 和每轮原始目录。

只跑压力阶段：

```sh
sudo python3 tools/rdma/pebs/run_memcached_sampling_cost.py \
  --user "$(id -un)" --output <new-dir> --phases pressure \
  --connections 32
```

## 4. 多负载矩阵

入口：

```sh
sudo python3 tools/rdma/pebs/run_workload_matrix.py \
  --user "$(id -un)" \
  --output "$PWD/tools/rdma/results/pebs/$(date +%Y%m%d-%H%M%S)-workloads"
```

先用 `--dry-run` 检查计划。默认矩阵：

- 7 种负载配置：anon 1t/8t full、anon 1t/8t chunk64k、Redis、YCSB、XGBoost；
- 9 个页档：4、16、32、64、128、256、512、1024、2048 KiB；
- 7 种配置：off、static-199/1999/19997、policy-199/1999/19997；
- 3 次重复，共 1323 个样本。

每个 case 使用独立进程和 cgroup；cgroup 的 `memory.max` 默认设为 resident 的
70%，用于制造压力。PEBS 通过 `enable_pebs_pid.py` 按工作负载 pid 启用，不写
`memory.hermit_pebs`。输出包含 `case.json`、`before.json`、`after.json`、
`run.log` 和 `results/`。

汇总：

```sh
python3 tools/rdma/pebs/summarize_workload_matrix.py <run-directory>
```

## 5. 页面选择微基准

入口与协议：

```sh
python3 tools/rdma/pebs/run_page_selection_microbench.py --help
cat tools/rdma/pebs/page_selection_microbench.txt
```

它用 `MADV_PAGEOUT` 做受控回收，不依赖 `memory.high` 压力；cgroup 只用于限额
和隔离，PEBS 按 worker pid 启用。默认场景：sparse、density16/64/256/dense、
mixed、switch；case：fixed-4k/64k/512k/2048k、all-off、sample-only、dynamic。
每个 epoch 采集实际 folio、WR 字节、PEBS 计数和访问时间，用于区分“页面选择”
与“传输粒度”。

## 6. 离线校验

```sh
python3 -m pytest tools/rdma/tests -q
```

覆盖 source-level 回归、folio 识别、CPU 绑定、固定连接参数、PEBS mm 作用域、
page-selection 协议和结果汇总。

## 7. 质量检查与常见问题

- **先看基线**：确认 `off` 是纯 4 KiB 还是自然大 folio；不同轮的 `off` 不可直接比较。
- **看实际 folio**：`pages-*/page-sizes.json` 是物理 folio 快照；不要用请求页档
  代替实际分配。
- **看采样质量**：`lost`、`throttled`、`perf_event_max_sample_rate`、
  `sampled/s` 都要检查；高频档的 lost 不能忽略。
- **看客户端瓶颈**：客户端 CPU 接近 100% 时，QPS 差异不能全部归因于内核。
- **看 swap 设备**：swapfile 会把大 folio 拆成 4 KiB；order>0 传输必须是块设备。
- **不要外推**：Memcached 的收益来自特定压力、对象集和基线；多负载矩阵没有
  一致加速。结论必须带负载和对照口径。
