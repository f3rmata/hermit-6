# 基线脚本

本目录保存 **native local / cgroup-linux** 基线、PEBS 前的 baseline 和多模式对比脚本。当前 Hermit page sweep 入口仍在 `tools/rdma/run_*_sweep.sh`；这里不取代它们。

| 脚本 | 用途 |
|---|---|
| `run_memcached_compare.sh` | 用 `memcached_load.sh`/`memcached_bench.sh` 在 `local`、`cgroup-linux`、`cgroup-hermit` 之间做模式对比 |
| `run_memcached_native_hermit_sweep.sh` | native 与 Hermit 两阶段 page-size sweep；内部调用 `../run_memcached_page_sweep.sh` |
| `run_redis_native_sweep.sh` | Redis 的 `local` / `cgroup-linux` 基线 |
| `run_xgboost_native_sweep.sh` | XGBoost 的 `local` / `cgroup-linux` 基线 |
| `run_prepebs_baseline.sh`、`continue_prepebs_memcached.sh`、`run_prepebs_memcached_tuned.sh` | 2026-09-24 PEBS 前 baseline 与续跑 |
| `verify_prepebs_restore.sh`、`plot_prepebs_baseline.py`、`summarize_prepebs_baseline.py` | baseline 恢复检查、绘图与汇总 |

所有脚本都从 `tools/rdma` 运行，例如：

```sh
cd tools/rdma
MODE=local ./baseline/run_redis_native_sweep.sh
MODE=cgroup-linux LOCAL_RATIO_PCT=70 ./baseline/run_xgboost_native_sweep.sh
```

脚本内部通过 `../common.sh` 和 `../redis_bench.py` / `../xgboost_train.py` 复用主目录的共享实现。当前 PEBS 策略与固定内存预算协议见 [../../docs/pebs/design.md](../../docs/pebs/design.md) 和 [../../docs/pebs/runbook.md](../../docs/pebs/runbook.md)。
