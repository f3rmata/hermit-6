# Hermit Linux 6.6 第六阶段适配说明：vaddr/vpage directed reclaim

## 1. 文档目的

Stage6 恢复 5.14 Hermit 中 `vaddr_swapout=Y` 依赖的
vaddr/vpage directed reclaim 安全子集。

本阶段目标不是完整替换 Linux 6.6 rmap，而是在最容易证明正确的匿名
order-0 单映射页上，用页上的 vaddr hint 快速定位唯一 PTE，并恢复以下
profiling 可观测性：

```text
ADC_HERMIT_RMAP1_LAT  direct reference check latency
ADC_HERMIT_RMAP2_LAT  direct unmap latency
```

任何条件不满足或校验失败都会回退到 6.6 原生
`folio_referenced()` / `try_to_unmap()`，vaddr hint 只作为优化线索，不作为
正确性来源。

## 2. 支持范围

Stage6 direct reclaim 只支持同时满足以下条件的 folio：

- `HMT_VADDR_OUT` 已开启，也就是 debugfs 中 `vaddr_swapout=Y`。
- `folio_nr_pages(folio) == 1`，只处理 order-0 page。
- 匿名页：`folio_test_anon(folio)`。
- 非 KSM：`!folio_test_ksm(folio)`。
- 非 hugetlb：`!folio_test_hugetlb(folio)`。
- 单映射：`folio_mapcount(folio) == 1`。
- 当前上下文有 `current->mm`。
- `mmap_read_trylock(current->mm)` 成功。
- `hmt_get_page_vaddr(page)` 非 0。
- `vma_lookup(mm, vaddr)` 能找到覆盖该地址的 VMA。
- `page_vma_mapped_walk()` 能在该地址重新 walk 到当前 page 对应的 locked PTE。

以下场景本阶段全部回退 native reclaim：

- THP / large folio / PMD mapped folio。
- hugetlb。
- KSM。
- file-backed page。
- 多映射匿名页。
- kswapd 或其他没有 `current->mm` 的回收上下文。
- 无法获得 `mmap_read_trylock()`。
- vaddr hint 过期、VMA 不存在、PTE 不存在、PTE 已变成 swap PTE、PFN 不匹配。

## 3. 已修改文件

### 3.1 `include/linux/hermit.h`

`hermit_try_to_unmap()` 从 `void` 改为 `bool`：

```c
bool hermit_try_to_unmap(struct vpage *vpage, struct page *page,
			 enum ttu_flags flags);
```

返回值语义：

- `true`：Hermit direct unmap 已完成，调用方不再走 native unmap。
- `false`：Hermit fast path 不适用或失败，调用方必须回退 native unmap。

`struct vpage` 保留 5.14 字段，但 Stage6 不再信任旧 `pte` 指针。
reference check 和 unmap 前都会重新 walk page table，并在持有 PTE lock 后使用。

### 3.2 `mm/rmap.c`

恢复匿名页 vaddr hint 维护：

```text
page_add_anon_rmap()
  -> order-0 anon 且 mapcount == 1: hmt_set_page_vaddr(page, address)
  -> 否则清 0

folio_add_new_anon_rmap()
  -> order-0 新匿名页: hmt_set_page_vaddr(&folio->page, address)

page_remove_rmap()
  -> order-0 anon unmap 时清 0
```

新增 6.6 版本的 direct unmap wrapper：

```text
hermit_try_to_unmap(vpage, page, flags)
  -> 校验 order-0 / anon / non-KSM / non-hugetlb / mapcount == 1
  -> 拒绝 TTU_RMAP_LOCKED / TTU_SPLIT_HUGE_PMD
  -> 调用 6.6 原生 static try_to_unmap_one(folio, vma, address, flags)
  -> 如果 folio 仍 mapped，则清 hint 并返回 false
```

注意：Stage6 没有复制 5.14 的旧 PTE 指针方案。真正 unmap 时仍由
`try_to_unmap_one()` 内部通过 `page_vma_mapped_walk()` 重新获得 locked PTE，
因此 PTE 生命周期跟 6.6 原生路径一致。

### 3.3 `mm/page_vma_mapped.c`

新增 Hermit vaddr 校验 helper：

```c
bool hermit_addr_vma_walk(struct page_vma_mapped_walk *pvmw, bool force_lock);
bool hermit_addr_vma_walk_nolock(struct page_vma_mapped_walk *pvmw);
```

6.6 安全版本总是返回 locked PTE，调用者必须用
`page_vma_mapped_walk_done()` 释放。`force_lock` 和 `_nolock()` 只保留
5.14 源码兼容含义，当前实现不会返回未加锁 PTE。

### 3.4 `mm/hermit.c`

重写 `hermit_page_referenced()`：

```text
hermit_page_referenced()
  -> 校验 vpage/page/VMA
  -> 校验 order-0 anon non-KSM non-hugetlb mapcount == 1
  -> 必要时 trylock folio
  -> hermit_addr_vma_walk() 重新 walk locked PTE
  -> VM_LOCKED: 标记 vm_flags 并退出
  -> ptep_clear_flush_young_notify()
  -> folio_clear_idle() / folio_test_clear_young()
```

失败时返回 `-1`，由 `vmscan.c` 回退 native `folio_referenced()`。
这和 5.14 的最大区别是：6.6 版本不会使用保存在 `vpage->pte` 中的旧 PTE。

### 3.5 `mm/vmscan.c`

新增 `folio2vpage_locked()`：

```text
folio2vpage_locked()
  -> 检查 HMT_VADDR_OUT 和安全子集
  -> mmap_read_trylock(current->mm)
  -> vma_lookup(mm, hint)
  -> hermit_addr_vma_walk() 校验 PTE/PFN
  -> 在栈上构造临时 struct vpage
```

