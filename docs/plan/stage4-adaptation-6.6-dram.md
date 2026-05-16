# Hermit Linux 6.6 第四阶段适配说明：DRAM backend 运行验证

## 1. 文档目的

这份文档记录第四阶段已经在 `hermit/linux-stable` 和
`hermit/remoteswap-6.6` 上完成的 DRAM backend 迁移。

第三阶段只恢复了 6.6 `do_swap_page()` 中的 Hermit backend read 插点。
第四阶段继续向前推进一层：让 `remoteswap-6.6` 的 DRAM mock backend
脱离旧 `frontswap_ops`，改为注册到新的 `hermit_backend_ops`，并让
swapout / swapin 能通过同一个本地 DRAM pool 成对运行。

本阶段目标是先跑通本地 DRAM 模式，验证内存路径正确性；RDMA backend
仍然留到后续阶段迁移。

## 2. 背景问题

Linux 6.6 已经没有 5.14 Hermit 依赖的 `frontswap` 接口。
因此 `remoteswap-6.6/client` 中原来的路径无法直接工作：

```text
swapout
  -> frontswap_store()
  -> rswap_frontswap_store()
  -> rswap_dram_write()

swapin
  -> frontswap_load()
  -> rswap_frontswap_load()
  -> rswap_dram_read()
```

第三阶段新增的 6.6 原生接口是：

```c
struct hermit_backend_ops {
	int (*load)(swp_entry_t entry, struct page *page, int cpu, bool async);
	int (*store)(swp_entry_t entry, struct page *page, int cpu, bool async);
	int (*poll_load)(int cpu);
	int (*peek_load)(int cpu);
};
```

所以第四阶段的核心工作不是恢复 `frontswap`，而是把 DRAM backend 改成：

```text
rswap-client.ko
  -> hermit_register_backend(&rswap_hermit_ops)
  -> hermit_backend_store()
  -> hermit_backend_load()
```

## 3. 内核侧修改

### 3.1 `hermit_backend.h` 外部模块可见性

文件：

- `linux-stable/include/linux/hermit_backend.h`

修改：

```c
#include <linux/swap.h>
```

原因是外部模块实现 `hermit_backend_ops` 时需要完整可见 `swp_entry_t`。
只依赖 `mm_types.h` 在部分编译上下文中不够稳妥。

### 3.2 swapout 接入 backend store

文件：

- `linux-stable/mm/page_io.c`

新增 helper：

```c
static bool hermit_swap_writepage(struct page *page)
```

路径位置：

```text
swap_writepage()
  -> arch_prepare_to_swap()
  -> zswap_store()
  -> __swap_writepage()
       -> hermit_swap_writepage()
            -> hermit_backend_store(entry, page, cpu, false)
```

成功时行为：

- backend 同步复制页面内容；
- 内核侧统计 `PSWPOUT`；
- 递增 `ADC_HERMIT_SWAPOUT`；
- `folio_start_writeback()`；
- `folio_unlock()`；
- `folio_end_writeback()`；
- 不再提交到 bdev/fs swap I/O。

失败时行为：

- 返回原生路径；
- 继续走 `swap_writepage_fs()` / `swap_writepage_bdev_sync()` /
  `swap_writepage_bdev_async()`。

当前只处理 `folio_nr_pages(folio) == 1`。这是为了让第一轮 DRAM backend
验证保持保守，因为 `hermit_backend_ops` 当前是 page 粒度接口，而原生 swap
路径可能处理大 folio。

### 3.3 swapin 接入 backend load

文件：

- `linux-stable/mm/page_io.c`

新增 helper：

```c
static bool hermit_swap_readpage(struct page *page)
```

路径位置：

```text
swap_readpage()
  -> zswap_load()
  -> hermit_swap_readpage()
       -> hermit_backend_load(entry, page, cpu, false)
  -> fallback native swap read
```

成功时行为：

- backend 将页面内容复制到 `page`；
- 内核侧确保 folio uptodate；
- 统计 `PSWPIN`；
- unlock folio；
- 不再提交到 bdev/fs swap read。

失败时行为：

- 返回原生 `swap_readpage()` 后续路径；
- 如果 DRAM backend valid bitmap 未命中，会返回 `-ENOENT`，这会触发原生
  swap read fallback。

