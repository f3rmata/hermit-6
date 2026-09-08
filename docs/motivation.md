## Motivation

内存资源正在走向池化/解耦：主机可以通过 RDMA 把冷内存换到远端内存节点，在不增加本地 DRAM 的前提下扩大可用内存。
Hermit通过内存压力触发swap-out，经 RDMA 写到远端；本地再次访问时经RDMA swap-in读回的方式，提高了系统的可用内存和应用的吞吐量。

当前工作回答的核心问题是：RDMA swap 的传输粒度（folio 大小）应该怎么选，收益边界在哪里？

- 4 KiB 小页路径下，每页都要付一次完整的固定开销：DMA map/unmap、RDMA WR post、CQ completion、pending 计数与对象释放。实测 4 KiB 的 swap-in 协议带宽只有 0.6 GiB/s 左右，RDMA 网卡远未喂满。
- 使用 THP/mTHP 大 folio 后，一次 RDMA WR 可以传输 16 KiB–2 MiB，固定开销被摊薄。anon 顺序扫描中 swap-in 协议带宽从 4 KiB 的 0.6 GiB/s 提升到 2 MiB 的约 10–11 GiB/s，应用可见带宽到 36 GiB/s。
- 但大 folio 存在读写放大的权衡：如果应用只访问 folio 里的一小部分（比例 `f`），读放大约为 `1/f`，无用数据传输的资源消耗会反过来影响收益。
  在Memcached随机小对象、16 KiB value 随机 GET 都表现为 QPS 对 folio 大小不敏感；而稀疏 chunk64k 扫描则出现明显倒 U。
- 大页收益不是由页大小本身决定，而是取决于**访问密度与空间局部性**，即根据固定开销节省和无效传输代价决定最佳页面大小。

通过使用同一套页面扫描方法（同时改变 mTHP 分配大小与 Hermit `remote_order_mask`），我们在多种负载上复现了这个权衡：

- 匿名数组扫描：数组全扫 `f≈1`，有效带宽（除以热页比例之后）随粒度单调上升；chunk64k 稀疏访问峰值在 64 KiB，读放大按 `folio/chunk` 增长。
- Memcached / Redis 小 value 随机 GET：`f` 低且热点离散，QPS 基本平坦，不是大页的好场景。
- Redis / YCSB 大 value 顺序或整值读：访问密度高，最佳 folio 移到 64–256 KiB；继续增大 folio 后 overfetch 主导，吞吐下降。
- XGBoost dense 训练：协议带宽随粒度上升，但训练计算密集，应用时间只改善约 1%。

## PEBS

PEBS：
counter overflow （计数器溢出）
-> 触发 PEBS 机制（不中断）
-> 硬件等待下一次目标事件发生
-> 硬件把 PEBS record 写进专用的 PEBS buffer（Debug Store）
-> 硬件自动清溢出状态、重新装载计数器初值
-> 只有 buffer 写满时才产生一次中断，批量刷新到内存

`perf mem` 是 PEBS 内存分析的最常用入口，底层用 PEBS-LL 的 load-latency 事件：

```bash
# 采样 load 与 store，记录其 Data Linear Address / Latency / Data Source
perf mem record -- ./a.out
perf mem report --stdio

# 只采样长延迟 load（例如超过 30 个 cycle 的 load）
perf mem record -e mem_load_retired.l3_miss:pp -- ./a.out
perf mem report
```

`perf mem report` 输出里 `mem_lvl` / `mem_snoop` / `mem_dtlb` 列配合`Data Symbol`（数据地址命中的变量/结构）可以得到数据结构在哪个内存层级被访问，延迟多少

PEBS 的 Data Linear Address（DLA）给出被采样 load 的**线性地址**。按页大小（4 KiB / 64 KiB / 2 MiB）聚合样本：

1. 采样 `mem_load_retired.*`（配合 `ldlat` 阈值过滤长延迟 load），得到`(address, latency, data_source)` 数据；
2. 把地址右移对齐到 page，统计每页的**访问次数**（热度）与**平均延迟**；
3. `data_src` 里的 `Local RAM` vs `Remote RAM` 直接区分本地命中与远端（swap-in 后仍未本地化）的访问；
4. 由此得到一张页面热度 + 远端访问占比的 heatmap。
