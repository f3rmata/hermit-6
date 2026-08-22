# XGBoost 大页 RDMA 交换测试

本文档描述如何在 Hermit 6.18 上用 XGBoost 训练负载替代 memcached，
测试 4 KiB–2 MiB 大 folio 对 RDMA 交换传输的收益。

## 1. 设计

测试复用 `tools/rdma/run_anon_swapout_sweep.sh` 的 page-size sweep 框架，
把顺序匿名扫描负载换成 XGBoost 二分类训练：

```text
启动 python worker
  -> 阻塞等待 SIGUSR1
  -> 构造/加载 DMatrix，打印 READY
  -> 脚本降低 cgroup memory.max，触发 Hermit RDMA swap-out
  -> 脚本恢复 memory.max，发送 SIGUSR2
  -> XGBoost 训练读取被换出的 DMatrix，触发 RDMA swap-in
  -> worker 打印 TRAIN sec/metric
```

与 memcached 相比，XGBoost `hist` 训练按列/块顺序扫描训练矩阵，
内存分配连续，THP/mTHP 覆盖率高，更能体现大 folio 传输的收益。

## 2. 文件

| 文件 | 作用 |
| --- | --- |
| `tools/rdma/xgboost_train.py` | 信号驱动的 XGBoost worker |
| `tools/rdma/run_xgboost_page_sweep.sh` | Hermit 大页 page-size sweep |
| `tools/rdma/run_xgboost_native_sweep.sh` | 原生 local swap / 无 swap 基线 |

## 3. 依赖

- 已加载 Hermit RDMA backend（`rswap-client.ko`），`/sys/kernel/debug/hermit/` 可用；
- 单一活动块设备 swap（order>0 传输前提），zswap 已关闭；
- cgroup v2；
- Python 3 + `xgboost`：

```bash
pip install xgboost
```

## 4. Hermit 大页 sweep

```bash
cd tools/rdma

# 默认合成数据（约 16 GiB 训练矩阵），limit = resident * 70%
MODE=cgroup-hermit WORKSET_MB=16384 LOCAL_RATIO_PCT=70 \
  ./run_xgboost_page_sweep.sh

# 使用真实 CSV 数据（例如 HIGGS：第一列为 label，后 28 列为特征）
MODE=cgroup-hermit XGB_DATA_FILE=/data/higgs.csv \
  XGB_DATA_FORMAT=csv LOCAL_RATIO_PCT=70 XGB_ROUNDS=30 \
  ./run_xgboost_page_sweep.sh

# 或使用 libsvm 数据
MODE=cgroup-hermit XGB_DATA_FILE=/data/higgs.train.libsvm \
  XGB_DATA_FORMAT=libsvm LOCAL_RATIO_PCT=70 XGB_ROUNDS=30 \
  ./run_xgboost_page_sweep.sh
```

脚本会自动：

1. 逐 page size（默认 `4 16 32 64 128 256 512 1024 2048` KiB）设置 THP 策略；
2. 写入对应的 `/sys/kernel/debug/hermit/remote_order_mask`；
3. 启动 worker，构造 DMatrix 并确认常驻内存；
4. 降低 `memory.max` 触发 swap-out，等待 `order_stats` stores 安静；
5. 恢复 `memory.max`，运行训练，统计 swap-in；
6. 输出 `tools/rdma/results/<run-id>/xgboost-swapio-summary.csv`。

常用环境变量见脚本 `usage`；`XGB_EXPECTED_METRIC_MIN/MAX` 用于训练指标
合理性校验，例如 HIGGS AUC 可设为 `XGB_EXPECTED_METRIC_MIN=0.75`。

## 5. HIGGS 数据

Kaggle 上的 HIGGS 镜像支持 Range，可用 aria2c 多连接下载：

```bash
mkdir -p data
aria2c -x 16 -s 16 -k 4M --file-allocation=none \
  --dir="$PWD/data" --out=HIGGS.zip \
  'https://www.kaggle.com/api/v1/datasets/download/arpit1bansal/higgs-dataset'

unzip data/HIGGS.zip -d data
# -> data/HIGGS.csv/HIGGS.csv（约 7.5 GiB，第一列为 label）
```

运行 sweep 时把 `XGB_DATA_FILE` 指向解压后的 CSV 即可，worker 会自动
按 CSV 格式读取（`XGB_DATA_FORMAT=csv` 也可显式指定）。

## 6. 原生基线

```bash
cd tools/rdma

# 无限内存的理想本地基线
MODE=local WORKSET_MB=16384 ./run_xgboost_native_sweep.sh

# cgroup + 本地 swap 基线
MODE=cgroup-linux WORKSET_MB=16384 LOCAL_RATIO_PCT=70 \
  ./run_xgboost_native_sweep.sh
```

原生脚本使用同一个 worker 和 THP page-size sweep，但不接触 Hermit
debugfs。结果输出到
`tools/rdma/results/<run-id>/xgboost-native-summary.csv`。

## 7. 指标

Hermit sweep CSV 的关键列：

| 列 | 含义 |
| --- | --- |
| `large_store_pct` | swap-out 中 order>0 大 folio 传输字节占比 |
| `large_load_pct` | swap-in 中 order>0 大 folio 传输字节占比 |
| `target_fallback_delta` | 目标 order 回退 4 KiB 的次数 |
| `target_errors_delta` | 目标 order 后端错误次数，必须为 0 |
| `protocol_gib_per_sec` | 后端 swap-out 协议带宽 |
| `load_protocol_gib_per_sec` | 后端 swap-in 协议带宽 |
| `train_sec` | XGBoost 训练耗时 |
| `train_metric_value` | 训练指标（默认 AUC），用于正确性校验 |

判断标准：

- `large_store_pct`/`large_load_pct` 在 64 KiB/2 MiB mask 下应显著高于 4 KiB；
- `target_errors_delta == 0`，`target_fallback_delta` 在目标 order 禁用或后端不支持时为 0；
- 每个 page size 的 `train_metric_value` 应一致（同一数据集、同一随机种子），
  证明远端换入的数据完整；
- Hermit 大页 mask 下 `train_sec` 应低于 4 KiB mask，`load_protocol_gib_per_sec`
  应更高。

## 8. 正确性校验建议

正式实验建议先在 `MODE=local` 下跑一次同一数据集的训练，记录 AUC/耗时，
然后在 Hermit sweep 中设置：

```bash
XGB_EXPECTED_METRIC_MIN=<local_auc - 0.01> \
XGB_EXPECTED_METRIC_MAX=<local_auc + 0.01>
```

如果换入数据损坏或后端错误，训练指标会漂移或 worker 直接失败。

## 9. 注意事项

- **合成数据默认用随机特征，AUC 接近 0.5**，只适合性能测试和链路验证；
  有说服力的结果请使用 HIGGS 等真实数据集（`XGB_DATA_FILE`）。
- 合成数据构造和 DMatrix 都会占用内存，`WORKSET_MB` 不要超过本地内存的
  一半；脚本按 READY 后的实际 `memory.current` 计算 limit，因此不会因
  XGBoost 内部复制而低估换出量。
- order>0 的远端传输要求 swap 落在块设备上（`/proc/swaps` 中类型为
  `partition`），swapfile 会被拆成 4 KiB。
- 卸载 `rswap_client` 前必须 `swapoff`，Hermit remote-only entry 没有本地副本。