### 3.4 与第三阶段 direct swapin 的关系

第三阶段已有的 `do_swap_page()` / `HMT_BPS_SCACHE` 路径保留：

```text
do_swap_page()
  -> hermit_direct_swap_readpage()
       -> hermit_issue_read()
       -> hermit_backend_load()
       -> hermit_poll_read()
```

第四阶段新增的是 `swap_readpage()` 内部的 backend read fallback。
这样即使关闭 `bypass_swapcache`，swapcache/native swapin 路径仍然能通过
Hermit backend 读回数据，避免只验证 direct swapin 这一条路。

## 4. remoteswap-6.6 DRAM backend 修改

### 4.1 移除 DRAM 路径 frontswap 依赖

文件：

- `remoteswap-6.6/client/rswap_client.c`
- `remoteswap-6.6/client/rswap_dram_ops.c`
- `remoteswap-6.6/client/rswap_dram.h`
- `remoteswap-6.6/client/rswap_ops.h`
- `remoteswap-6.6/client/Makefile`

删除/替换的旧概念：

- `<linux/frontswap.h>`
- `struct frontswap_ops`
- `frontswap_enabled()`
- `frontswap_register_ops()`
- `frontswap_ops->load/store`
- `rswap_register_frontswap()`
- `rswap_replace_frontswap()`
- `rswap_deregister_frontswap()`

新的模块初始化顺序：

```text
insmod rswap-client.ko rmsize=N
  -> rswap_client_init()
       -> rswap_init_local_dram()
  -> rswap_register_backend()
       -> hermit_register_backend(&rswap_hermit_ops)
```

新的模块退出顺序：

```text
rmmod rswap-client
  -> rswap_unregister_backend()
       -> hermit_unregister_backend()
       -> synchronize_rcu()
  -> rswap_client_exit()
       -> rswap_remove_local_dram()
```

这个顺序很重要：必须先注销 backend，再释放 DRAM pool，否则内核 RCU 读侧
可能仍然通过 `hermit_backend_load/store()` 访问已释放内存。

`remoteswap-6.6/client/Makefile` 当前默认 `BACKEND ?= DRAM`。显式传入
`BACKEND=RDMA` 仍会进入 RDMA 分支，但 RDMA 分支还没有完成 6.6 backend
迁移。

### 4.2 新 DRAM backend ops

文件：

- `remoteswap-6.6/client/rswap_dram_ops.c`

新增：

```c
static const struct hermit_backend_ops rswap_hermit_ops = {
	.load = rswap_hermit_load,
	.store = rswap_hermit_store,
	.poll_load = rswap_hermit_poll_load,
	.peek_load = rswap_hermit_peek_load,
};
```

接口映射：

```text
old frontswap store(type, offset, page)
  -> new store(swp_entry_t entry, page, cpu, async)
  -> rswap_dram_write(page, swp_offset(entry) << PAGE_SHIFT)

old frontswap load(type, offset, page)
  -> new load(swp_entry_t entry, page, cpu, async)
  -> rswap_dram_read(page, swp_offset(entry) << PAGE_SHIFT)
```

DRAM backend 是同步 memcpy backend，所以当前：

- `async` 参数忽略；
- `cpu` 参数忽略；
- `poll_load()` 直接返回 0；
- `peek_load()` 保持返回 1，避免误导后续 prefetch 策略过早停止。

### 4.3 DRAM pool valid bitmap

文件：

- `remoteswap-6.6/client/rswap_dram.c`

新增状态：

```c
static void *local_dram;
static uint64_t local_mem_size;
static unsigned long local_nr_pages;
static unsigned long *local_dram_valid;
```

store 成功后：

```text
copy page -> local_dram[offset]
set_bit(offset >> PAGE_SHIFT, local_dram_valid)
stores++
```

load 前：

```text
if !test_bit(offset >> PAGE_SHIFT, local_dram_valid)
    load_misses++
    return -ENOENT
```

这个 bitmap 的作用是区分两类 swap entry：

- 已经由 Hermit DRAM backend store 过，可以从 DRAM pool 读；
- 模块加载前或 backend 未接管时由原生 swap device 写过，不能从 DRAM pool
  读，必须回退原生 swap read。

### 4.4 page 状态职责调整

旧 `rswap_dram_write()` 会直接：

