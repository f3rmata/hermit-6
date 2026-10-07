# Hermit Linux 6.6 第七阶段适配说明：RDMA backend 迁移

## 1. 文档目的

Stage7 将 `remoteswap/client` 的 RDMA 路径从旧 `frontswap_ops` 迁移到
Linux 6.6 Hermit 当前使用的 `hermit_backend_ops`。

本阶段目标：

- `BACKEND=RDMA` 不再依赖已删除的 `<linux/frontswap.h>`。
- swapout 通过 RDMA WRITE 写入远端 rswap-server。
- swapin 通过 RDMA READ 从远端读回。
- direct swapin 支持 async post + `hermit_poll_read()` 收口。
- 保持 6.6 当前写穿语义：RDMA store 成功并 poll 完成后，仍继续写 native
  swap 设备，保证 backend miss 或 RDMA 异常时可以从磁盘副本回退。

本阶段不恢复 5.14 frontswap 独占远端副本模式，也不改变 server 协议。

## 2. 接口映射

5.14 旧路径：

```text
swapout
  -> frontswap_store()
  -> rswap_frontswap_store()
  -> RDMA WRITE

swapin
  -> frontswap_load()
  -> rswap_frontswap_load()
  -> RDMA READ
```

6.6 Stage7 新路径：

```text
rswap-client.ko
  -> rswap_register_backend()
  -> hermit_register_backend(&rswap_hermit_ops)

swapout
  -> page_io.c::hermit_swap_writepage()
  -> hermit_backend_store(entry, page, cpu, false)
  -> rswap_hermit_store()
  -> rswap_rdma_send(..., QP_STORE, async post)
  -> hermit_backend_poll_store(cpu)
  -> native swap write-through

swapin, swapcache/native read path
  -> page_io.c::swap_readpage()
  -> hermit_swap_readpage()
  -> hermit_backend_load(entry, page, cpu, false)
  -> rswap_hermit_load()
  -> rswap_rdma_send(..., QP_LOAD_SYNC, sync)
  -> fallback native swap read on miss/error

swapin, direct bypass swapcache
  -> do_swap_page()
  -> hermit_issue_read()
  -> hermit_backend_load(entry, page, cpu, true)
  -> rswap_rdma_send(..., QP_LOAD_SYNC, async)
  -> hermit_poll_read()
  -> hermit_backend_poll_load(cpu)
```

## 3. 内核侧修改

### 3.1 backend store poll 接口

文件：

- `linux-stable/include/linux/hermit_backend.h`
- `linux-stable/mm/hermit_backend.c`

`struct hermit_backend_ops` 新增可选 store poll 回调：

```c
int (*poll_store)(int cpu);
int (*peek_store)(int cpu);
```

新增导出函数：

```c
int hermit_backend_poll_store(int cpu);
int hermit_backend_peek_store(int cpu);
```

语义：

- DRAM backend：同步完成，`poll_store()` 为 no-op。
- RDMA backend：`poll_store()` drain 对应 CPU 的 `QP_STORE` completion。

### 3.2 page_io.c 写穿 store

文件：

- `linux-stable/mm/page_io.c`

`hermit_swap_writepage()` 中 backend store 成功后会立即：

```text
hermit_backend_poll_store(cpu)
  -> ADC_POLL_STORE
```

然后函数返回 `false`，继续走 Linux 6.6 原生 swap write。

这点和 Stage4 初版 DRAM-only 方案不同：当前实现已经恢复写穿语义，swap
设备上保留完整副本。RDMA valid bitmap miss、远端数据丢失或模块加载前已有
swap entry 都可以回退 native swap read。

### 3.3 direct swapin async

文件：

- `linux-stable/mm/page_io.c`

`hermit_issue_read()` 已改为：

```text
hermit_backend_load(entry, page, cpu, true)
```

RDMA backend 在 `async=true` 时只 post RDMA READ，不等待 completion。后续由：

```text
hermit_poll_read()
  -> hermit_backend_poll_load(cpu)
```

统一 drain completion。RDMA callback 设置 `PageUptodate`，page lock 生命周期
仍由 `page_io.c` / `hermit_poll_read()` 管理。

## 4. RDMA client 修改

### 4.1 移除 frontswap 依赖

