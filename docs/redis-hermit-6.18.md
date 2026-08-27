# Redis 大 value 大页 RDMA 交换测试

本文档描述如何在 Hermit 6.18 上用 Redis 大 value 负载测试大 folio 对
RDMA 交换传输的收益。

## 1. 设计

memcached 使用哈希表 + 小对象，内存碎片化严重，很难形成 THP/mTHP。
Redis 测试改用可配置大小的字符串 value：每个 value 是一块连续匿名内存，THP
`always` 策略下可以被大 folio 覆盖。harness 既可以顺序读取全部 key 作为 dense
基线，也可以确定性抽样一部分 key，并通过多个连接随机 GET，测量真实 Redis 请求
路径中的大页固定开销收益和读放大代价。

```text
启动 redis-server -> 启动 python harness
  -> 等待 SIGUSR1 -> FLUSHALL -> SET N 个 2 MiB value -> READY
  -> 脚本降低 cgroup memory.max，触发 Hermit RDMA swap-out
  -> 脚本恢复 memory.max，发送 SIGUSR2
  -> harness 按比例选择 key，多个连接同步执行顺序或随机 GET
  -> 校验每个 value 的长度和聚合 CRC32
  -> worker 打印 BENCH sec/checksum_errors
```

只有 Redis 服务端放入受限 cgroup；Python harness 留在 cgroup 外并固定到独立
CPU 集，避免客户端 key 列表、线程栈和 GET 返回缓冲区污染 Hermit swap 计数。
swap-out 的主体因此是 Redis 的 value、字典和 allocator 元数据。

## 2. 文件

| 文件 | 作用 |
| --- | --- |
| `tools/rdma/redis_bench.py` | 信号驱动的 Redis 大 value 加载/扫描 harness（纯 stdlib，无需 redis Python 包） |
| `tools/rdma/run_redis_page_sweep.sh` | Hermit 大页 page-size sweep |
| `tools/rdma/run_redis_native_sweep.sh` | 原生 local swap / 无 swap 基线 |

## 3. 依赖

- Hermit RDMA backend、cgroup v2、单一活动块设备 swap、zswap 关闭；
- Python 3（无需 `redis` Python 包）；
- Redis server：

```bash
# 有 sudo 时
sudo apt-get install redis-server

# 无 sudo 时，用户目录源码构建（dnet-61 已按此方式构建）
mkdir -p ~/redis-src && cd ~/redis-src
curl -L -o redis-7.4.1.tar.gz \
  https://github.com/redis/redis/archive/refs/tags/7.4.1.tar.gz
tar xzf redis-7.4.1.tar.gz
cd redis-7.4.1
make -j8 BUILD_TLS=no MALLOC=libc redis-server redis-cli
ln -sfn ~/redis-src/redis-7.4.1 ~/redis
```

脚本按 `REDIS_SERVER_BIN`、`./redis/src/redis-server`、
`~/redis/src/redis-server`、`command -v redis-server` 的顺序查找服务端。

## 4. Hermit 大页 sweep

```bash
cd ~/hermit-6/tools/rdma

MODE=cgroup-hermit REDIS_WORKSET_MB=16384 LOCAL_RATIO_PCT=70 \
  ./run_redis_page_sweep.sh
```

默认配置：

- `REDIS_VALUE_SIZE=2097152`（2 MiB）；
- `REDIS_WORKSET_MB=16384` -> `8192` 个 key；
- `REDIS_PORT=6391`；
- `PAGE_SIZES_KB="4 16 32 64 128 256 512 1024 2048"`；
- 每个 page size 重复 3 次。

稀疏随机访问和多连接测试：

```bash
MODE=cgroup-hermit \
PAGE_SIZES_KB='4 16 32 64 128 256 512 1024' \
REDIS_WORKSET_MB=16384 REDIS_VALUE_SIZE=16384 \
REDIS_ACTIVE_RATIOS='100 25 6.25' \
REDIS_ACCESS_ORDER=random REDIS_ACCESS_SEED=1 REDIS_CLIENTS=8 \
REDIS_CHECKSUM=N \
BENCH_CPUS=8-15 REDIS_SERVER_CPU=0 \
LOCAL_RATIO_PCT=50 BENCH_REPEATS=5 \
./run_redis_page_sweep.sh
```

