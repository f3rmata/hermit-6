# Redis 大 value 大页 RDMA 交换测试

本文档描述如何在 Hermit 6.18 上用 Redis 大 value 负载测试大 folio 对
RDMA 交换传输的收益。

## 1. 设计

memcached 使用哈希表 + 小对象，内存碎片化严重，很难形成 THP/mTHP。
Redis 测试改用 **大字符串 value（默认 2 MiB）**：每个 value 是一块连续
匿名内存，THP `always` 策略下可以被大 folio 覆盖；GET 扫描时按 key 顺序
整块读回，访问模式与大页 RDMA 传输匹配。

```text
启动 redis-server -> 启动 python harness
  -> 等待 SIGUSR1 -> FLUSHALL -> SET N 个 2 MiB value -> READY
  -> 脚本降低 cgroup memory.max，触发 Hermit RDMA swap-out
  -> 脚本恢复 memory.max，发送 SIGUSR2
  -> harness 顺序 GET 所有 key，校验 value 长度和 CRC32
  -> worker 打印 BENCH sec/checksum_errors
```

Redis 服务端和 Python harness 放入同一个 cgroup，swap-out 的主体是
Redis 的大 value 缓冲区。

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

输出：`tools/rdma/results/<run-id>/redis-swapio-summary.csv`。

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
| `bench_sec` | 顺序 GET 全部 key 的端到端耗时 |
| `checksum_errors` | 必须为 0 |
| `large_store_pct` | 大 folio store 字节占比 |
| `large_load_pct` | 大 folio load 字节占比 |
| `protocol_gib_per_sec` | store 协议带宽 |
| `load_protocol_gib_per_sec` | load 协议带宽 |
| `target_stores_delta` / `target_loads_delta` | 目标 order 的 store/load folio 数 |
| `target_errors_delta` | 必须为 0 |

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