文件：

- `remoteswap/client/rswap_rdma.h`
- `remoteswap/client/rswap_rdma_ops.c`

删除的旧依赖：

- `<linux/frontswap.h>`
- `struct frontswap_ops`
- `frontswap_register_ops()`
- `frontswap_ops->load/store`

替换为：

```c
static const struct hermit_backend_ops rswap_hermit_ops = {
	.load = rswap_hermit_load,
	.store = rswap_hermit_store,
	.poll_load = rswap_rdma_poll_load,
	.peek_load = rswap_rdma_peek_load,
	.poll_store = rswap_rdma_poll_store,
	.peek_store = rswap_rdma_peek_store,
};
```

模块初始化顺序：

```text
rswap_cpu_init()
  -> rswap_client_init()
       -> RDMA valid bitmap init
       -> init_rdma_sessions()
       -> rdma_session_connect()
  -> rswap_register_backend()
       -> hermit_register_backend()
```

模块退出顺序：

```text
rswap_cpu_exit()
  -> rswap_unregister_backend()
       -> hermit_unregister_backend()
       -> synchronize_rcu()
  -> rswap_client_exit()
       -> disconnect/free RDMA resource
       -> free valid bitmap/debugfs
```

### 4.2 valid bitmap

文件：

- `remoteswap/client/rswap_rdma_ops.c`

client 侧按远端页数分配 valid bitmap：

```text
remote_pages = rmsize_GB << 30 >> PAGE_SHIFT
```

规则：

- RDMA WRITE completion 成功后置位。
- RDMA READ 前检查 bit。
- bit 未命中返回 `-ENOENT`。
- 内核读路径看到 miss 后回退 native swap read。

该 bitmap 解决两个 correctness 问题：

- 模块加载前已经存在的 swap entry 不能误读远端未初始化页面。
- RDMA backend post/read 失败时不会把远端垃圾数据当成有效 swap 数据。

当前没有 invalidate API，swap slot 复用依赖后续 store 覆盖同一个 offset。

### 4.3 completion/page lock 语义

RDMA callback 现在只负责 RDMA 请求本身：

- 检查 `wc->status`。
- 成功 read：`folio_mark_uptodate(page_folio(page))`。
- 成功 write：置 valid bitmap。
- DMA unmap。
- 记录 status。
- 递减 queue pending counter。
- 同步请求 `complete()`，异步请求释放 request。

callback 不再 `unlock_page()`。原因是 6.6 中 page lock 的 owner 是：

- `swap_readpage()` 同步 fallback 路径；
- `hermit_poll_read()` direct swapin 路径；
- native write-through writeback 路径。

RDMA callback 如果直接 unlock，会和这些 owner 产生 double unlock 或提前释放。

### 4.4 RDMA request 结构

文件：

- `remoteswap/client/rswap_rdma.h`

`struct fs_rdma_req` 新增字段：

```c
struct completion done;
pgoff_t offset;
enum rdma_queue_type type;
int status;
bool sync;
bool dma_mapped;
```

用途：

- `done/status`：同步 load 等待 completion。
- `offset/type`：callback 中更新 valid bitmap 和计数。
- `sync`：区分 callback complete 还是释放 request。
- `dma_mapped`：post 失败或 callback 中安全 DMA unmap。

### 4.5 RDMA send 语义

文件：

- `remoteswap/client/rswap_rdma_ops.c`

新接口：

```c
int rswap_rdma_send(int cpu, pgoff_t offset, struct page *page,
		    enum rdma_queue_type type, bool sync,
		    struct fs_rdma_req **sync_req);
```

检查顺序：

```text
valid bitmap exists
offset < remote_pages
chunk_idx < remote_mem_pool.chunk_num
chunk_state == MAPPED
offset_within_chunk + PAGE_SIZE <= mapped_size
allocate fs_rdma_req
DMA map page
build RDMA READ/WRITE WR
ib_post_send()
```

失败时：

- DMA 已 map 则 unmap。
- request 未 post 则释放。
- `post_errors` 递增。
- 返回错误给 backend，上层回退 native path。

## 5. 可观测性

RDMA backend debugfs 目录：