这里刻意使用栈上 `struct vpage`，不走 `create_vpage()` 的
`GFP_KERNEL` slab 分配，避免在 reclaim 路径中递归触发内存分配。

reference check 路径：

```text
shrink_folio_list()
  -> folio_check_references()
       -> hermit_folio_referenced()
            -> folio2vpage_locked()
            -> hermit_page_referenced()
       -> 失败则 folio_referenced()
```

统计语义：

```text
Hermit fast path 成功: ADC_HERMIT_RMAP1_LAT += elapsed
fallback native:      ADC_RMAP1_LAT        += elapsed
```

direct unmap 路径：

```text
shrink_folio_list()
  -> folio_mapped()
  -> flags = TTU_BATCH_FLUSH
  -> 非 THP split 情况先尝试 hermit_try_to_unmap_folio()
  -> 成功则跳过 try_to_unmap()
  -> 失败则回退 try_to_unmap(folio, flags)
```

统计语义：

```text
Hermit fast path 成功: ADC_HERMIT_RMAP2_LAT += elapsed
fallback native:      ADC_RMAP2_LAT        += elapsed
```

## 4. 内存路径示意

开启 `vaddr_swapout=Y` 后，满足安全子集的匿名页会走：

```text
anon page fault / COW / map
  -> page_add_anon_rmap() / folio_add_new_anon_rmap()
  -> hmt_set_page_vaddr(page, user address)

memcg reclaim / direct reclaim
  -> shrink_folio_list()
  -> folio_check_references()
       -> folio2vpage_locked()
       -> hermit_page_referenced()
       -> ADC_HERMIT_RMAP1_LAT
  -> try_to_unmap position
       -> folio2vpage_locked()
       -> hermit_try_to_unmap()
       -> try_to_unmap_one()
       -> ADC_HERMIT_RMAP2_LAT
  -> writepage / swapout
```

关闭 `vaddr_swapout=N` 或任意校验失败时：

```text
shrink_folio_list()
  -> folio_referenced()
  -> ADC_RMAP1_LAT
  -> try_to_unmap()
  -> ADC_RMAP2_LAT
```

## 5. 与 5.14 的语义差异

5.14 Hermit 的 vpage 路径更激进：

- 会在 vpage 中携带 PTE 指针。
- 会更直接地复用 Hermit 自己的 page/vaddr reclaim 链路。
- 对多种实验路径的支持更宽，但也更依赖当时 5.14 page/rmap 结构。

6.6 Stage6 的选择更保守：

- vaddr hint 只保存地址，不保存可复用 PTE。
- 每次 reference/unmap 前重新 walk PTE。
- 只处理 order-0 单映射匿名页。
- 一旦无法证明 hint 对应当前 folio，就清 hint 并回退 native。
- `current->mm` 不匹配目标 folio 所属 mm 时会自然校验失败并回退。

因此 Stage6 的 Hermit rmap 统计可能低于 5.14，尤其在 kswapd、多进程共享、
THP 或非当前进程 reclaim 场景中。这是预期结果，不表示 swapout 路径失败。

## 6. 验证方法

### 6.1 构建

```bash
make -C hermit-6/linux-stable -j$(nproc) mm/rmap.o mm/page_vma_mapped.o mm/vmscan.o mm/hermit.o
make -C hermit-6/linux-stable -j$(nproc) bzImage modules_prepare drivers/block/brd.ko
make -C hermit-6/remoteswap/client BACKEND=DRAM KDIR=$PWD/hermit-6/linux-stable
```

### 6.2 DRAM 回归

建议至少跑三组：

```bash
BYPASS_SWAPCACHE=Y LAZY_POLL=N
BYPASS_SWAPCACHE=Y LAZY_POLL=Y
BYPASS_SWAPCACHE=N LAZY_POLL=N
```

验收：

- `VALIDATION: PASS`
- checksum pass
- `/sys/kernel/debug/rswap_dram/errors == 0`
- `/sys/kernel/debug/rswap_dram/stores > 0`
- `/sys/kernel/debug/rswap_dram/loads > 0`
- `pswpout` / `pswpin` 增长

### 6.3 vaddr/vpage profiling

开启：

```bash
echo Y > /sys/kernel/debug/hermit/vaddr_swapout
```

触发当前进程 direct reclaim 或 memcg reclaim 后，预期：

```text
ADC_HERMIT_RMAP1_LAT > 0
ADC_HERMIT_RMAP2_LAT > 0
```

关闭：

```bash
echo N > /sys/kernel/debug/hermit/vaddr_swapout
```

再次触发 reclaim，预期：

```text
ADC_HERMIT_RMAP1_LAT == 0 或不再增长
ADC_HERMIT_RMAP2_LAT == 0 或不再增长
ADC_RMAP1_LAT / ADC_RMAP2_LAT 仍正常增长
```

如果 reclaim 主要由 kswapd 执行，或 workload 使用 THP/large folio，
Hermit rmap 统计可能仍为 0。此时应先确认 native `ADC_RMAP1_LAT` /
`ADC_RMAP2_LAT` 是否增长，再判断是否因为安全子集条件没有命中。

## 7. 已知限制

- 没有恢复 RDMA backend；Stage6 只处理 vaddr/vpage reclaim。
- 没有支持 THP、large folio、KSM、hugetlb、file-backed page。
- 没有支持多映射匿名页。
- 没有为 vaddr hint 记录 mm，因此只会尝试 `current->mm`。
- kswapd 场景通常没有 `current->mm`，会回退 native。
- vaddr hint 清理是保守的；一次校验失败可能让该页后续不再命中 fast path，
  直到重新 map 时写入新 hint。
- 写穿 swapout 语义保持不变，backend miss 时仍可从原生 swap 设备读回副本。

