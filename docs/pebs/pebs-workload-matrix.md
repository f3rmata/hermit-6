
## Memcached 纯 4 KiB 对照

Memcached 使用独立入口 `pebs_perf_matrix.py`。现有脚本支持 `--page-kb 4 --thp always`：此处 `always` 表示由脚本应用所选页大小配置；选 4 KiB 时会将全局 THP 和所有 `hugepages-*kB/enabled` 设置为 `never`，然后启动新的 Memcached。`--cases off` 关闭 PEBS。不能使用 `--thp preserve` 来保证纯 4 KiB，因为它会保留机器现有的大页设置。

以下配置对应 2026-10-06 saturation 的对象数、内存阈值、预热及测量时长，仅改为纯 4 KiB baseline：

```sh
cd ~/hermit-6
sudo python3 tools/rdma/pebs/pebs_perf_matrix.py \
  --user "$(id -un)" \
  --output "$PWD/tools/rdma/results/pebs/$(date +%Y%m%d-%H%M%S)-memcached-4k-off" \
  --page-kb 4 --thp always --cases off \
  --records 800000 --memory-mb 640 --qps 0 \
  --warmup 20 --duration 60 --repeats 5 --require-fixed
```

新版 Memcached 矩阵的 `off` 固定关闭全部 THP/mTHP，并将 RDMA mask 设为 `0x1`，不再继承 `--page-kb` 的大页设置。每轮开始前分别应用配置，故随后 static/policy 会恢复指定的页大小；`--thp preserve` 下非 off 组恢复启动时保存的设置。脚本检查 off 的 AnonHugePages、anon_thp、大 folio 读取及大 WR 字节占比，不满足纯 4 KiB 条件则中止。默认不启动任何其他矩阵。本命令仅为运行说明，尚未获得这组测量结果；与 64 KiB + policy 比较时还需核对实际驻留内存，因为 `memory.high=640 MiB` 不是硬上限。


### 新旧 off 的区分

新版 Memcached 结果的 manifest/config 标记 `off_baseline=base-pages-4k-v2`。历史 2026-10-06 的 off 仍是大页基线，原始数据与既有图表保持原义，不能与新版 off 合并。`off-original-mask` 保留旧有“指定 THP 配置＋启动时 RDMA mask”的语义，并非新版纯 4 KiB off。

新版 `static/off` 同时改变页大小与采样状态，不能再称为纯采样开销；`policy/off` 是整套大页加 PEBS 系统相对纯 4 KiB 系统的净效果；同频 `policy/static` 仍可比较策略的附加影响。若使用旧绘图工具，其 static/off 数值应按此新含义解读，历史报告中的“纯采样开销”标签不适用于新版对照。
