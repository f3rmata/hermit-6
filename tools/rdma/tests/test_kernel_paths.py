#!/usr/bin/env python3
"""Compile and exercise production kernel/client paths with local C harnesses."""
import os
from pathlib import Path
import subprocess
import tempfile


def test_rdma_submission():
    ROOT = Path(__file__).resolve().parents[3]
    source = (ROOT / 'remoteswap/client/rswap_rdma_ops.c').read_text()
    start = source.index('static int rswap_submit_folio(')
    end = source.index('\nstatic int rswap_retry_base(', start)
    function = source[start:end]
    harness = r'''
    #include <assert.h>
    #include <stdbool.h>
    #include <stddef.h>
    #include <stdio.h>
    #include <errno.h>
    #define PAGE_SIZE 4096UL
    struct page { unsigned int index; };
    struct folio { struct page pages[512]; unsigned int nr_pages; };
    struct hermit_io {
        struct folio *folio;
        unsigned int transfer_order, folio_order;
        unsigned long entry;
        int cpu;
    };
    struct rswap_io_context { int pending, status; };
    enum rdma_queue_type { QP_STORE, QP_LOAD_SYNC };
    static unsigned int max_order = 9, calls, drained, fail_at;
    static unsigned int expected_pages;
    static size_t expected_len;
    static bool in_pool;
    static unsigned int folio_nr_pages(struct folio *f) { return f->nr_pages; }
    static struct page *folio_page(struct folio *f, unsigned int i) { return &f->pages[i]; }
    static unsigned long swp_offset(unsigned long entry) { return entry; }
    static bool rswap_extent_in_pool(unsigned long off, unsigned int n)
    { assert(off == 1024); assert(n == expected_pages); return in_pool; }
    static int atomic_read_acquire(int *p) { return *p; }
    static void atomic_cmpxchg(int *p, int old, int val) { if (*p == old) *p = val; }
    static int rswap_io_poll(struct rswap_io_context *ctx, bool wait)
    { assert(wait); assert(ctx->pending); ctx->pending = 0; drained++; return 0; }
    static int rswap_rdma_send(struct rswap_io_context *ctx, int cpu,
        unsigned long offset, struct page *page, size_t len, enum rdma_queue_type type)
    {
        assert(cpu == 0);
        assert(type == QP_STORE || type == QP_LOAD_SYNC);
        assert(len == expected_len);
        assert(page->index == calls * (len / PAGE_SIZE));
        assert(offset == 1024 + page->index);
        assert(page->index + len / PAGE_SIZE <= expected_pages);
        calls++;
        if (fail_at && calls == fail_at) return -EIO;
        ctx->pending++;
        return 0;
    }
    '''
    tests = r'''
    static void run(unsigned int order, unsigned int transfer, bool base,
                    enum rdma_queue_type type, unsigned int failure)
    {
        struct folio folio = { .nr_pages = 1U << order };
        struct hermit_io io = { .folio = &folio, .folio_order = order,
            .transfer_order = transfer, .entry = 1024, .cpu = 0 };
        struct rswap_io_context ctx = {0};
        for (unsigned int i = 0; i < 512; i++) folio.pages[i].index = i;
        calls = drained = 0; fail_at = failure; in_pool = true;
        expected_pages = folio.nr_pages;
        expected_len = PAGE_SIZE << (base ? 0 : transfer);
        int ret = rswap_submit_folio(&io, &ctx, type, base);
        if (transfer > order || transfer > max_order || transfer == 1) {
            assert(ret == -EINVAL && calls == 0);
        } else if (failure) {
            assert(ret == -EIO && calls == failure && ctx.status == -EIO);
            assert(ctx.pending == 0 && drained == (failure > 1));
        } else {
            assert(ret == 0);
            assert(calls == (PAGE_SIZE * expected_pages) / expected_len);
        }
        calls = 0; in_pool = false;
        assert(rswap_submit_folio(&io, &ctx, type, base) == -ERANGE);
        assert(calls == 0);
    }
    int main(void)
    {
        for (unsigned int type = QP_STORE; type <= QP_LOAD_SYNC; type++) {
            run(9, 9, false, type, 0);   /* one 2 MiB WR */
            run(9, 4, false, type, 0);   /* 32 x 64 KiB */
            run(9, 0, false, type, 0);   /* 512 x 4 KiB */
            run(9, 4, true, type, 0);    /* whole-folio base-page retry */
            run(0, 0, false, type, 0);
            run(9, 4, false, type, 1);   /* no outstanding work */
            run(9, 4, false, type, 3);   /* drain two posted segments */
            run(9, 1, false, type, 0);
            run(4, 9, false, type, 0);
            max_order = 4;
            run(9, 9, false, type, 0);
            max_order = 9;
        }
        puts("RDMA submission tests: PASS");
    }
    '''
    with tempfile.TemporaryDirectory(prefix='hermit-rdma-test-') as tmp:
        cfile = Path(tmp) / 'test.c'
        binary = Path(tmp) / 'test'
        cfile.write_text(harness + function + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra',
                        '-Werror', '-fsanitize=address,undefined', '-g',
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def test_remote_read():
    ROOT = Path(__file__).resolve().parents[3]
    source = (ROOT / 'linux-stable/mm/page_io.c').read_text()
    start = source.index('static bool hermit_swap_read_folio(')
    end = source.index('\n/*', start)
    function = source[start:end]
    harness = r'''
    #include <assert.h>
    #include <stdbool.h>
    #include <stdint.h>
    #include <errno.h>
    #include <stdio.h>
    typedef uint64_t u64;
    struct folio { unsigned long swap; unsigned int order; };
    struct hermit_io {
        unsigned long entry;
        struct folio *folio;
        unsigned int folio_order, transfer_order;
        int cpu;
        bool fallback;
    };
    static bool remote, ready;
    static int load_result, completions, loads, result, accounts;
    static unsigned int folio_order(struct folio *f) { return f->order; }
    static bool hermit_backend_range_remote(unsigned long entry, unsigned int order)
    { assert(entry == 42 && order == 4); return remote; }
    static bool hermit_backend_ready(void) { return ready; }
    static void hermit_backend_account(unsigned int order, bool store, bool fallback, int err)
    { assert(order == 4 && !store && !fallback); accounts++; result = err; }
    static u64 ktime_get_mono_fast_ns(void) { return 10; }
    static void hermit_swap_read_complete(struct folio *f, u64 start, int err)
    { assert(f->swap == 42 && start == 10); completions++; result = err; }
    static int get_cpu(void) { return 0; }
    static void put_cpu(void) {}
    static unsigned int hermit_backend_transfer_order(unsigned int order) { return order; }
    static int hermit_backend_load(struct hermit_io *io, bool async)
    { assert(io->entry == 42 && io->transfer_order == 4 && !async); loads++; return load_result; }
    '''
    tests = r'''
    int main(void)
    {
        struct folio folio = { .swap = 42, .order = 4 };
        remote = false; ready = false;
        assert(!hermit_swap_read_folio(&folio));
        assert(!completions && !loads && !accounts);
        remote = true;
        /* Returning false here would authorize a stale local-device read. */
        assert(hermit_swap_read_folio(&folio));
        assert(result == -EIO && completions == 1 && accounts == 1 && loads == 0);
        ready = true; load_result = 0;
        assert(hermit_swap_read_folio(&folio));
        assert(result == 0 && completions == 2 && accounts == 2 && loads == 1);
        load_result = -EIO;
        assert(hermit_swap_read_folio(&folio));
        assert(result == -EIO && completions == 3 && accounts == 3 && loads == 2);
        puts("Remote read fail-closed tests: PASS");
    }
    '''
    with tempfile.TemporaryDirectory(prefix='hermit-read-test-') as tmp:
        cfile = Path(tmp) / 'test.c'
        binary = Path(tmp) / 'test'
        cfile.write_text(harness + function + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra',
                        '-Werror', '-fsanitize=address,undefined', '-g',
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def test_folio_selection():
    """Production allocation loop: legal candidates, failed allocation/charge."""
    root = Path(__file__).resolve().parents[3]
    source = (root / 'linux-stable/mm/memory.c').read_text()
    start = source.index('static inline unsigned long thp_swap_suitable_orders(')
    end = source.index('#else /* !CONFIG_TRANSPARENT_HUGEPAGE */', start)
    functions = source[start:end]
    page_io = (root / 'linux-stable/mm/page_io.c').read_text()
    start = page_io.index('int hermit_swapout_order(')
    end = page_io.index('static bool hermit_swap_write_folio', start)
    functions += page_io[start:end]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
#define CONFIG_HERMIT 1
#define PMD_ORDER 9
#define PAGE_SHIFT 12
#define PAGE_SIZE 4096UL
#define PMD_MASK (~((1UL << 21) - 1))
#define BIT(n) (1UL << (n))
#define ALIGN_DOWN(x, a) ((x) & ~((a) - 1))
#define unlikely(x) (x)
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define TVA_PAGEFAULT 0
#define MTHP_STAT_SWPIN_FALLBACK_CHARGE 0
#define MTHP_STAT_SWPIN_FALLBACK 1
typedef unsigned long pgoff_t;
typedef unsigned long swp_entry_t;
typedef unsigned int gfp_t;
typedef int pte_t;
typedef int spinlock_t;
struct mm_struct {};
struct vm_area_struct { struct mm_struct *vm_mm; unsigned long vm_flags; };
struct vm_fault {
    struct vm_area_struct *vma;
    unsigned long address, hermit_fault_pc;
    unsigned int hermit_policy_order;
    bool hermit_policy_used;
    pte_t orig_pte;
    void *pmd;
};
struct folio { unsigned int order; };
static unsigned long wr_mask, enabled_orders, alloc_fail, charge_fail;
static unsigned int extent, valid_order, allocs, frees, selects;
static bool uffd, zswap, remote, map_fail;
static int requested;
static pte_t ptes[512];
static struct folio result;
static unsigned int folio_order(struct folio *f) { return f->order; }
static int fls_long(unsigned long x) { return x ? 64 - __builtin_clzl(x) : 0; }
static int highest_order(unsigned long x) { return fls_long(x) - 1; }
static int next_order(unsigned long *x, int order)
{ *x &= ~BIT(order); return highest_order(*x); }
static bool userfaultfd_armed(struct vm_area_struct *vma) { (void)vma; return uffd; }
static bool zswap_never_enabled(void) { return !zswap; }
static swp_entry_t pte_to_swp_entry(pte_t pte) { return pte; }
static pgoff_t swp_offset(swp_entry_t entry) { return entry; }
static unsigned int hermit_backend_entry_order(swp_entry_t entry) { (void)entry; return extent; }
static unsigned long hermit_backend_effective_order_mask(void) { return wr_mask; }
static bool hermit_backend_entry_remote(swp_entry_t entry) { (void)entry; return remote; }
static unsigned long thp_vma_allowable_orders(struct vm_area_struct *vma,
    unsigned long flags, int kind, unsigned long mask)
{ (void)vma; (void)flags; (void)kind; return mask & enabled_orders; }
static unsigned long thp_vma_suitable_orders(struct vm_area_struct *vma,
    unsigned long addr, unsigned long orders)
{ (void)vma; (void)addr; return orders; }
static pte_t *pte_offset_map_lock(struct mm_struct *mm, void *pmd,
    unsigned long addr, spinlock_t **ptl)
{
    static spinlock_t lock;
    (void)mm; (void)pmd; (void)addr; *ptl = &lock;
    return map_fail ? NULL : ptes;
}
static unsigned int pte_index(unsigned long addr) { return (addr >> 12) & 511; }
static bool can_swapin_thp(struct vm_fault *vmf, pte_t *pte, unsigned int nr)
{
    assert(pte >= ptes && pte + nr <= ptes + 512);
    assert(!((pte - ptes) % nr));
    (void)vmf; return nr <= (1U << valid_order);
}
static void pte_unmap_unlock(pte_t *pte, spinlock_t *ptl) { (void)pte; (void)ptl; }
static int hermit_swapin_order(struct mm_struct *mm, unsigned long addr,
    unsigned long pc, unsigned long candidates)
{
    (void)mm; (void)addr; assert(pc == 0x1000); selects++;
    assert(!(candidates & ~((BIT(valid_order + 1) - 1))));
    if (requested < 0) return -1;
    return highest_order((candidates & (BIT(requested + 1) - 1)) | BIT(0));
}
static gfp_t vma_thp_gfp_mask(struct vm_area_struct *vma) { (void)vma; return 0; }
static struct folio *vma_alloc_folio(gfp_t gfp, unsigned int order,
    struct vm_area_struct *vma, unsigned long addr)
{
    (void)gfp; (void)vma; assert(!(addr % (PAGE_SIZE << order)));
    allocs++; result.order = order; return alloc_fail & BIT(order) ? NULL : &result;
}
static int mem_cgroup_swapin_charge_folio(struct folio *f, struct mm_struct *mm,
    gfp_t gfp, swp_entry_t entry)
{ (void)mm; (void)gfp; (void)entry; return !!(charge_fail & BIT(f->order)); }
static void folio_put(struct folio *folio) { assert(folio == &result); frees++; }
static void count_mthp_stat(unsigned int order, int event) { (void)order; (void)event; }
static struct folio *__alloc_swap_folio(struct vm_fault *vmf)
{ (void)vmf; result.order = 0; return &result; }
'''
    tests = r'''
static void check(unsigned int expected, unsigned int wanted)
{
    struct mm_struct mm;
    struct vm_area_struct vma = { .vm_mm = &mm };
    struct vm_fault vmf = { .vma = &vma, .address = 0x2000000 + 7 * PAGE_SIZE,
        .orig_pte = 1024 + 7, .hermit_fault_pc = 0x1000 };
    struct folio *f = alloc_swap_folio(&vmf);
    assert(f && f->order == expected);
    if (vmf.hermit_policy_used) assert(vmf.hermit_policy_order == wanted);
}
int main(void)
{
    wr_mask = 0x3fd; enabled_orders = 0x3fc;
    extent = valid_order = 9; remote = true; requested = 9;
    check(9, 9);
    requested = 4; check(4, 4);
    requested = 0; check(0, 0);
    requested = 9; alloc_fail = BIT(9) | BIT(8) | BIT(7);
    check(6, 9);
    alloc_fail = 0; charge_fail = BIT(9); check(8, 9); assert(frees == 1);
    alloc_fail = ~0UL; check(0, 9);
    alloc_fail = charge_fail = 0;
    valid_order = 4; check(4, 4);
    enabled_orders = BIT(9) | BIT(2); check(2, 2);
    enabled_orders = 0; check(0, 0);
    enabled_orders = 0x3fc; valid_order = 9;
    requested = -1; check(9, 0);
    remote = false; extent = 0; check(8, 0);
    remote = true; extent = 9; requested = 9;
    unsigned int old = selects;
    uffd = true; check(0, 0); uffd = false;
    zswap = true; check(0, 0); zswap = false;
    map_fail = true; check(0, 0); map_fail = false;
    assert(selects == old);
    struct folio f = { .order = 9 };
    wr_mask = BIT(0) | BIT(4);
    assert(hermit_swapout_order(&f) == 4 && f.order == 9);
    wr_mask |= BIT(9); assert(hermit_swapout_order(&f) == 9);
    f.order = 0; assert(hermit_swapout_order(&f) == 0);
    f.order = 4; wr_mask = BIT(9); assert(hermit_swapout_order(&f) == 0);
    puts("Swap-in allocation and max-granularity swap-out tests: PASS");
}
'''
    with tempfile.TemporaryDirectory(prefix='hermit-allocation-test-') as tmp:
        cfile, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        cfile.write_text(harness + functions + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra',
                        '-Werror', '-fsanitize=address,undefined', '-g',
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    test_rdma_submission()
    test_remote_read()
    test_folio_selection()