`REDIS_ACTIVE_RATIOS` 控制每轮实际 GET 的 key 比例；抽样集合和随机顺序由
`REDIS_ACCESS_SEED` 固定。`REDIS_CLIENTS` 创建多个独立 TCP 连接并使用 barrier
同步开始请求，但单个 Redis server 的命令执行仍主要由主线程串行完成；该参数增加
连接和请求队列压力，不应解释成多个 Redis 执行线程。
性能轮建议使用 `REDIS_CHECKSUM=N`，此时仍检查 value 是否存在和长度，但不对每个
返回字节执行 CRC32；另跑一次 `REDIS_CHECKSUM=Y BENCH_REPEATS=1` 做完整正确性复核。

输出：`tools/rdma/results/<run-id>/redis-swapio-summary.csv`。

绘制不同 active ratio 的应用吞吐与读放大：

```bash
RESULT_DIR=tools/rdma/results/<run-id>
python3 tools/rdma/plot_redis_swapio.py \
  "$RESULT_DIR/redis-swapio-summary.csv" \
  --output "$RESULT_DIR/redis-sparse-swapio-by-page.png"
```

## 5. 原生基线

```bash
cd ~/hermit-6/tools/rdma

# 无限内存的理想本地基线
MODE=local REDIS_WORKSET_MB=16384 ./run_redis_native_sweep.sh

# cgroup + 本地 swap 基线
MODE=cgroup-linux REDIS_WORKSET_MB=16384 LOCAL_RATIO_PCT=70 \
  ./run_redis_native_sweep.sh
```

## 6. 指标

Hermit sweep CSV 关键列：

| 列 | 含义 |
| --- | --- |
| `bench_sec` | 本轮选中 key 的并发 GET 端到端耗时 |
| `checksum_errors` | 必须为 0 |
| `large_store_pct` | 大 folio store 字节占比 |
| `large_load_pct` | 大 folio load 字节占比 |
| `protocol_gib_per_sec` | store 协议带宽 |
| `load_protocol_gib_per_sec` | load 协议带宽 |
| `target_stores_delta` / `target_loads_delta` | 目标 order 的 store/load folio 数 |
| `target_errors_delta` | 必须为 0 |
| `get_qps` | 实际完成的 GET/s |
| `useful_gib_per_sec` | Redis 返回给客户端的有效 value 字节吞吐 |
| `remote_bytes_per_useful_byte` | Hermit load 字节 / Redis 返回字节；未校正本地命中 |
| `normalized_read_amplification` | 按本轮换出比例归一化后的远端读放大估计 |
| `backend_loads_per_get` | 每个 GET 触发的 Hermit backend load 数 |

判断标准与 XGBoost 测试一致：`large_store_pct`/`large_load_pct` 随
page size 上升，`checksum_errors=0`，`target_errors_delta=0`。

## 7. 注意事项

- Redis 使用 libc malloc 构建（`MALLOC=libc`），大 value 是连续 mmap
  分配，THP `always` 下可以形成大 folio；jemalloc 构建也可用但 THP 覆盖
  取决于 extent 对齐，实验结果需要看 `smaps_rollup` 的 `AnonHugePages`。
- 默认所有 value 使用同一模板，校验依赖长度 + CRC32 求和；如果希望更
  严格，可改 `REDIS_CHECKSUM=Y`（默认）或让 harness 逐 key 生成不同 value。
- 测试结束后脚本会 `kill` redis-server 和 harness，但不会自动删除
  Redis 内存数据；每次 run 开始时 harness 会执行 `FLUSHALL`。
- 多个客户端连接不会让单个 Redis 实例并行执行 GET。若目标是通过多个独立 Redis
  地址空间压满 Hermit/RDMA，还需要在 sweep 中启动多个 redis-server 实例；当前
  `REDIS_CLIENTS` 只用于真实协议路径上的并发连接和请求排队。
