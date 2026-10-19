# Redis 2 MiB value + 64 KiB chunk 最新测试分析（20260831-001724）

- 数据：`redis-swapio-summary.csv`（9 page sizes × 5 repeats）
- 配置：`REDIS_VALUE_SIZE=2097152`、`REDIS_SCAN_CHUNK=65536`、`active_ratio=100`、`sequential`、`1 instance`
- 说明：`protocol_gib_per_sec`（store 带宽）仍异常，因 Redis swapout 在 1s 内完成，脚本 1s 轮询无法计时；本次分析用 load 侧和应用侧指标。

## 1. 端到端结果

| page | GET/s | useful GiB/s | bench_s | backend loads/GET | large_store_% | large_load_% | read_amp |
|---|---:|---:|---:|---:|---:|---:|---:|
| 4k | 7963 | 0.4860 | 1.029 | 7.81 | 0.0 | 0.0 | 0.96 |
| 16k | 10056 | 0.6138 | 0.815 | 2.26 | 100.0 | 100.0 | 1.11 |
| 32k | 10442 | 0.6373 | 0.785 | 1.36 | 100.0 | 100.0 | 1.36 |
| 64k | 10726 | 0.6547 | 0.764 | 0.91 | 100.0 | 100.0 | 1.82 |
| 128k | 11065 | 0.6753 | 0.740 | 0.46 | 100.0 | 100.0 | 1.80 |
| 256k | 10004 | 0.6106 | 0.819 | 0.45 | 100.0 | 100.0 | 3.44 |
| 512k | 8444 | 0.5154 | 0.970 | 0.45 | 100.0 | 100.0 | 6.39 |
| 1024k | 6290 | 0.3839 | 1.302 | 0.47 | 100.0 | 100.0 | 12.76 |
| 2048k | 8044 | 0.4910 | 1.018 | 7.73 | 0.0 | 0.0 | 0.95 |

## 2. 结论

1. **本次负载成功出现明显先升后降**：GET/s 从 4k 的 7963 升至 128k 的 11065（峰值），之后下降到 1024k 的 6290。
2. **大 folio 形成情况大幅改善**：16k–1024k 的 `large_store_%` 和 `large_load_%` 都达到 100%，说明 Redis value 缓冲区已经能形成 16 KiB–1 MiB 的 mTHP。
3. **峰值在 128 KiB**：64 KiB chunk 在 128 KiB folio 时达到最优权衡；再往上，读放大从 1.80（128k）增大到 12.76（1024k），有效带宽快速下降。
4. **2048k 仍未形成 2 MiB folio**：`large_store_%=0`、`large_load_%=0`，QPS 反弹到 8044。说明 Redis 的 2 MiB value 尚未按 2 MiB 对齐；你修复的 Hermit 2 MiB 路径还没有被 Redis 侧触发。
5. **backend loads/GET 在 128k 后稳定在 ~0.45**：请求数已不是瓶颈，继续增大 folio 只会增加 overfetch。