```text
/sys/kernel/debug/rswap_rdma/stores
/sys/kernel/debug/rswap_rdma/loads
/sys/kernel/debug/rswap_rdma/load_misses
/sys/kernel/debug/rswap_rdma/post_errors
/sys/kernel/debug/rswap_rdma/wc_errors
/sys/kernel/debug/rswap_rdma/poll_loads
/sys/kernel/debug/rswap_rdma/poll_stores
```

预期：

- swap 压力后 `stores > 0`。
- swapin 后 `loads > 0`。
- 正常 RDMA 环境下 `post_errors == 0`、`wc_errors == 0`。
- direct swapin 场景 `poll_loads > 0`。
- 写穿 store 路径 `poll_stores > 0`。

Hermit profiling 继续复用 Stage5 字段：

```text
ADC_RDMA_WRITE_LAT
ADC_RDMA_READ_LAT
ADC_POLL_STORE
ADC_POLL_LOAD
ADC_SWAPOUT
ADC_HERMIT_SWAPOUT
```

注意：当前 `ADC_RDMA_WRITE_LAT` 主要覆盖 store post 时段，completion drain
耗时记录在 `ADC_POLL_STORE`。后续如果恢复跨页面 batch async store，可以再
细化该语义。

## 6. 构建命令

内核和 brd：

```bash
make -C hermit-6/linux-stable -j$(nproc) bzImage modules_prepare drivers/block/brd.ko
```

DRAM 回归：

```bash
make -C hermit-6/remoteswap/client BACKEND=DRAM KDIR=$PWD/hermit-6/linux-stable
```

RDMA client：

```bash
make -C hermit-6/remoteswap/client BACKEND=RDMA KDIR=$PWD/hermit-6/linux-stable
```

如需 OFED：

```bash
make -C hermit-6/remoteswap/client BACKEND=RDMA USE_OFA=1 \
	OFA_DIR=/usr/src/ofa_kernel/default KDIR=$PWD/hermit-6/linux-stable
```

如果本机内核未启用 `CONFIG_INFINIBAND` 且没有 OFED `Module.symvers`，
`rswap_rdma_ops.o` / `rswap_rdma.o` 可以通过 C 编译，但 `modpost` 会报告
RDMA core symbol unresolved。这属于本地构建环境限制，不是 frontswap 迁移
错误。

## 7. RDMA 功能验证

server：

```bash
cd hermit-6/remoteswap/server
./rswap-server <server_ip> <port> <remote_mem_gb> <client_cores>
```

client：

```bash
cd hermit-6/remoteswap/client
sudo insmod ./rswap-client.ko sip=<server_ip> sport=<port> rmsize=<remote_mem_gb>
```

dmesg 预期：

```text
rdma_session_connect, RDMA queue[...] Connect to remote server successfully
rswap_request_for_chunk, Got ... chunks from memory server.
rswap_rdma: Hermit backend registered
```

压力测试后检查：

```bash
grep -E 'pswpin|pswpout' /proc/vmstat
cat /sys/kernel/debug/rswap_rdma/stores
cat /sys/kernel/debug/rswap_rdma/loads
cat /sys/kernel/debug/rswap_rdma/wc_errors
```

验收标准：

- `pswpout` 和 `pswpin` 增长。
- `stores > 0`。
- `loads > 0`。
- `wc_errors == 0`。
- `lazy_poll=N` 和 `lazy_poll=Y` 都能完成 direct swapin。
- `bypass_swapcache=N` 时 native swapcache read path 也能从 RDMA backend 读回；
  backend miss 时能回退 native swap 副本。

卸载前建议：

```bash
sudo swapoff -a
sudo rmmod rswap_client
```

当前仍不支持 active swap entry 存在时安全卸载。

## 8. 已知限制

- 保留写穿 swapout，不恢复 5.14 frontswap-only 远端副本模式。
- RDMA server 协议未改动，仍使用现有 chunk/rkey/remote_addr 下发流程。
- valid bitmap 暂无 invalidate API。
- store 当前 post 后立即 poll，保证 page 被 native write-through 解锁前 RDMA
  WRITE 已完成；真正跨页面 async batch store 留到后续阶段。
- 本地无 RDMA/OFED 环境时，只能验证 C 编译和 DRAM 回归，不能完成 RDMA
  module link/load。