```text
set_page_writeback()
unlock_page()
end_page_writeback()
```

旧 `rswap_dram_read()` 会直接：

```text
SetPageUptodate()
unlock_page()
```

第四阶段后，DRAM backend 只负责复制数据和设置 uptodate：

- writeback / unlock 由 `mm/page_io.c` 管；
- read unlock 由 `mm/page_io.c` 或 `hermit_poll_read()` 管；
- `PageSwapCache(page)` 断言被移除，因为 `HMT_BPS_SCACHE` direct swapin
  会传入非 swapcache folio。

这样职责更接近 6.6 原生 MM 路径：backend 是 I/O provider，page 生命周期由
内核 swap 层控制。

## 5. 可观测性

DRAM backend 新增 debugfs 目录：

```text
/sys/kernel/debug/rswap_dram/
  stores
  loads
  load_misses
  errors
```

含义：

- `stores`：成功写入 DRAM pool 的页面数；
- `loads`：成功从 DRAM pool 读回的页面数；
- `load_misses`：valid bitmap 未命中并回退原生 swap read 的次数；
- `errors`：offset 越界或 DRAM pool 未初始化等错误次数。

内核 profiling 方面：

- backend store 成功后递增 `ADC_HERMIT_SWAPOUT`；
- swapin 仍沿用第三阶段的 `ADC_ONDEMAND_SWAPIN`、swap fault breakdown、
  `ADC_PF_HERMIT_BIT` 等统计。

## 6. QEMU DRAM 验证脚本修改

文件：

- `tools/qemu-dram/build-qemu-dram.sh`
- `tools/qemu-dram/validate-qemu-dram.sh`
- `tools/qemu-dram/memhog.c`

### 6.1 路径切到 6.6

旧路径：

```text
linux-5.14-rc5
remoteswap/client
```

新路径：

```text
linux-stable
remoteswap-6.6/client
```

### 6.2 kernel config 自动准备

如果 `linux-stable/.config` 不存在，脚本会先执行：

```bash
make -C hermit/linux-stable x86_64_defconfig
```

然后强制设置：

```text
CONFIG_MEMCG=y
CONFIG_HERMIT=y
CONFIG_SWAP=y
CONFIG_DEBUG_FS=y
CONFIG_BLK_DEV_RAM=m
# CONFIG_ZSWAP is not set
```

注意 `CONFIG_HERMIT` 依赖 `MEMCG && SWAP`，所以必须同时打开
`CONFIG_MEMCG`。

### 6.3 frontswap 统计替换为 rswap_dram 统计

旧脚本读取：

```text
/sys/kernel/debug/frontswap/*
```

新脚本读取：

```text
/sys/kernel/debug/rswap_dram/{stores,loads,load_misses,errors}
/proc/vmstat 中的 pswpin / pswpout
```

输出格式变为：

```text
RSWAP_DRAM_STATS: label=... stores=... loads=... load_misses=... errors=...
SWAP_VMSTAT: label=... pswpin=... pswpout=...
```

### 6.4 Hermit debugfs 开关

当前 6.6 实际存在的控制项来自 `mm/hermit.c`：

```text
bypass_swapcache
batch_swapout
batch_tlb
batch_io
batch_account
vaddr_swapout
speculative_io
speculative_lock
lazy_poll
apt_reclaim
sthd_cnt
reclaim_mode
```

QEMU 脚本不再写旧的 `swap_thread` / `prefetch_thread`。

### 6.4.1 initramfs hotplug 兼容

6.6 配置中可能没有 `CONFIG_UEVENT_HELPER`，此时
`/proc/sys/kernel/hotplug` 不存在。旧 initramfs 脚本在 `set -e` 下直接执行：

```sh
echo /bin/mdev > /proc/sys/kernel/hotplug
```

会导致 PID 1 退出，然后触发：

```text
Kernel panic - not syncing: Attempted to kill init!
```

当前脚本已经改为：

```sh
if [ -w /proc/sys/kernel/hotplug ]; then
    echo /bin/mdev > /proc/sys/kernel/hotplug
else
    echo "INITRAMFS: kernel hotplug helper unavailable; using mdev -s only"
fi
mdev -s
```

因此新内核没有 hotplug helper 时会继续使用 `mdev -s` 扫描设备，不再 panic。

