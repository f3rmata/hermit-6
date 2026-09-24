# Conclusion

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


## pebs design

使用PEBS硬件采样测量swapin的内存地址附近（2MiB）的访问密度（偏斜度skewness），并在Hermit的swapin路径上自动选择每次RDMA传输的folio粒度，替代原先的全局变量（remote_order_mask）

### swapout

尽量整个大 folio 换出  
选中大 folio  
→ 分配连续 swap 槽位  
→ 整个 folio 加入 swapcache  
→ 解除映射  
→ 按整个 folio 构造写 BIO/hermit_swap_write_folio()  
  
hermit_swap_write_folio()  
-> prepare remote()  
-> 选择transfer_order // PEBS/debugfs mask  
-> backend_store()  
-> commit_remote()  

### swapin

memory.c
```c
alloc_swap_folio()  // 选择并分配读回的folio (PEBS预测)  
swapcache_prepare() // 并发设置  
发起hermit load  
处理memcg/workingset/lru数据  
poll read完成  
```

### kernel design

我们复用了很多Linux原生的mTHP优化

- folio 连续分配
- 整个folio写出（RDMA）
- mTHP和PTE批量恢复

还有一些kernel原生的优化

- swap readahead: 缺页时提前读取缺页地址附近的页面，但是策略是在正确性约束下尝试较大的folio (我们则是用PEBS实现更精确的调控)
- khugepaged collapse: 后台线程负责把符合条件的小页区域合并成THP，也可以把之前换出的页面先读回再合并

### design analysis

写出与读回的目标并不相同：

- swap-out：一个已确定要回收的 folio，其全部数据都必须保存。仅把它从一个大 WR 改成多个小 WR，不会减少写出的总字节，只会增加请求数。
- swap-in：一次 fault 可以只读原始远端 extent 的一部分。减小读回大小能直接减少无用传输和本地内存占用。

因此，合理的初始设计是：

> 换出时尽量合并传输；读回时让 PEBS 决定围绕 fault 地址应该带回多少相邻数据。

例如原来是 2 MiB folio，可以先整体写出；随后某处缺页，PEBS 判断附近只有 64 KiB 活跃，就只恢复对应的 64 KiB。当前远端 extent 跟踪已允许读取其完整有效的子范围，具备这条路径的基础；性能收益仍需独立实验验证。

如果要通过小粒度 swap-out 减少“误换出热页”，则还需要改变选页或 folio 拆分

4. PEBS 应预测“这次读回来后会用多少”，而不是累计触及多少

倒 U 的转折点意味着：继续扩大读回范围，新增的有用数据已不足以抵消新增成本。

PEBS 应围绕 fault 地址，为每个候选大小估计：

- 一个近期时间窗口内，哪些相邻页会一起被访问；
- 扩大范围后能减少多少后续 fault / RDMA 请求；
- 会额外读回多少冷数据；
- 样本是否足够，能否支持这个判断。

