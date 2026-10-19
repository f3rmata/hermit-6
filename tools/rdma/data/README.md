# 输入数据集

本目录不随仓库保存大型数据文件，只保留说明。基准脚本在缺少真实数据时会使用合成数据，结果可用于功能检查，但不适合作为论文级性能结论。

## HIGGS

XGBoost 使用 HIGGS 的 CSV 格式（第一列 label，后 28 列特征）：

```bash
mkdir -p tools/rdma/data
aria2c -x 16 -s 16 -k 4M --file-allocation=none \
  --dir="$PWD/tools/rdma/data" --out=HIGGS.zip \
  'https://www.kaggle.com/api/v1/datasets/download/arpit1bansal/higgs-dataset'
unzip tools/rdma/data/HIGGS.zip -d tools/rdma/data
# -> tools/rdma/data/HIGGS.csv/HIGGS.csv（约 7.5 GiB）
```

运行时把 `XGB_DATA_FILE` 指向解压后的 CSV；worker 默认按 CSV 解析，也可显式设置 `XGB_DATA_FORMAT=csv`。

## 其他负载

- 数组、Memcached、Redis、YCSB：脚本生成工作集，不需要外部数据。
- 大型结果和压缩包由 `.gitignore` 忽略；索引与复现命令保留在 [../results/README.md](../results/README.md)。