脚本新增环境变量：

```bash
BYPASS_SWAPCACHE=Y|N
LAZY_POLL=Y|N
```

用于验证：

- direct swapin；
- swapcache/native swapin；
- lazy poll 开关。

### 6.5 memhog checksum 校验

`memhog.c` 现在在写入页面时记录 expected checksum，在 reload 读回时重新计算。

成功输出：

```text
MEMHOG_CHECKSUM: status=pass expected=... actual=...
```

失败时 `memhog` 返回非 0，QEMU 验证失败。

## 7. 构建与验证命令

### 7.1 轻量编译验证

已经执行并通过：

```bash
bash -n hermit/tools/qemu-dram/build-qemu-dram.sh
bash -n hermit/tools/qemu-dram/validate-qemu-dram.sh
cc -Wall -Wextra -O2 -c hermit/tools/qemu-dram/memhog.c -o /tmp/hermit_memhog.o
```

内核对象级验证：

```bash
make -C hermit/linux-stable x86_64_defconfig
hermit/linux-stable/scripts/config --file hermit/linux-stable/.config \
  -e MEMCG -e HERMIT -e SWAP -e DEBUG_FS -m BLK_DEV_RAM -d ZSWAP
make -C hermit/linux-stable olddefconfig
make -C hermit/linux-stable -j2 \
  mm/page_io.o mm/hermit_backend.o mm/hermit.o mm/swap_stats.o
```

结果：通过。

DRAM module 编译：

```bash
make -C hermit/linux-stable modules_prepare
make -C hermit/remoteswap-6.6/client \
  BACKEND=DRAM \
  KDIR=$PWD/hermit/linux-stable \
  KBUILD_MODPOST_WARN=1
```

结果：源码编译和 `.ko` 链接通过。

当前因为没有完整内核 `Module.symvers`，外部模块 modpost 会提示普通内核符号
未解析，例如 `_printk`、`vzalloc`、`copy_page`，以及
`hermit_register_backend`。这是 `modules_prepare` 的已知限制；完整构建内核
或使用 `KBUILD_MODPOST_WARN=1` 可以继续生成 `.ko`。

### 7.2 QEMU 验证

默认 direct swapin：

```bash
cd hermit/tools/qemu-dram
./run-qemu-dram.sh
```

lazy poll：

```bash
cd hermit/tools/qemu-dram
LAZY_POLL=Y ./run-qemu-dram.sh
```

关闭 bypass swapcache，验证 swapcache/native swapin 仍能经 backend 读回：

```bash
cd hermit/tools/qemu-dram
BYPASS_SWAPCACHE=N ./run-qemu-dram.sh
```

预期成功信号：

```text
RSWAP_DRAM_STATS: ... stores>0 ... loads>0 ... errors=0
SWAP_VMSTAT: ... pswpin 增长 ... pswpout 增长
MEMHOG_CHECKSUM: status=pass
VALIDATION: PASS
```

## 8. 当前限制与后续工作

### 8.1 当前限制

- 本阶段只实现 DRAM backend，不迁移 RDMA backend。
- backend ops 当前按 order-0 page 实现，大 folio 回退原生 swap I/O。
- 尚未新增 invalidate API；swap slot 复用依赖后续 store 覆盖同一 offset。
- 第一轮不支持在仍有 active swap entry 时安全 `rmmod rswap-client`。
  实机卸载前应先 `swapoff`。
- `rswap_dram` valid bitmap 只能判断“是否由当前 backend store 过”，不表示
  swap entry 生命周期的完整所有权。

### 8.2 后续建议

1. 跑完整 QEMU DRAM 三组验证：
   - `BYPASS_SWAPCACHE=Y LAZY_POLL=N`
   - `BYPASS_SWAPCACHE=Y LAZY_POLL=Y`
   - `BYPASS_SWAPCACHE=N`
2. 如果 QEMU 通过，补一轮长时间 memhog / stress-ng 压力测试。
3. 为 `hermit_backend_ops` 增加 invalidate/store poll 接口，补齐 slot 生命周期。
4. 将 RDMA backend 从 `frontswap_ops` 迁移到同一套 `hermit_backend_ops`。
5. 再考虑恢复 5.14 Hermit 的 prefetch / speculative IO 更完整路径。
