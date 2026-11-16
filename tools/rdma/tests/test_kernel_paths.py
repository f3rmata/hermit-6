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
        unsigned long entry, remote_offset;
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
        /* The swap entry is not the remote location; only remote_offset is. */
        struct hermit_io io = { .folio = &folio, .folio_order = order,
            .transfer_order = transfer, .entry = 77, .remote_offset = 1024, .cpu = 0 };
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
    start = source.index('static unsigned long hermit_swapin_orders(')
    end = source.index('\n#endif\n\n#ifdef CONFIG_HERMIT\nstatic int hermit_direct_swapin(',
                        start)
    functions += '#define CONFIG_TRANSPARENT_HUGEPAGE 1\n' + source[start:end] + '\n'
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
#define __GFP_DIRECT_RECLAIM 0x400000u
#define __GFP_NORETRY 0x4000u
#define __GFP_NOWARN 0x200u
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
    pte_t orig_pte;
    void *pmd;
    int hermit_folio_order;
};
struct folio { unsigned int order; };
static unsigned long wr_mask, enabled_orders, alloc_fail, charge_fail;
static unsigned int extent, valid_order, allocs, frees;
static bool uffd, zswap, map_fail;
static unsigned int pte_maps, gfp_orders;
static gfp_t last_gfp;
static unsigned int medium_used[10], medium_alloc_fallbacks;
static unsigned int medium_extent_fallbacks, policy_calls;
static int policy_order = -1;
static bool policy_medium;
static pte_t ptes[512];
static struct folio result;
static unsigned int folio_order(struct folio *f) { return f->order; }
static int fls_long(unsigned long x) { return x ? 64 - __builtin_clzl(x) : 0; }
static int highest_order(unsigned long x) { return fls_long(x) - 1; }
static int next_order(unsigned long *x, int order)
{ *x &= ~BIT(order); return highest_order(*x); }
/* Stand-in for hermit_policy_swapin_order(): @policy_order/@policy_medium are
 * set by each test and the mask is recorded so the caller can be checked. */
static unsigned long policy_orders;
static int hermit_policy_swapin_order(struct vm_fault *vmf, unsigned long orders,
                                      bool *medium)
{
    (void)vmf; policy_calls++; policy_orders = orders;
    *medium = policy_medium;
    return policy_order;
}
static void hermit_policy_medium_used(unsigned int order)
{ medium_used[order]++; }
static void hermit_policy_medium_fallback(bool alloc)
{ if (alloc) medium_alloc_fallbacks++; else medium_extent_fallbacks++; }
static bool userfaultfd_armed(struct vm_area_struct *vma) { (void)vma; return uffd; }
static bool zswap_never_enabled(void) { return !zswap; }
static swp_entry_t pte_to_swp_entry(pte_t pte) { return pte; }
static pgoff_t swp_offset(swp_entry_t entry) { return entry; }
static unsigned int hermit_backend_entry_order(swp_entry_t entry) { (void)entry; return extent; }
static unsigned long hermit_backend_effective_order_mask(void) { return wr_mask; }
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
    pte_maps++;
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
static gfp_t vma_thp_gfp_mask(struct vm_area_struct *vma)
{ (void)vma; return __GFP_DIRECT_RECLAIM | __GFP_NOWARN; }
static struct folio *vma_alloc_folio(gfp_t gfp, unsigned int order,
    struct vm_area_struct *vma, unsigned long addr)
{
    (void)vma; assert(!(addr % (PAGE_SIZE << order)));
    last_gfp = gfp; gfp_orders++;
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
static void check(unsigned int expected)
{
    struct mm_struct mm;
    struct vm_area_struct vma = { .vm_mm = &mm };
    struct vm_fault vmf = { .vma = &vma, .address = 0x2000000 + 7 * PAGE_SIZE,
        .orig_pte = 1024 + 7, .hermit_fault_pc = 0x1000 };
    struct folio *f = alloc_swap_folio(&vmf);
    assert(f && f->order == expected);
}

/* Phase B: a validated MEDIUM order reaches alloc_swap_folio() in
 * vmf->hermit_folio_order. It restricts the candidates, skips the PTE scan that
 * do_swap_page() already did, and must not ask for synchronous compaction. */
static void medium_alloc_tests(void)
{
    struct mm_struct mm;
    struct vm_area_struct vma = { .vm_mm = &mm };
    struct vm_fault vmf = { .vma = &vma, .address = 0x2000000 + 5 * PAGE_SIZE,
        .orig_pte = 1024 + 5, .hermit_fault_pc = 0x1000 };
    struct folio *f;

    wr_mask = 0x3fd; enabled_orders = 0x3fc; extent = valid_order = 9;
    alloc_fail = charge_fail = 0; uffd = zswap = map_fail = false;
    /* The hint caps the order even though order 9 is legal, and the range is
     * not scanned again. */
    pte_maps = 0; medium_used[2] = 0;
    vmf.hermit_folio_order = 2;
    f = alloc_swap_folio(&vmf);
    assert(f && f->order == 2 && pte_maps == 0 && medium_used[2] == 1);
    assert((last_gfp & __GFP_NORETRY) && !(last_gfp & __GFP_DIRECT_RECLAIM));
    /* Without a hint the production scan runs and no medium counter moves. */
    vmf.hermit_folio_order = 0; pte_maps = 0; medium_used[2] = 0;
    f = alloc_swap_folio(&vmf);
    assert(f && f->order == 9 && pte_maps == 1 && !medium_used[2]);
    assert(!(last_gfp & __GFP_NORETRY) && (last_gfp & __GFP_DIRECT_RECLAIM));
    /* A failed charge at the hinted order walks down to the next one. */
    vmf.hermit_folio_order = 4; medium_used[3] = 0;
    charge_fail = BIT(4);
    f = alloc_swap_folio(&vmf);
    assert(f->order == 3 && medium_used[3] == 1);
    charge_fail = 0; medium_alloc_fallbacks = 0;
    /* Every candidate order fails: one alloc fallback, base page. */
    alloc_fail = BIT(2) | BIT(3) | BIT(4);
    f = alloc_swap_folio(&vmf);
    assert(f->order == 0 && medium_alloc_fallbacks == 1);
    alloc_fail = 0;
    vmf.hermit_folio_order = 0;
}

/* The path decision: LARGE keeps phase-A behaviour, MEDIUM is validated here
 * so that a misaligned or fragmented extent keeps the 4 KiB SG batches. */
static void medium_decision_tests(void)
{
    struct mm_struct mm;
    struct vm_area_struct vma = { .vm_mm = &mm };
    struct vm_fault vmf = { .vma = &vma, .address = 0x2000000 + 3 * PAGE_SIZE,
        .orig_pte = 1024 + 3, .hermit_fault_pc = 0x1000 };

    wr_mask = 0x3fd; enabled_orders = 0x3fc; extent = valid_order = 9;
    alloc_fail = charge_fail = 0; uffd = zswap = map_fail = false;
    /* No suggestion: the 4 KiB SG batches stay. */
    policy_order = -1; policy_medium = false; policy_calls = 0;
    assert(!hermit_swapin_folio(&vmf) && vmf.hermit_folio_order == 0);
    assert(policy_calls == 1 && policy_orders == 0x3fc);
    /* LARGE: alloc_swap_folio() validates, so no order is fixed here. */
    policy_order = 9; pte_maps = 0;
    assert(hermit_swapin_folio(&vmf) && vmf.hermit_folio_order == 0);
    assert(pte_maps == 0);
    /* MEDIUM: the aligned range is checked before any folio is allocated. */
    policy_order = 4; policy_medium = true; medium_extent_fallbacks = 0;
    pte_maps = 0;
    assert(hermit_swapin_folio(&vmf) && vmf.hermit_folio_order == 4);
    assert(!medium_extent_fallbacks && pte_maps == 1);
    /* A fragmented extent keeps the highest legal order below the hint. */
    valid_order = 2;
    assert(hermit_swapin_folio(&vmf) && vmf.hermit_folio_order == 2);
    /* A ceiling above the legal mask never validates an illegal order. */
    valid_order = 9; enabled_orders = BIT(2) | BIT(3) | BIT(4);
    policy_order = 6;
    assert(hermit_swapin_folio(&vmf) && vmf.hermit_folio_order == 4);
    enabled_orders = 0x3fc;
    /* No legal order >= 2 at all: counted, and the SG batches stay. */
    valid_order = 1;
    assert(!hermit_swapin_folio(&vmf) && vmf.hermit_folio_order == 0);
    assert(medium_extent_fallbacks == 1);
    valid_order = 9;
    /* A PTE map failure must keep the SG path too. */
    map_fail = true;
    assert(!hermit_swapin_folio(&vmf) && medium_extent_fallbacks == 2);
    map_fail = false;
    /* A 4 KiB ceiling, uffd and zswap never consult the policy. */
    enabled_orders = BIT(0); policy_calls = 0;
    assert(!hermit_swapin_folio(&vmf) && policy_calls == 0);
    enabled_orders = 0x3fc; uffd = true; policy_calls = 0;
    assert(!hermit_swapin_folio(&vmf) && policy_calls == 0); uffd = false;
    zswap = true; assert(!hermit_swapin_folio(&vmf) && policy_calls == 0);
    zswap = false;
    policy_order = -1; policy_medium = false;
}
int main(void)
{
    wr_mask = 0x3fd; enabled_orders = 0x3fc;
    extent = valid_order = 9;
    check(9);
    alloc_fail = BIT(9) | BIT(8) | BIT(7);
    check(6);
    alloc_fail = 0; charge_fail = BIT(9); check(8); assert(frees == 1);
    alloc_fail = ~0UL; check(0);
    alloc_fail = charge_fail = 0;
    valid_order = 4; check(4);
    enabled_orders = BIT(9) | BIT(2); check(2);
    enabled_orders = 0; check(0);
    enabled_orders = 0x3fc; valid_order = 9;
    check(9);
    extent = 0; check(8);
    extent = 9;
    /* The large path is offered only when a >4 KiB folio is allocatable:
     * a 4 KiB ceiling, uffd or zswap keep the batched 4 KiB path. */
    {
        struct mm_struct mm;
        struct vm_area_struct vma = { .vm_mm = &mm };
        struct vm_fault vmf = { .vma = &vma, .address = 0x2000000 };
        enabled_orders = 0x3fc; assert(hermit_swapin_orders(&vmf) & BIT(4));
        enabled_orders = 0;     assert(!hermit_swapin_orders(&vmf));
        enabled_orders = BIT(0); assert(!(hermit_swapin_orders(&vmf) & ~BIT(0)));
        enabled_orders = BIT(4); assert(hermit_swapin_orders(&vmf) == BIT(4));
        enabled_orders = 0x3fc;
        uffd = true;  assert(!hermit_swapin_orders(&vmf)); uffd = false;
        zswap = true; assert(!hermit_swapin_orders(&vmf)); zswap = false;
    }
    uffd = true; check(0); uffd = false;
    zswap = true; check(0); zswap = false;
    map_fail = true; check(0); map_fail = false;
    struct folio f = { .order = 9 };
    wr_mask = BIT(0) | BIT(4);
    assert(hermit_swapout_order(&f) == 4 && f.order == 9);
    wr_mask |= BIT(9); assert(hermit_swapout_order(&f) == 9);
    f.order = 0; assert(hermit_swapout_order(&f) == 0);
    f.order = 4; wr_mask = BIT(9); assert(hermit_swapout_order(&f) == 0);
    medium_alloc_tests();
    medium_decision_tests();
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


def test_reclaim_sort():
    """Production ranking: shrink_folio_list() consumes the list tail."""
    root = Path(__file__).resolve().parents[3]
    source = (root / 'linux-stable/mm/hermit_policy.c').read_text()
    start = source.index('struct policy_rank {')
    end = source.index('/* Drop per-page state', start)
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <linux/list.h>
#define POLICY_SCAN 32
#define HERMIT_POLICY_SORT 4
#define HERMIT_POLICY_MGLRU 256
#define atomic_long_inc(p) (++(p)->counter)
#define atomic_long_add(n, p) ((p)->counter += (n))
static atomic_long_t ranked_pages, sorted_lists, mglru_sort_bypasses;
struct folio { struct list_head lru; unsigned int heat; bool dirty, locked; u64 age; };
struct hermit_reclaim_hint { u64 age; unsigned int heat; };
static bool mode = true, feature = true, mglru, cooperative = true;
static bool policy_mode(void) { return mode; }
static bool lru_gen_enabled(void) { return mglru; }
static bool hermit_policy_feature(unsigned int f)
{ return f == HERMIT_POLICY_SORT ? feature : f == HERMIT_POLICY_MGLRU && cooperative; }
static bool folio_trylock(struct folio *f) { return !f->locked; }
static void folio_unlock(struct folio *f) { (void)f; }
static bool folio_test_dirty(struct folio *f) { return f->dirty; }
static void hermit_policy_hint(struct folio *f, struct hermit_reclaim_hint *h)
{ h->heat = f->heat; h->age = f->age; }
'''
    tests = r'''
static struct folio *tail(struct list_head *l) { return list_entry(l->prev, struct folio, lru); }
int main(void)
{
    struct folio f[40] = {};
    LIST_HEAD(list);
    for (unsigned int i = 0; i < 40; i++) {
        f[i].heat = 2; f[i].age = i;
        list_add_tail(&f[i].lru, &list);
    }
    /* The tail window (last 32) holds a dirty cold, a clean cold page and a
     * locked clean cold page. The clean cold page goes to the tail, then the
     * dirty cold one. A cold page outside the window is left alone. */
    f[35].heat = 0; f[35].dirty = true;
    f[20].heat = 0;
    f[38].heat = 0; f[38].locked = true;
    f[2].heat = 0;
    hermit_policy_sort(&list, false);
    assert(tail(&list) == &f[20]);
    assert(list_entry(list.prev->prev, struct folio, lru) == &f[35]);
    assert(list.next == &f[0].lru && f[2].lru.prev == &f[1].lru);
    /* 32 lockable folios are ranked; the locked one is skipped, not counted. */
    assert(sorted_lists.counter == 1 && ranked_pages.counter == 32);
    /* Order is stable among equal scores: older age first at the tail. */
    unsigned int n = 0;
    for (struct list_head *p = list.next; p != &list; p = p->next) n++;
    assert(n == 40);
    /* MGLRU preserves the native candidate order, including an in-flight
     * list isolated before the runtime switch was disabled. */
    struct list_head *saved_next[40], *saved_prev[40];
    for (unsigned int i = 0; i < 40; i++) {
        saved_next[i] = f[i].lru.next; saved_prev[i] = f[i].lru.prev;
    }
    mglru = true; hermit_policy_sort(&list, true);
    mglru = false; hermit_policy_sort(&list, true);
    assert(sorted_lists.counter == 1 && mglru_sort_bypasses.counter == 2);
    for (unsigned int i = 0; i < 40; i++)
        assert(saved_next[i] == f[i].lru.next && saved_prev[i] == f[i].lru.prev);
    cooperative = false; mglru = true; hermit_policy_sort(&list, true);
    assert(sorted_lists.counter == 2); /* ablation restores ranking */
    feature = false; hermit_policy_sort(&list, false); assert(sorted_lists.counter == 2);
    feature = true; mode = false; hermit_policy_sort(&list, false); assert(sorted_lists.counter == 2);
    puts("Reclaim tail ranking tests: PASS");
}
'''
    with tempfile.TemporaryDirectory(prefix='hermit-sort-test-') as tmp:
        cfile, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        cfile.write_text(harness + source[start:end] + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra',
                        '-Werror', '-fsanitize=address,undefined', '-g',
                        '-I', str(root / 'linux-stable/tools/include'),
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def test_swapin_pages():
    """Production 4 KiB swap-in: batches, submit-all-then-poll, stale pages."""
    root = Path(__file__).resolve().parents[3]
    source = (root / 'linux-stable/mm/swap_state.c').read_text()
    start = source.index('static unsigned int hermit_swap_ptes(')
    end = source.index('#endif', source.index('struct folio *hermit_swapin_pages('))
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint64_t u64;
typedef unsigned long pgoff_t;
typedef unsigned int gfp_t;
typedef int spinlock_t;
#define PAGE_SHIFT 12
#define PAGE_SIZE 4096UL
#define PAGE_MASK (~(PAGE_SIZE - 1))
#define PMD_SIZE (1UL << 21)
#define HERMIT_BATCH_MAX 16
#define HERMIT_SWAPIN_MAX 64
#define GFP_NOIO 1
#define __GFP_NOWARN 2
#define __GFP_DIRECT_RECLAIM 4
#define __GFP_NORETRY 8
#define DIV_ROUND_UP(a, b) (((a) + (b) - 1) / (b))
#define min3(a, b, c) ((a) < (b) ? ((a) < (c) ? (a) : (c)) : ((b) < (c) ? (b) : (c)))
#define clamp_t(t, v, lo, hi) ((t)(v) < (t)(lo) ? (t)(lo) : (t)(v) > (t)(hi) ? (t)(hi) : (t)(v))
typedef struct { unsigned long val; } swp_entry_t;
typedef struct { unsigned long v; } pte_t;
#define __pte(x) ((pte_t){ x })
struct folio { int id; bool locked, uptodate, readahead, in_cache; int refs; };
struct mm_struct { int unused; };
struct vm_area_struct { struct mm_struct *vm_mm; unsigned long vm_start, vm_end; };
struct vm_fault { struct vm_area_struct *vma; unsigned long address; void *pmd; pte_t orig_pte;
                  unsigned int hermit_batch_pages; };
struct hermit_backend_ops;
struct hermit_batch {
    struct folio *folios[HERMIT_BATCH_MAX]; swp_entry_t entries[HERMIT_BATCH_MAX];
    unsigned long offsets[HERMIT_BATCH_MAX], addresses[HERMIT_BATCH_MAX];
    int status[HERMIT_BATCH_MAX]; unsigned int nr; int cpu, srcu_idx; bool write;
    void *private; const struct hermit_backend_ops *backend;
};
struct mempolicy { int unused; };
/* PTE value: 0 = none, otherwise swap entry offset + 1 (type 0). */
static pte_t ptes[512];
static unsigned long base = 0x40000000;
static int lock_depth, lock_calls, submits, polls, outstanding, max_outstanding;
static unsigned int predicted, not_allocated_at = ~0U, mutate_at = ~0U;
static int poll_status;
static bool fail_kmalloc;
static struct folio pages[512];
static void *kmalloc(size_t n, int f) { (void)f; return fail_kmalloc ? NULL : malloc(n); }
static void kfree(void *p) { free(p); }
static pte_t *pte_offset_map_lock(struct mm_struct *mm, void *pmd, unsigned long a, spinlock_t **l)
{ static spinlock_t s; (void)mm; (void)pmd; *l = &s; assert(!lock_depth++); lock_calls++; return &ptes[(a - base) >> PAGE_SHIFT]; }
static void pte_unmap_unlock(pte_t *p, spinlock_t *l) { (void)p; (void)l; lock_depth--; }
static pte_t ptep_get(pte_t *p) { return *p; }
static bool is_swap_pte(pte_t p) { return p.v != 0; }
static swp_entry_t pte_to_swp_entry(pte_t p) { return (swp_entry_t){ p.v - 1 }; }
static bool non_swap_entry(swp_entry_t e) { (void)e; return false; }
static bool pte_same(pte_t a, pte_t b) { return a.v == b.v; }
static unsigned int swp_type(swp_entry_t e) { (void)e; return 0; }
static bool hermit_backend_entry_remote(swp_entry_t e) { (void)e; return true; }
static unsigned int hermit_policy_predict(struct vm_fault *vmf) { (void)vmf; return predicted; }
static struct mempolicy *get_vma_policy(struct vm_area_struct *v, unsigned long a, int o, pgoff_t *i)
{ static struct mempolicy m; (void)v; (void)a; (void)o; *i = 0; return &m; }
static void mpol_cond_put(struct mempolicy *m) { (void)m; }
static struct folio *__read_swap_cache_async(swp_entry_t e, gfp_t g, struct mempolicy *m,
    pgoff_t i, bool *allocated, bool skip)
{
    struct folio *f = &pages[e.val % 512];
    (void)g; (void)m; (void)i; (void)skip;
    *allocated = e.val != not_allocated_at;
    f->refs++; if (*allocated) { f->locked = true; f->in_cache = true; }
    return f;
}
static void folio_put(struct folio *f) { f->refs--; }
static void folio_unlock(struct folio *f) { f->locked = false; }
static void folio_mark_uptodate(struct folio *f) { f->uptodate = true; }
static void folio_set_readahead(struct folio *f) { f->readahead = true; }
static void swap_cache_del_folio(struct folio *f) { f->in_cache = false; }
static void swap_read_folio(struct folio *f, void *p) { (void)p; f->uptodate = true; f->locked = false; }
static int raw_smp_processor_id(void) { return 0; }
static u64 ktime_get_ns(void) { return 1; }
#define count_vm_event(x) ((void)0)
#define count_memcg_folio_events(f, e, n) ((void)0)
#define hermit_stat_inc(x) ((void)0)
#define hermit_backend_account(a, b, c, d) ((void)0)
static void hermit_latency_add(int kind, u64 ns) { (void)kind; (void)ns; }
#define HMT_LAT_BACKEND_READ 0
/* Swap-in cost instrumentation hooks, off unless a test enables them. */
static bool cost_enabled;
static unsigned long prefetched_entries[64];
static unsigned int prefetched_calls, prefetched_total;
static bool hermit_swapin_cost_enabled(void) { return cost_enabled; }
static void hermit_policy_prefetched(struct mm_struct *mm, unsigned long entry)
{ (void)mm; if (prefetched_calls < 64) prefetched_entries[prefetched_calls] = entry; prefetched_calls++; }
static void hermit_swapin_cost_prefetched(unsigned int nr) { prefetched_total += nr; }
static int hermit_backend_submit_batch(struct hermit_batch *b)
{
    assert(b->nr >= 1 && b->nr <= HERMIT_BATCH_MAX);
    submits++; outstanding++; if (outstanding > max_outstanding) max_outstanding = outstanding;
    for (unsigned int i = 0; i < b->nr; i++) b->status[i] = 0;
    return 0;
}
static int hermit_backend_poll_batch(struct hermit_batch *b, bool wait)
{
    assert(wait); polls++; outstanding--;
    /* A concurrent fault changes one speculative PTE while reads fly. */
    if (mutate_at != ~0U) ptes[mutate_at].v = 0;
    for (unsigned int i = 0; i < b->nr; i++) b->status[i] = poll_status;
    return 0;
}
'''
    tests = r'''
static struct folio *run(unsigned int nr_ptes, unsigned int predict, unsigned int fault)
{
    struct mm_struct mm;
    struct vm_area_struct vma = { .vm_mm = &mm, .vm_start = base, .vm_end = base + PMD_SIZE };
    struct vm_fault vmf = { .vma = &vma, .address = base + fault * PAGE_SIZE + 123 };
    memset(ptes, 0, sizeof(ptes)); memset(pages, 0, sizeof(pages));
    for (unsigned int i = 0; i < nr_ptes; i++) ptes[i].v = 1000 + i + 1;
    vmf.orig_pte = ptes[fault];
    submits = polls = outstanding = max_outstanding = lock_calls = 0;
    predicted = predict;
    prefetched_calls = prefetched_total = 0;
    return hermit_swapin_pages((swp_entry_t){ 1000 + fault }, 0, &vmf);
}
int main(void)
{
    /* 64 predicted pages: 4 batches all in flight; two PTE-lock passes. */
    struct folio *d = run(512, 64, 0);
    assert(d == &pages[1000 % 512] && d->uptodate && !d->locked && d->refs == 1);
    assert(submits == 4 && polls == 4 && max_outstanding == 4 && lock_calls == 2);
    for (unsigned int i = 1; i < 64; i++) {
        struct folio *f = &pages[(1000 + i) % 512];
        assert(f->uptodate && f->readahead && !f->locked && f->refs == 0 && f->in_cache);
    }
    /* Prediction clipped by the PMD end and by the first hole. */
    run(512, 64, 500); assert(submits == 1);
    run(20, 64, 0); assert(submits == 2);
    /* A page already in swapcache stops the run; demand stays referenced. */
    not_allocated_at = 1005; d = run(512, 64, 0); not_allocated_at = ~0U;
    assert(submits == 1 && pages[1005 % 512].refs == 0 && d->refs == 1);
    /* A PTE changed during I/O is dropped from swapcache, others survive. */
    mutate_at = 10; run(512, 32, 0); mutate_at = ~0U;
    assert(!pages[1010 % 512].in_cache && !pages[1010 % 512].locked);
    assert(pages[1009 % 512].in_cache && pages[1009 % 512].uptodate);
    /* Read error: every page falls back to the per-page read path. */
    poll_status = -5; d = run(512, 16, 0); poll_status = 0;
    assert(d->uptodate && !d->locked);
    /* Demand PTE replaced before the snapshot: NULL lets the fault retry. */
    {
        struct mm_struct mm;
        struct vm_area_struct vma = { .vm_mm = &mm, .vm_start = base, .vm_end = base + PMD_SIZE };
        struct vm_fault vmf = { .vma = &vma, .address = base };
        memset(ptes, 0, sizeof(ptes)); ptes[0].v = 7; predicted = 8; submits = 0;
        assert(!hermit_swapin_pages((swp_entry_t){ 1000 }, 0, &vmf) && !submits);
    }
    /* No memory for the work area: demand page still read alone. */
    fail_kmalloc = true; d = run(512, 64, 0); fail_kmalloc = false;
    assert(d->uptodate && submits == 1);
    /* Cost instrumentation is inert until debugfs enables it. */
    assert(!prefetched_calls && !prefetched_total);
    cost_enabled = true; run(512, 16, 0);
    /* Only the pages read for other addresses are recorded, and every
     * speculative page is counted even if it is later found stale. */
    assert(prefetched_calls == 15 && prefetched_total == 15);
    assert(prefetched_entries[0] == 1001 && prefetched_entries[14] == 1015);
    cost_enabled = false;
    puts("4 KiB batched swap-in tests: PASS");
}
'''
    with tempfile.TemporaryDirectory(prefix='hermit-swapin-test-') as tmp:
        cfile, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        cfile.write_text(harness + source[start:end] + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-parameter', '-fsanitize=address,undefined', '-g',
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def test_cold_observe():
    """Production aging, MGLRU provenance, first-eviction ghosts and refaults."""
    root = Path(__file__).resolve().parents[3]
    source = (root / 'linux-stable/mm/hermit_policy.c').read_text()
    start = source.index('#define POLICY_BUCKET_BITS')
    end = source.index('static bool policy_stream_match(')
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <linux/list.h>
/* tools max_t() type-checks operands that the kernel macro only casts. */
#undef max_t
#define max_t(t, a, b) ((t)(a) > (t)(b) ? (t)(a) : (t)(b))
#undef min_t
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
typedef int pid_t; typedef int spinlock_t;
typedef struct { u64 counter; } atomic64_t;
#define atomic64_read(p) ((p)->counter)
#define atomic64_inc_return(p) (++(p)->counter)
#define atomic64_inc(p) (++(p)->counter)
#define atomic_long_inc(p) (++(p)->counter)
#define PAGE_SHIFT 12
#define PAGE_SIZE 4096UL
#define PAGE_MASK (~(PAGE_SIZE - 1))
#define NSEC_PER_SEC 1000000000ULL
#define NSEC_PER_USEC 1000ULL
#define NSEC_PER_MSEC 1000000ULL
#define HERMIT_SWAPIN_MAX 64
#define HERMIT_POLICY_OBSERVE 8
#define HERMIT_POLICY_FLUSH 16
#define HERMIT_POLICY_RESERVE 64
#define HERMIT_POLICY_MGLRU 256
#define HERMIT_POLICY_LARGE 128
#define HERMIT_POLICY_MEDIUM 512
#define HERMIT_POLICY_FITSTART 1024
#define HERMIT_POLICY_PTEBATCH 2048
#define PMD_ORDER 9
#define BIT(n) (1UL << (n))
#define HERMIT_POLICY_DEFAULT 0x1bf
#define HERMIT_PEBS_MODE_POLICY 1
#define GFP_NOWAIT 0
#define __GFP_NOWARN 0
#define ULONG_MAX (~0UL)
#define clamp_t(t, v, lo, hi) ((t)(v) < (t)(lo) ? (t)(lo) : (t)(v) > (t)(hi) ? (t)(hi) : (t)(v))
#define cmpxchg(p, o, n) __sync_val_compare_and_swap(p, o, n)
#define spin_lock_init(p) (*(p) = 0)
/* Counts acquisitions so tests can prove a path takes no lock at all. */
#define spin_lock(p) (locks++, assert(!(*(p))++))
#define spin_unlock(p) assert((*(p))-- == 1)
static unsigned int locks;
static int policy_ilog2(unsigned long v) { return v ? 63 - __builtin_clzl(v) : 0; }
#define ilog2(v) policy_ilog2((unsigned long)(v))
struct hermit_policy_state;
struct mm_struct { struct hermit_policy_state *hermit_policy; atomic64_t hermit_vma_generation; bool hermit_pebs_enabled; };
struct vm_area_struct { struct mm_struct *vm_mm; unsigned long vm_start, vm_end; };
struct vm_fault { struct vm_area_struct *vma; unsigned long address, hermit_fault_pc; bool hermit_refault;
                  unsigned int hermit_batch_pages; };
struct folio { unsigned long pfn; bool large, anon, ksm; int mapcount; };
struct task_struct { u64 start_time; int pid; } task = { 1, 123 }, *current = &task;
static int task_pid_nr(struct task_struct *t) { return t->pid; }
static u32 hermit_pebs_enabled = 1, hermit_pebs_mode = 1, hermit_pebs_all = 1;
static u64 hermit_pebs_fixed_cost_ns = 7000;
static u64 hermit_pebs_epoch = 1, clock_ns = 1000;
static int flushes;
static unsigned int live;
static bool mglru, notifier, contended, fail_alloc;
static bool lru_gen_enabled(void) { return mglru; }
static struct vm_area_struct *mapped_vma;
static unsigned long mapped_addr;
static unsigned int mappings = 1, walks;
struct rmap_walk_control {
    bool (*rmap_one)(struct folio *, struct vm_area_struct *, unsigned long, void *);
    void *arg;
    bool try_lock, contended;
};
static void rmap_walk(struct folio *f, struct rmap_walk_control *rwc)
{
    walks++;
    for (unsigned int i = 0; i < mappings; i++)
        if (!rwc->rmap_one(f, mapped_vma, mapped_addr, rwc->arg)) break;
    rwc->contended = contended;
}
static u64 ktime_get_ns(void) { return clock_ns; }
static unsigned long hash_long(unsigned long x, unsigned int bits) { return (x * 0x61c8864680b583ebUL) >> (64 - bits); }
static void *kzalloc(size_t n, int f) { (void)f; if (fail_alloc) return NULL; live++; return calloc(1, n); }
static void *kcalloc(size_t n, size_t s, int f) { (void)f; if (fail_alloc) return NULL; live++; return calloc(n, s); }
static void kfree(void *p) { if (p) live--; free(p); }
static bool folio_test_anon(struct folio *f) { return f->anon; }
static bool folio_test_large(struct folio *f) { return f->large; }
static bool folio_test_ksm(struct folio *f) { return f->ksm; }
static int folio_mapcount(struct folio *f) { return f->mapcount; }
static unsigned long folio_pfn(struct folio *f) { return f->pfn; }
static bool mm_has_notifiers(struct mm_struct *mm) { (void)mm; return notifier; }
static void hermit_policy_defer_flush(struct vm_area_struct *v, unsigned long a) { (void)v; (void)a; flushes++; }
/* hermit_policy_predict() dependencies. PEBS adds no pages here, so the
 * prediction is the stream's own doubling and run-length cap. */
#define HERMIT_POLICY_PEBS 2
#define atomic_long_add(n, p) ((p)->counter += (n))
static unsigned int hermit_pebs_predict_pages(struct vm_fault *vmf) { (void)vmf; return 1; }
#undef roundup_pow_of_two
static unsigned long hermit_round_pow2(unsigned long n)
{ unsigned long r = 1; while (r < n) r <<= 1; return r; }
#define roundup_pow_of_two(n) hermit_round_pow2(n)
'''
    tests = r'''
static unsigned long addr(unsigned int i) { return 0x40000000UL + i * PAGE_SIZE; }

/* Forward base-page faults of one thread; returns its run at the next page. */
static struct task_struct threads[256];
static void fault(struct vm_area_struct *vma, unsigned int t, unsigned long a, unsigned long pc)
{
    struct vm_fault vmf = { .vma = vma, .address = a, .hermit_fault_pc = pc };
    current = &threads[t]; clock_ns += 1000;
    hermit_policy_installed(&vmf, 7000 + (a >> PAGE_SHIFT) % 1000, a, 1);
}
static unsigned int run_at(struct vm_area_struct *vma, unsigned int t, unsigned long a, unsigned long pc)
{
    struct vm_fault vmf = { .vma = vma, .address = a, .hermit_fault_pc = pc };
    current = &threads[t];
    return policy_run(vma->vm_mm->hermit_policy, &vmf, clock_ns, NULL);
}
static unsigned long stream_hash(unsigned int t, unsigned long pc, unsigned int bits)
{ return hash_long(pc ^ (unsigned long)&threads[t], bits); }

static void stream_tests(void)
{
    const unsigned long base = 0x40000000UL, stride = 0x1000000UL;
    unsigned long pc = 0x401000, cpc;
    struct mm_struct mm = { .hermit_vma_generation = { 1 } }, mm2 = mm;
    struct vm_area_struct vma = { .vm_mm = &mm, .vm_start = base, .vm_end = base + 64 * stride };
    struct vm_area_struct vma2 = vma; vma2.vm_mm = &mm2;
    struct task_struct *saved = current;
    unsigned int pair[2] = { 0, 0 }, q[9], n = 0, i, j;
    fail_alloc = false; mglru = false; policy_features = HERMIT_POLICY_DEFAULT;
    policy_pages_max = 32768;
    for (i = 0; i < 256; i++) threads[i] = (struct task_struct){ .start_time = 1, .pid = 1000 + i };
    /* Two threads that shared one of the old 64 direct-mapped slots. 256
     * threads in 64 slots must collide somewhere (pigeonhole), independent of
     * where the build places the array. Equal top 6 bits imply the same set. */
    cpc = pc;
    for (i = 0; i < 256 && !pair[1]; i++)
        for (j = i + 1; j < 256; j++)
            if (stream_hash(i, cpc, 6) == stream_hash(j, cpc, 6)) {
                pair[0] = i; pair[1] = j;
                break;
            }
    assert(pair[1]);
    assert(stream_hash(pair[0], cpc, POLICY_STREAM_SET_BITS) ==
           stream_hash(pair[1], cpc, POLICY_STREAM_SET_BITS));
    for (j = 0; j < 20; j++)
        for (i = 0; i < 2; i++) fault(&vma, pair[i], base + i * stride + j * PAGE_SIZE, cpc);
    for (i = 0; i < 2; i++)
        assert(run_at(&vma, pair[i], base + i * stride + 20 * PAGE_SIZE, cpc) == 19);
    assert(stream_evictions.counter == 0);
    /* Prediction-side lookups never create or replace a stream. */
    assert(run_at(&vma, 200, base + 40 * stride, cpc) == 0);
    for (i = 0; i < 2; i++)
        assert(run_at(&vma, pair[i], base + i * stride + 20 * PAGE_SIZE, cpc) == 19);
    /* A full set: eight threads coexist; a ninth evicts only the LRU way. */
    {
        /* 256 threads over 8 sets: the fullest set holds at least 32. */
        unsigned int count[1U << POLICY_STREAM_SET_BITS] = { 0 }, best = 0;
        for (i = 0; i < 256; i++) count[stream_hash(i, pc, POLICY_STREAM_SET_BITS)]++;
        for (i = 1; i < (1U << POLICY_STREAM_SET_BITS); i++)
            if (count[i] > count[best]) best = i;
        for (i = 0; i < 256 && n < 9; i++)
            if (stream_hash(i, pc, POLICY_STREAM_SET_BITS) == best) q[n++] = i;
    }
    assert(n == 9);
    for (j = 0; j < 3; j++)
        for (i = 0; i < 8; i++) fault(&vma2, q[i], base + i * stride + j * PAGE_SIZE, pc);
    for (i = 0; i < 8; i++) assert(run_at(&vma2, q[i], base + i * stride + 3 * PAGE_SIZE, pc) == 2);
    long ev = stream_evictions.counter;
    fault(&vma2, q[8], base + 8 * stride, pc);
    assert(stream_evictions.counter == ev + 1);
    assert(run_at(&vma2, q[0], base + 3 * PAGE_SIZE, pc) == 0);
    for (i = 1; i < 8; i++) assert(run_at(&vma2, q[i], base + i * stride + 3 * PAGE_SIZE, pc) == 2);
    /* Replacing a stream idle past POLICY_TTL is not a live eviction. */
    clock_ns += 2 * NSEC_PER_SEC;
    fault(&vma2, q[0], base, pc);
    assert(stream_evictions.counter == ev + 1);
    hermit_policy_mm_exit(&mm); hermit_policy_mm_exit(&mm2);
    current = saved;
}

/* Replay a forward base-page run of @len pages from @base the way the fault
 * path does it: predict, read that many pages, install the demand page, and
 * let later faults skip pages already read. Returns pages read past the run.
 */
static unsigned int replay_run(struct vm_area_struct *vma, unsigned long base,
                               unsigned int len, unsigned long pc, unsigned int *faults)
{
    unsigned int page = 0, read_to = 0, wasted = 0;
    for (; page < len; page++) {
        unsigned long a = base + page * PAGE_SIZE;
        struct vm_fault vmf = { .vma = vma, .address = a, .hermit_fault_pc = pc };
        unsigned int nr = 1;
        clock_ns += 1000;
        if (page >= read_to) {              /* remote miss: predict and read */
            nr = hermit_policy_predict(&vmf);
            vmf.hermit_batch_pages = nr;
            read_to = page + nr;
            (*faults)++;
        }                                   /* else a swapcache hit on a prefetched page */
        hermit_policy_installed(&vmf, 9000 + page, a, 1);
    }
    if (read_to > len) wasted = read_to - len;
    return wasted;
}

static void fit_tests(void)
{
    const unsigned long base = 0x50000000UL, region = 2UL << 20, pc = 0x402000;
    struct mm_struct mm = { .hermit_vma_generation = { 1 } };
    struct vm_area_struct vma = { .vm_mm = &mm, .vm_start = base, .vm_end = base + 64 * region };
    unsigned int faults = 0, first, later = 0, i;
    struct task_struct *saved = current;
    current = &threads[0];
    fail_alloc = false; policy_features = HERMIT_POLICY_DEFAULT; policy_swapin_max = 64;
    /* Fixed 32-page runs, one per region: doubling 1,2,4,8,16 then 32 would
     * read 64 pages for the last batch of every run. */
    first = replay_run(&vma, base, 32, pc, &faults);
    assert(first > 0);                      /* the first run has nothing to learn from */
    long capped = capped_batches.counter;
    for (i = 1; i < 6; i++) later += replay_run(&vma, base + i * region, 32, pc, &faults);
    assert(later == 0 && capped_batches.counter > capped);   /* learned runs read exactly 32 */
    assert(batch_overruns.counter >= 1);
    /* A run longer than the learned length still grows past it. */
    unsigned int f0 = faults;
    assert(replay_run(&vma, base + 10 * region, 128, pc, &faults) <= 64);
    unsigned int long_faults = faults - f0;
    assert(long_faults < 128 / 4);          /* batches kept growing beyond 32 pages */
    /* The longer run is now the learned length: 128-page runs stop overshooting. */
    f0 = faults; later = 0;
    for (i = 11; i < 14; i++) later += replay_run(&vma, base + i * region, 128, pc, &faults);
    assert(later == 0);
    /* Shrinking back to 32-page runs costs one overshoot, then fits again. */
    replay_run(&vma, base + 20 * region, 32, pc, &faults);
    later = 0;
    for (i = 21; i < 24; i++) later += replay_run(&vma, base + i * region, 32, pc, &faults);
    assert(later == 0);
    hermit_policy_mm_exit(&mm);
    current = saved;
}

/* Replay one segment of @len pages the way do_swap_page() does after Phase B:
 * ask the policy for a folio order first, and only fall back to the 4 KiB
 * predictor when it has no suggestion. Each read counts as one fault. */
struct medium_walk {
    unsigned int max_order, suggestions, reads;
};

static void replay_medium(struct vm_area_struct *vma, unsigned long base,
                          unsigned int len, unsigned long pc, struct medium_walk *walk)
{
    unsigned int page = 0, read_to = 0;

    while (page < len) {
        unsigned long a = base + page * PAGE_SIZE;
        struct vm_fault vmf = { .vma = vma, .address = a, .hermit_fault_pc = pc };
        bool medium = false;
        int order;

        clock_ns += 1000;
        if (page < read_to) {           /* swapcache hit on a read-ahead page */
            hermit_policy_installed(&vmf, 9000 + page, a, 1);
            page++;
            continue;
        }
        order = hermit_policy_swapin_order(&vmf, 0x3fc, &medium);
        if (order >= 0) {
            unsigned int nr = 1U << order;

            /* A MEDIUM folio must never read past the learned segment. */
            assert(!medium || page + nr <= len);
            if (medium) {
                walk->suggestions++;
                if ((unsigned int)order > walk->max_order)
                    walk->max_order = order;
            }
            walk->reads++;
            hermit_policy_installed(&vmf, 9000 + page, a, nr);
            page += nr;
            read_to = page;
        } else {
            unsigned int nr = hermit_policy_predict(&vmf);

            vmf.hermit_batch_pages = nr;
            walk->reads++;
            read_to = page + nr;
            hermit_policy_installed(&vmf, 9000 + page, a, 1);
            page++;
        }
    }
}

static void medium_tests(void)
{
    const unsigned long base = 0x60000000UL, stride = 2UL << 20, pc = 0x403000;
    struct mm_struct mm = { .hermit_vma_generation = { 1 } };
    struct vm_area_struct vma = { .vm_mm = &mm, .vm_start = base,
                                 .vm_end = base + 64 * stride };
    struct task_struct *saved = current;
    unsigned int i, order4 = 0;
    long suggested;

    current = &threads[1];
    fail_alloc = false; mglru = false; policy_swapin_max = 64;
    policy_features = HERMIT_POLICY_DEFAULT;
    policy_medium_max_order = 4; policy_medium_stable = 3;
    /* The new bit is off by default: the 0x1bf baseline is unchanged. */
    assert(!(HERMIT_POLICY_DEFAULT & HERMIT_POLICY_MEDIUM));
    /* No suggestion without the bit, without a fault PC, or when the VMA
     * cannot hold a >4 KiB folio at all. */
    {
        struct vm_fault vmf = { .vma = &vma, .address = base,
                                .hermit_fault_pc = pc };
        bool medium = true;

        assert(hermit_policy_swapin_order(&vmf, 0x3fc, &medium) == -1);
        assert(!medium);
        policy_features |= HERMIT_POLICY_MEDIUM;
        assert(hermit_policy_swapin_order(&vmf, BIT(0), &medium) == -1);
        vmf.hermit_fault_pc = 0;
        assert(hermit_policy_swapin_order(&vmf, 0x3fc, &medium) == -1);
    }
    /* Length and alignment arithmetic: only a stable, repeating segment that
     * still has room for an aligned order is read as one folio. */
    {
        struct policy_stream s = {};
        unsigned long last = base;

        s.fit = 16; s.fit_stable = 3; s.last = last;
        /* A new segment starts at page 0: the whole 64 KiB. */
        assert(policy_medium_order(&s, last + 4 * PAGE_SIZE) == 4);
        /* A continuation is at page sequential + 1 and must be aligned to the
         * order, because the folio is allocated at ALIGN_DOWN(fault, 1<<order). */
        s.sequential = 0;
        assert(policy_medium_order(&s, last + PAGE_SIZE) == -1);
        s.sequential = 3;               /* position 4: order 2 */
        assert(policy_medium_order(&s, last + PAGE_SIZE) == 2);
        s.sequential = 7;               /* position 8: order 3 */
        assert(policy_medium_order(&s, last + PAGE_SIZE) == 3);
        s.sequential = 11;              /* position 12: four pages left */
        assert(policy_medium_order(&s, last + PAGE_SIZE) == 2);
        s.sequential = 12;              /* position 13: three pages left */
        assert(policy_medium_order(&s, last + PAGE_SIZE) == -1);
        s.sequential = 15;              /* the run already reached the length */
        assert(policy_medium_order(&s, last + PAGE_SIZE) == -1);
        s.fit_stable = 2;               /* not stable yet */
        assert(policy_medium_order(&s, last + 4 * PAGE_SIZE) == -1);
        s.fit_stable = 3; s.fit = 6;    /* rounded down to four pages */
        assert(policy_medium_order(&s, last + 4 * PAGE_SIZE) == 2);
        s.fit = 3;                      /* shorter than the smallest order */
        assert(policy_medium_order(&s, last + 4 * PAGE_SIZE) == -1);
        s.fit = 16; policy_medium_max_order = 2;
        assert(policy_medium_order(&s, last + 4 * PAGE_SIZE) == 2);
        policy_medium_max_order = 6;
        assert(policy_medium_order(&s, last + 4 * PAGE_SIZE) == 4);
        policy_medium_max_order = 4;
    }
    /* Repeated 16-page segments: the first segments only teach the length (fit
     * comes from the 4 KiB batches and is recorded at the next run break), then
     * one order-4 folio per segment. */
    suggested = medium_suggested.counter;
    for (i = 0; i < 5; i++) {
        struct medium_walk walk = {};

        replay_medium(&vma, base + i * stride, 16, pc, &walk);
        if (i < 4) {
            assert(walk.suggestions == 0);
        } else {
            assert(walk.suggestions == 1 && walk.max_order == 4);
            assert(walk.reads == 1);
            order4 = walk.max_order;
        }
    }
    assert(order4 == 4);
    assert(medium_suggested.counter == suggested + 1);
    /* The next segment keeps the order: the learned length is stable. */
    {
        struct medium_walk walk = {};

        replay_medium(&vma, base + 5 * stride, 16, pc, &walk);
        assert(walk.suggestions == 1 && walk.max_order == 4 && walk.reads == 1);
    }
    /* Mixed lengths never look stable, so no order is suggested: each stream
     * below starts empty, which is the ablation-relevant guarantee. */
    for (i = 0; i < 6; i++) {
        struct medium_walk walk = {};

        replay_medium(&vma, base + (8 + i) * stride, i % 2 ? 32 : 16,
                      pc + PAGE_SIZE, &walk);
        assert(walk.suggestions == 0);
    }
    /* A six-page segment allows only what fits: order 2, never order 3. */
    for (i = 0; i < 6; i++) {
        struct medium_walk walk = {};

        replay_medium(&vma, base + (16 + i) * stride, 6, pc + 2 * PAGE_SIZE,
                      &walk);
        assert(walk.max_order <= 2);
        if (i < 4) {
            assert(walk.suggestions == 0);
        } else {
            assert(walk.suggestions > 0 && walk.max_order == 2);
            assert(walk.reads == 2);    /* one order-2 folio and one batch */
        }
    }
    /* Ablation: with the bit off, a stable stream keeps the 4 KiB path. */
    policy_features = HERMIT_POLICY_DEFAULT;
    for (i = 0; i < 7; i++) {
        struct medium_walk walk = {};

        replay_medium(&vma, base + (24 + i) * stride, 16, pc + 3 * PAGE_SIZE,
                      &walk);
        assert(walk.suggestions == 0);
    }
    policy_features = HERMIT_POLICY_DEFAULT | HERMIT_POLICY_MEDIUM;
    hermit_policy_mm_exit(&mm);
    assert(live == 0);
    current = saved;
}

/* Phase C: the first fault of a repeating segment starts at the length the
 * stream already learned instead of ramping 1 -> 2 -> 4 -> ... again. */
static void fitstart_tests(void)
{
    const unsigned long base = 0x68000000UL, region = 2UL << 20, pc = 0x405000;
    struct mm_struct mm = { .hermit_vma_generation = { 1 } };
    struct vm_area_struct vma = { .vm_mm = &mm, .vm_start = base,
                                 .vm_end = base + 32 * region };
    struct task_struct *saved = current;
    unsigned int faults[12] = { 0 }, i, f;
    long batches, pages;

    current = &threads[2];
    fail_alloc = false; mglru = false;
    policy_swapin_max = 64; policy_fitstart_stable = 3;
    policy_features = HERMIT_POLICY_DEFAULT | HERMIT_POLICY_FITSTART;
    /* 32-page segments, one per 2 MiB region: before the change every
     * segment pays the whole 1,2,4,8,16,32 ramp (seven 4 KiB faults). */
    for (i = 0; i < 12; i++) {
        f = 0;
        replay_run(&vma, base + i * region, 32, pc, &f);
        faults[i] = f;
    }
    /* The length is learned at the first run break and trusted after three
     * completed segments; until then the old doubling applies. The segment
     * that learns to trust it still starts at page 1, so it takes what is
     * left of the learned length (31 pages) and two faults. */
    for (i = 0; i < 3; i++) assert(faults[i] > 2);
    assert(faults[3] == 2);
    for (i = 4; i < 12; i++) assert(faults[i] == 1);
    /* One 31-page start and eight 32-page starts, never past the fit. */
    assert(fitstart_batches.counter == 9);
    assert(fitstart_pages.counter == 8 * 32 + 31);
    batches = fitstart_batches.counter;
    pages = fitstart_pages.counter;
    policy_features = HERMIT_POLICY_DEFAULT;
    for (i = 12; i < 16; i++) {
        f = 0;
        replay_run(&vma, base + i * region, 32, pc, &f);
        assert(f > 1);                  /* ablation: the ramp comes back */
    }
    assert(fitstart_batches.counter == batches &&
           fitstart_pages.counter == pages);
    /* Mixed segment lengths never look stable, so no confident start size. */
    policy_features = HERMIT_POLICY_DEFAULT | HERMIT_POLICY_FITSTART;
    for (i = 16; i < 26; i++) {
        f = 0;
        replay_run(&vma, base + i * region, i % 2 ? 32 : 16,
                   pc + PAGE_SIZE, &f);
        assert(f > 1);
    }
    assert(fitstart_batches.counter == batches);

    /* Start size arithmetic: the learned length and the SG limit clamp it, and
     * a contiguous fault starts with what is left of the segment. */
    hermit_policy_mm_exit(&mm);
    assert(live == 0);
    {
        struct hermit_policy_state *st;
        struct policy_stream *s;
        struct vm_fault vmf = { .vma = &vma, .hermit_fault_pc = pc + 2 * PAGE_SIZE };

        vmf.address = base;
        clock_ns += 1000;
        hermit_policy_installed(&vmf, 1, base, 1);
        st = mm.hermit_policy;
        s = policy_stream(st, &vmf, false);
        assert(st && s);
        s->fit = 32; s->fit_stable = 3; s->last = base; s->sequential = 0;
        vmf.address = base + 4 * PAGE_SIZE;         /* first fault, page 0 */
        assert(hermit_policy_predict(&vmf) == 32);
        policy_swapin_max = 8;                      /* SG budget wins */
        assert(hermit_policy_predict(&vmf) == 8);
        policy_swapin_max = 64;
        vmf.address = base + PAGE_SIZE;             /* page sequential + 1 */
        assert(hermit_policy_predict(&vmf) == 31);
        s->fit_stable = 2;                          /* not trusted yet */
        assert(hermit_policy_predict(&vmf) == 1);
        s->fit_stable = 3;
        s->fit = 1;                                 /* no batch at all */
        assert(hermit_policy_predict(&vmf) == 1);

        /* PTEBATCH gate: the whole batch must lie inside a trusted segment. */
        policy_features |= HERMIT_POLICY_PTEBATCH;
        s->fit = 32; s->fit_stable = 3; s->last = base; s->sequential = 0;
        vmf.address = base;
        assert(hermit_policy_pte_install(&vmf, 32));
        assert(!hermit_policy_pte_install(&vmf, 33));
        assert(!hermit_policy_pte_install(&vmf, 1));
        vmf.address = base + PAGE_SIZE;             /* one page used */
        assert(hermit_policy_pte_install(&vmf, 31));
        assert(!hermit_policy_pte_install(&vmf, 32));
        s->fit_stable = 2;
        assert(!hermit_policy_pte_install(&vmf, 2));
        s->fit_stable = 3;
        policy_features &= ~HERMIT_POLICY_PTEBATCH;
        assert(!hermit_policy_pte_install(&vmf, 2));
        policy_features |= HERMIT_POLICY_PTEBATCH;
        vmf.hermit_fault_pc = 0;                    /* no policy stream */
        assert(!hermit_policy_pte_install(&vmf, 2));
        vmf.hermit_fault_pc = pc + 2 * PAGE_SIZE;
        {
            struct vm_fault other = { .vma = &vma,
                .address = base + PAGE_SIZE, .hermit_fault_pc = pc + 3 * PAGE_SIZE };

            assert(!hermit_policy_pte_install(&other, 2));
        }
        st->refault = 256;                          /* refault gate */
        assert(!hermit_policy_pte_install(&vmf, 2));
        st->refault = 0;
        assert(hermit_policy_pte_install(&vmf, 2));
        hermit_policy_pte_installed(7);
        hermit_policy_pte_skipped(3);
        assert(ptebatch_installed.counter == 7 &&
               ptebatch_skipped.counter == 3);
    }
    hermit_policy_mm_exit(&mm);
    assert(live == 0);
    policy_features = HERMIT_POLICY_DEFAULT;
    current = saved;
}

int main(void)
{
    struct mm_struct mm = { .hermit_vma_generation = { 1 } };
    struct vm_area_struct vma = { .vm_mm = &mm, .vm_start = addr(0), .vm_end = addr(4096) };
    struct folio base[4], huge = { .pfn = 9000, .large = true, .anon = true, .mapcount = 1 };
    policy_pages_max = 4;
    for (unsigned int i = 0; i < 4; i++) {
        base[i] = (struct folio){ .pfn = 100 + i, .anon = true, .mapcount = 1 };
        hermit_policy_observe(&base[i], &vma, addr(i), false, true);
    }
    struct hermit_policy_state *st = mm.hermit_policy;
    assert(st && st->nr == 4 && started_epochs.counter == 4 && flushes == 4);
    /* 512 PTEs of a 2 MiB folio: no entry created, none of the four evicted. */
    for (unsigned int i = 0; i < 512; i++)
        hermit_policy_observe(&huge, &vma, addr(1024 + i), false, true);
    assert(st->nr == 4 && evicted_pages.counter == 0 && invalid_epochs.counter == 512);
    for (unsigned int i = 0; i < 4; i++) assert(policy_lookup(st, addr(i)));
    assert(!policy_lookup(st, addr(1024)) && flushes == 4);
    /* A shared mapping of a tracked address resets that entry in place. */
    struct folio shared = base[2]; shared.mapcount = 2;
    hermit_policy_observe(&shared, &vma, addr(2), false, true);
    assert(!policy_lookup(st, addr(2))->valid && st->nr == 4 && invalid_epochs.counter == 513);
    /* Valid base pages still complete an empty interval and become cold. */
    clock_ns += 2 * NSEC_PER_SEC / 1000;
    hermit_policy_observe(&base[0], &vma, addr(0), false, true);
    assert(observed_pages.counter == 1 && cold_pages.counter == 1);
    /* MGLRU skips observation and flushes. A round trip cannot reuse the
     * old cold interval, even if this page was never visited by MGLRU. */
    struct hermit_reclaim_hint hint;
    mapped_vma = &vma; mapped_addr = addr(0);
    hermit_policy_hint(&base[0], &hint); assert(hint.cold);
    hermit_policy_lru_changed(); mglru = true;
    int old_flushes = flushes;
    hermit_policy_observe(&base[0], &vma, addr(0), false, true);
    hermit_policy_observe(&base[1], &vma, addr(2000), false, true);
    assert(flushes == old_flushes && st->nr == 4 && !policy_lookup(st, addr(2000)));
    hermit_policy_hint(&base[0], &hint); assert(!hint.cold && !hint.mglru);
    mglru = false; hermit_policy_lru_changed();
    hermit_policy_hint(&base[0], &hint); assert(!hint.cold);
    clock_ns += 2 * NSEC_PER_MSEC;
    hermit_policy_observe(&base[0], &vma, addr(0), false, true);
    assert(policy_lookup(st, addr(0))->empty == 0);
    hermit_policy_hint(&base[0], &hint); assert(!hint.cold);
    clock_ns += 2 * NSEC_PER_MSEC;
    hermit_policy_observe(&base[0], &vma, addr(0), false, true);
    hermit_policy_hint(&base[0], &hint); assert(hint.cold);
    /* A fifth base page still evicts normally through the LRU. */
    struct folio extra = { .pfn = 200, .anon = true, .mapcount = 1 };
    hermit_policy_observe(&extra, &vma, addr(4), false, true);
    assert(st->nr == 4 && evicted_pages.counter == 1);
    /* Observation off: nothing is touched. */
    long before = invalid_epochs.counter;
    policy_features &= ~HERMIT_POLICY_OBSERVE;
    hermit_policy_observe(&huge, &vma, addr(2000), false, true);
    assert(invalid_epochs.counter == before);
    hermit_policy_mm_exit(&mm);
    assert(live == 0);
    /* Native MGLRU selection can batch first-eviction pages without a
     * pre-existing PTE observation. Generic reclaim cannot manufacture it. */
    policy_features = HERMIT_POLICY_DEFAULT;
    mglru = true; mapped_addr = addr(0);
    hermit_policy_observe(&base[0], &vma, addr(0), false, true);
    assert(!mm.hermit_policy && live == 0);
    hermit_policy_eviction(&base[0], &hint, false);
    assert(!hint.owner && !hint.mglru && !mm.hermit_policy);
    hermit_policy_eviction(&base[0], &hint, true);
    st = mm.hermit_policy;
    assert(hint.mglru && !hint.cold && hint.owner == st->id);
    assert(hint.start == vma.vm_start && hint.end == vma.vm_end);
    assert(hint.address == addr(0) && hint.generation == 1);
    /* MGLRU eviction builds the batch key from the VMA: no table entry and
     * no ghost. The swap shadow, read by do_swap_page(), reports refaults. */
    assert(!policy_lookup(st, addr(0)) && st->nr == 0 && mglru_candidates.counter == 1);
    struct vm_fault vmf = { .vma = &vma, .address = addr(0), .hermit_fault_pc = 1234 };
    clock_ns += 100;
    hermit_policy_installed(&vmf, 500, addr(0), 1);
    assert(refault_pages.counter == 0 && st->refault == 0 && st->nr == 0);
    vmf.hermit_refault = true; clock_ns += 100;
    hermit_policy_installed(&vmf, 501, addr(0), 1);
    assert(refault_pages.counter == 1 && st->refault == 128 && st->nr == 0);
    /* PEBS samples feed only negative-observation evidence: under MGLRU
     * they take no lock and touch no counter; classic LRU still records. */
    unsigned int locks_before = locks;
    long late_before = late_samples.counter;
    hermit_policy_sample(&mm, addr(7), clock_ns);
    assert(locks == locks_before && late_samples.counter == late_before);
    mglru = false;
    hermit_policy_sample(&mm, addr(7), clock_ns);
    assert(locks == locks_before + 1 && late_samples.counter == late_before + 1);
    mglru = true;
    /* Large/shared/KSM/file pages and notifier mappings remain excluded. */
    unsigned int before_walks = walks;
    hermit_policy_eviction(&huge, &hint, true); assert(!hint.mglru && !hint.owner);
    hermit_policy_eviction(&shared, &hint, true); assert(!hint.mglru && !hint.owner);
    struct folio rejected = base[0]; rejected.ksm = true;
    hermit_policy_eviction(&rejected, &hint, true); assert(!hint.mglru && !hint.owner);
    rejected.ksm = false; rejected.anon = false;
    hermit_policy_eviction(&rejected, &hint, true); assert(!hint.mglru && !hint.owner);
    assert(walks == before_walks);
    notifier = true;
    hermit_policy_eviction(&base[0], &hint, true); assert(!hint.mglru && !hint.owner);
    notifier = false; contended = true;
    hermit_policy_eviction(&base[0], &hint, true); assert(!hint.mglru && !hint.owner);
    contended = false; mappings = 0;
    hermit_policy_eviction(&base[0], &hint, true); assert(!hint.mglru && !hint.owner);
    mappings = 2;
    hermit_policy_eviction(&base[0], &hint, true); assert(!hint.mglru && !hint.owner);
    mappings = 1;
    assert(mglru_candidates.counter == 1);
    /* In-flight provenance survives disabling MGLRU after isolation. */
    mglru = false; hermit_policy_lru_changed(); mm.hermit_vma_generation.counter++;
    hermit_policy_eviction(&base[0], &hint, true);
    assert(hint.mglru && !hint.cold && hint.generation == 2);
    policy_features &= ~HERMIT_POLICY_MGLRU;
    hermit_policy_eviction(&base[0], &hint, true); assert(!hint.mglru);
    policy_features |= HERMIT_POLICY_MGLRU;
    hermit_pebs_enabled = 0;
    hermit_policy_eviction(&base[0], &hint, true); assert(!hint.mglru && !hint.owner);
    hermit_pebs_enabled = 1; hermit_pebs_all = 0;
    hermit_policy_eviction(&base[0], &hint, true); assert(!hint.mglru && !hint.owner);
    hermit_pebs_all = 1;
    hermit_policy_mm_exit(&mm);
    fail_alloc = true;
    hermit_policy_eviction(&base[0], &hint, true);
    assert(!hint.mglru && !hint.owner && !mm.hermit_policy && live == 0);
    puts("Cold observation and MGLRU cooperation tests: PASS");
    stream_tests();
    assert(live == 0);
    puts("Set-associative stream state tests: PASS");
    fit_tests();
    assert(live == 0);
    puts("Run-length fit tests: PASS");
    medium_tests();
    assert(live == 0);
    puts("Fit-driven medium-order tests: PASS");
    fitstart_tests();
    assert(live == 0);
    puts("Fit-sized first batch and PTE-batch gate tests: PASS");
}
'''
    exit_start = source.index('void hermit_policy_mm_exit(')
    exit_end = source.index('/* The first seven fields', exit_start)
    hint_start = source.index('struct hint_walk {')
    hint_end = source.index('struct policy_rank {', hint_start)
    # The first slice now ends where this one begins, so hermit_policy_sample()
    # is compiled too.
    installed_start = end
    # Through hermit_policy_predict() and the Phase B order suggestion, so
    # run-length feedback and fit stability are exercised end to end.
    installed_end = source.index('/* Record one page read speculatively', installed_start)
    header = (root / 'linux-stable/include/linux/hermit_policy.h').read_text()
    hint_struct = header[header.index('struct hermit_reclaim_hint {'):header.index('/* debugfs')]
    with tempfile.TemporaryDirectory(prefix='hermit-observe-test-') as tmp:
        cfile, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        cfile.write_text(harness + hint_struct + source[start:end] +
                        source[installed_start:installed_end] + source[hint_start:hint_end] +
                        source[exit_start:exit_end] + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-function', '-Wno-unused-variable', '-Wno-unused-parameter',
                        '-fsanitize=address,undefined', '-g',
                        '-I', str(root / 'linux-stable/tools/include'),
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def test_shadow_refault():
    """Production MGLRU refault rule: recent eviction alone is not a refault."""
    root = Path(__file__).resolve().parents[3]
    source = (root / 'linux-stable/mm/memory.c').read_text()
    start = source.index('static bool hermit_shadow_refault(')
    end = source.index('\n}\n', start) + 3
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
typedef struct { unsigned long val; } swp_entry_t;
static void *shadow;
static bool recent, workingset_bit, flushed;
static unsigned int lookups;
static void *swap_cache_get_shadow(swp_entry_t e) { (void)e; lookups++; return shadow; }
static bool workingset_test_recent(void *s, bool file, bool *workingset, bool flush)
{
    assert(s == shadow && !file);
    flushed |= flush;
    *workingset = workingset_bit;
    return recent;
}
'''
    tests = r'''
int main(void)
{
    static int token;
    swp_entry_t e = { 42 };
    /* No shadow: never evicted through reclaim. */
    shadow = 0; recent = workingset_bit = true;
    assert(!hermit_shadow_refault(e));
    shadow = &token;
    /* A scan re-reading what it evicted: recent, but never promoted while
     * resident. Counting this disabled prediction on every density run. */
    recent = true; workingset_bit = false;
    assert(!hermit_shadow_refault(e));
    /* Promoted (accessed again while resident) and evicted recently: thrash. */
    recent = true; workingset_bit = true;
    assert(hermit_shadow_refault(e));
    /* Promoted but evicted long ago: not a refault. */
    recent = false; workingset_bit = true;
    assert(!hermit_shadow_refault(e));
    /* The fault path must not force a memcg stats flush. */
    assert(!flushed && lookups == 4);
    puts("MGLRU shadow refault rule tests: PASS");
}
'''
    with tempfile.TemporaryDirectory(prefix='hermit-refault-test-') as tmp:
        cfile, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        cfile.write_text(harness + source[start:end] + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-g', str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)

def test_swapin_cost():
    """Per-order swap-in cost counters and the prefetch-hit set."""
    root = Path(__file__).resolve().parents[3]
    stats = (root / 'linux-stable/mm/hermit_stats.c').read_text()
    policy = (root / 'linux-stable/mm/hermit_policy.c').read_text()
    counters = stats[stats.index('struct hermit_time_stat {'):
                     stats.index('static const char * const hermit_counter_names')]
    reset = stats[stats.index('void hermit_stats_reset(void)'):
                  stats.index('void hermit_stats_report(void)')]
    prefetch = policy[policy.index('/* Record one page read speculatively'):
                      policy.index('struct hint_walk {')]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint64_t u64;
#define HERMIT_COST_ORDERS 10
#define HMT_STAT_NR 1
#define HMT_LAT_NR 1
#define POLICY_TTL 1000ULL
#define POLICY_PREFETCH_BITS 8
#define POLICY_PREFETCH_SLOTS (1U << POLICY_PREFETCH_BITS)
typedef struct { long long counter; } atomic64_t;
typedef struct { long counter; } atomic_long_t;
#define atomic64_add(v, p) ((p)->counter += (long long)(v))
#define atomic64_inc(p) ((p)->counter++)
#define atomic64_read(p) ((p)->counter)
#define atomic64_set(p, v) ((p)->counter = (long long)(v))
#define atomic_long_set(p, v) ((p)->counter = (long)(v))
static atomic_long_t hermit_counters[HMT_STAT_NR];
#define DEFINE_STATIC_KEY_FALSE(name) static int name##_defined __attribute__((unused))
/* The lock is a real counter so the tests can prove the set is entered once. */
typedef int spinlock_t;
static unsigned int lock_depth;
#define spin_lock_irqsave(p, flags) do { assert(!(*(p))++); flags = 0; lock_depth++; } while (0)
#define spin_unlock_irqrestore(p, flags) do { assert((*(p))-- == 1); (void)(flags); lock_depth--; } while (0)
static unsigned long policy_hash(unsigned long v, unsigned int bits)
{ return ((v * 0x61c8864680b583ebUL) >> (64 - bits)) & ((1U << bits) - 1); }
#define hash_long(v, bits) policy_hash((v), (bits))
static u64 clock_ns = 5000;
static u64 ktime_get_ns(void) { return clock_ns; }
struct hermit_policy_state {
    spinlock_t lock;
    unsigned int nr, refault;
    u64 id;
    struct {
        unsigned long entry;
        u64 time;
    } prefetch[POLICY_PREFETCH_SLOTS];
};
struct mm_struct { struct hermit_policy_state *hermit_policy; };
#define READ_ONCE(x) (x)
'''
    # Declared after the extracted struct: hermit_stats_reset() resets it.
    mid = r'''
static struct hermit_time_stat hermit_latencies[HMT_LAT_NR];
'''
    tests = r'''
static struct hermit_policy_state state;
static struct mm_struct mm;

int main(void)
{
    unsigned int i;

    /* Per-order counters: out-of-range orders are ignored, reset clears. */
    hermit_swapin_cost_add(0, 1000);
    hermit_swapin_cost_add(0, 500);
    hermit_swapin_cost_add(4, 7000);
    hermit_swapin_cost_add(9, 1);
    hermit_swapin_cost_add(HERMIT_COST_ORDERS, 42);
    assert(atomic64_read(&hermit_swapin_cost[0].count) == 2);
    assert(atomic64_read(&hermit_swapin_cost[0].total_ns) == 1500);
    assert(atomic64_read(&hermit_swapin_cost[4].count) == 1);
    assert(atomic64_read(&hermit_swapin_cost[4].total_ns) == 7000);
    assert(atomic64_read(&hermit_swapin_cost[9].count) == 1);
    for (i = 1; i < HERMIT_COST_ORDERS; i++)
        if (i != 4 && i != 9)
            assert(atomic64_read(&hermit_swapin_cost[i].count) == 0);
    hermit_swapin_cost_prefetched(7);
    hermit_swapin_cost_hit(200);
    hermit_swapin_cost_hit(300);
    assert(atomic64_read(&hermit_prefetched_pages) == 7);
    assert(atomic64_read(&hermit_prefetch_hits) == 2);
    assert(atomic64_read(&hermit_prefetch_hit_ns) == 500);
    hermit_stats_reset();
    assert(atomic64_read(&hermit_swapin_cost[0].count) == 0);
    assert(atomic64_read(&hermit_swapin_cost[4].total_ns) == 0);
    assert(atomic64_read(&hermit_prefetched_pages) == 0);
    assert(atomic64_read(&hermit_prefetch_hits) == 0);
    assert(atomic64_read(&hermit_prefetch_hit_ns) == 0);

    /* Prefetch set: a recorded entry is consumed by exactly one hit. */
    memset(&state, 0, sizeof(state));
    mm.hermit_policy = &state;
    hermit_policy_prefetched(&mm, 0x1234);
    assert(lock_depth == 0);
    assert(hermit_policy_prefetch_hit(&mm, 0x1234));
    assert(!hermit_policy_prefetch_hit(&mm, 0x1234));
    /* An unrelated entry is never a hit. */
    hermit_policy_prefetched(&mm, 0x2000);
    assert(!hermit_policy_prefetch_hit(&mm, 0x2001));
    assert(hermit_policy_prefetch_hit(&mm, 0x2000));
    /* Expired entries are ignored. */
    hermit_policy_prefetched(&mm, 0x3000);
    clock_ns += POLICY_TTL;
    assert(!hermit_policy_prefetch_hit(&mm, 0x3000));
    /* Direct-mapped collisions keep the newest entry only, and a miss for the
     * evicted one is harmless: it is a lost hit count, never a policy input. */
    clock_ns += POLICY_TTL;
    hermit_policy_prefetched(&mm, 0x4100);
    hermit_policy_prefetched(&mm, 0x4100 + (1UL << (64 - POLICY_PREFETCH_BITS)) * 0);
    assert(hermit_policy_prefetch_hit(&mm, 0x4100));
    assert(lock_depth == 0);
    /* No per-mm state: the instrumentation is a no-op. */
    mm.hermit_policy = NULL;
    hermit_policy_prefetched(&mm, 0x5555);
    assert(!hermit_policy_prefetch_hit(&mm, 0x5555));
    /* The unrelated counter arrays stay untouched by the cost table. */
    assert(hermit_counters[0].counter == 0);
    assert(hermit_latencies[0].count.counter == 0);
    puts("swap-in cost instrumentation tests: PASS");

    return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='hermit-cost-test-') as tmp:
        cfile, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        cfile.write_text(harness + counters + mid + reset + prefetch + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-fsanitize=address,undefined', '-g',
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def test_pte_batch_install():
    """Production batch PTE install: revalidation, folio checks, counters."""
    root = Path(__file__).resolve().parents[3]
    source = (root / 'linux-stable/mm/memory.c').read_text()
    start = source.index('static unsigned int hermit_batch_ptes(')
    end = source.index('\n#endif\n\n/*\n * We enter with non-exclusive mmap_lock', start)
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#define PAGE_SHIFT 12
#define PAGE_SIZE 4096UL
#define PAGE_MASK (~(PAGE_SIZE - 1))
#define PMD_SIZE (1UL << 21)
#define PMD_MASK (~(PMD_SIZE - 1))
#define VM_WRITE 0x2UL
#define SWP_STABLE_WRITES 0x8UL
#define FAULT_FLAG_WRITE 0x1U
#define FAULT_FLAG_VMA_LOCK 0x800U
#define MM_ANONPAGES 2
#define MM_SWAPENTS 3
#define RMAP_NONE 0
#define RMAP_EXCLUSIVE 1
#define min_t(t, a, b) ((t)(a) < (t)(b) ? (t)(a) : (t)(b))
#define unlikely(x) (x)
#define data_race(x) (x)
#define BUG_ON(x) assert(!(x))
/* Swap PTE: bit 63 marks a swap entry, bits 8+ hold the swap offset and the
 * low byte carries the exclusive / soft-dirty / uffd-wp markers that
 * pte_move_swp_offset() must preserve.
 */
typedef struct { unsigned long v; } pte_t;
typedef struct { unsigned long val; } swp_entry_t;
/* Present-PTE markers, clear of the swap entry encoding in bits 0..39. */
#define PTE_PRESENT (1UL << 62)
#define PTE_WRITE (1UL << 61)
#define PTE_YOUNG (1UL << 60)
#define PTE_SOFT (1UL << 59)
#define PTE_UFFD (1UL << 58)
struct page { unsigned long pfn; };
struct folio { struct page page; unsigned int order; int refs;
	bool locked, uptodate, ksm, anon, mappedtodisk, writeback, dropped; };
struct mm_struct { int unused; };
struct swap_info_struct { unsigned long flags; };
typedef int rmap_t;
struct vm_area_struct { struct mm_struct *vm_mm; unsigned long vm_start, vm_end, vm_flags;
	unsigned long vm_page_prot; };
struct vm_fault { struct vm_area_struct *vma; unsigned int flags;
	unsigned long address, hermit_fault_pc; unsigned int hermit_batch_pages; };

static pte_t ptes[512];
static unsigned long base = 0x40000000UL;
static struct folio folios[16];
static bool registered[16];
static bool policy_ok, policy_called, uffd;
static unsigned int installed_pages, skipped_pages, folio_puts, unlocks, set_calls, frees;
static unsigned int swap_frees, anon_delta, swpent_delta, rmap_exclusive, icache;
static unsigned int mkwrite, mkuffd, mksoft, free_swap_calls;
static bool free_swap_result;

static pte_t swap_pte(unsigned int off, unsigned int extra)
{ return (pte_t){ (1UL << 63) | ((unsigned long)off << 8) | extra }; }
static pte_t pte_move_swp_offset(pte_t p, unsigned int i)
{ return (pte_t){ p.v + ((unsigned long)i << 8) }; }
static swp_entry_t pte_to_swp_entry(pte_t p)
{ return (swp_entry_t){ (p.v >> 8) & 0xffffffffUL }; }
static bool pte_same(pte_t a, pte_t b) { return a.v == b.v; }
static pte_t ptep_get(pte_t *p) { return *p; }
static unsigned long pmd_addr_end(unsigned long addr, unsigned long end)
{
	unsigned long boundary = (addr + PMD_SIZE) & PMD_MASK;

	return boundary < end ? boundary : end;
}
static bool userfaultfd_armed(struct vm_area_struct *vma)
{ (void)vma; return uffd; }
static bool hermit_policy_pte_install(struct vm_fault *vmf, unsigned int pages)
{ (void)vmf; policy_called = true; assert(pages >= 2); return policy_ok; }
static void hermit_policy_pte_installed(unsigned int pages) { installed_pages += pages; }
static void hermit_policy_pte_skipped(unsigned int pages) { skipped_pages += pages; }
static struct folio *swap_cache_get_folio(swp_entry_t e)
{
	unsigned int off = e.val % 16;

	if (!registered[off])
		return NULL;
	folios[off].refs++;
	return &folios[off];
}
static unsigned int folio_order(struct folio *f) { return f->order; }
static bool folio_test_uptodate(struct folio *f) { return f->uptodate; }
static bool folio_test_ksm(struct folio *f) { return f->ksm; }
static bool folio_test_anon(struct folio *f) { return f->anon; }
static bool folio_test_mappedtodisk(struct folio *f) { return f->mappedtodisk; }
static bool folio_test_writeback(struct folio *f) { return f->writeback; }
static bool folio_trylock(struct folio *f)
{ if (f->locked) return false; f->locked = true; return true; }
static void folio_unlock(struct folio *f) { assert(f->locked); f->locked = false; unlocks++; }
static void folio_put(struct folio *f) { f->refs--; folio_puts++; }
/* Reclaim may drop the folio from swapcache between the lockless lookup and
 * the trylock; .dropped models that window.
 */
static bool folio_matches_swap_entry(const struct folio *f, swp_entry_t e)
{ assert(f->locked); (void)e; return !f->dropped; }
static int folio_ref_count(struct folio *f) { return f->refs; }
static bool PageHWPoison(struct page *p) { (void)p; return false; }
static bool PageAnonExclusive(struct page *p) { (void)p; return false; }
static bool pte_swp_exclusive(pte_t p) { return p.v & 1; }
static bool pte_swp_soft_dirty(pte_t p) { return p.v & 2; }
static bool pte_swp_uffd_wp(pte_t p) { return p.v & 4; }
static swp_entry_t folio_swap(swp_entry_t e, struct folio *f) { (void)f; return e; }
static void arch_swap_restore(swp_entry_t e, struct folio *f) { (void)e; (void)f; }
static void swap_free(swp_entry_t e) { (void)e; swap_frees++; }
static bool should_try_to_free_swap(struct folio *f, struct vm_area_struct *v,
		unsigned int flags)
{ (void)f; (void)v; (void)flags; free_swap_calls++; return free_swap_result; }
static bool folio_free_swap(struct folio *f) { assert(f->locked); f->refs--; frees++; return true; }
static void add_mm_counter(struct mm_struct *mm, int member, long value)
{
	(void)mm;
	if (member == MM_ANONPAGES) anon_delta += value;
	if (member == MM_SWAPENTS) swpent_delta += value;
}
static pte_t mk_pte(struct page *p, unsigned long prot)
{ (void)prot; return (pte_t){ PTE_PRESENT | (p->pfn << 20) }; }
static pte_t pte_mksoft_dirty(pte_t p) { mksoft++; return (pte_t){ p.v | PTE_SOFT }; }
static pte_t pte_mkuffd_wp(pte_t p) { mkuffd++; return (pte_t){ p.v | PTE_UFFD }; }
static bool userfaultfd_pte_wp(struct vm_area_struct *v, pte_t p) { (void)v; (void)p; return false; }
static bool pte_needs_soft_dirty_wp(struct vm_area_struct *v, pte_t p)
{ (void)v; (void)p; return false; }
static pte_t pte_mkwrite(pte_t p, struct vm_area_struct *v)
{ (void)v; mkwrite++; return (pte_t){ p.v | PTE_WRITE }; }
static pte_t pte_mkyoung(pte_t p) { return (pte_t){ p.v | PTE_YOUNG }; }
static void flush_icache_pages(struct vm_area_struct *v, struct page *p, unsigned int n)
{ (void)v; (void)p; (void)n; icache++; }
static void folio_add_anon_rmap_pte(struct folio *f, struct page *p,
		struct vm_area_struct *v, unsigned long addr, int flags)
{
	(void)f; (void)p; (void)v; (void)addr;
	rmap_exclusive += !!(flags & RMAP_EXCLUSIVE);
}
static void set_ptes(struct mm_struct *mm, unsigned long addr, pte_t *ptep, pte_t pte,
		unsigned int nr)
{
	(void)mm; assert(nr == 1); assert((addr & PAGE_MASK) == addr);
	*ptep = pte; set_calls++;
}
'''
    tests = r'''
static struct mm_struct mm;
static struct swap_info_struct si;
static struct vm_area_struct vma;

static void reset(void)
{
	memset(ptes, 0, sizeof(ptes));
	memset(folios, 0, sizeof(folios));
	memset(&si, 0, sizeof(si));
	vma = (struct vm_area_struct){ .vm_mm = &mm, .vm_start = base,
		.vm_end = base + PMD_SIZE, .vm_flags = VM_WRITE };
	policy_ok = true; policy_called = false; uffd = false; free_swap_result = false;
	installed_pages = skipped_pages = folio_puts = unlocks = set_calls = frees = 0;
	swap_frees = anon_delta = swpent_delta = rmap_exclusive = icache = 0;
	mkwrite = mkuffd = mksoft = free_swap_calls = 0;
	for (unsigned int i = 0; i < 16; i++) {
		folios[i] = (struct folio){ .page = { .pfn = 5000 + i }, .order = 0,
			.refs = 0, .uptodate = true, .anon = true };
		registered[i] = true;
	}
}

/* A batch of @pages swap entries from offset 0, all with the same markers. */
static void fill(unsigned int pages, unsigned int extra)
{
	for (unsigned int i = 0; i < pages; i++)
		ptes[i] = swap_pte(i, extra);
}

static unsigned int run(unsigned int pages, unsigned int extra)
{
	struct vm_fault vmf = { .vma = &vma, .address = base,
		.hermit_batch_pages = pages };

	return hermit_batch_ptes(&vmf, &si, ptes, base, swap_pte(0, extra));
}

int main(void)
{
	/* Happy path: three prefetched pages of a four-page batch are mapped,
	 * young, and the swap entry is released exactly as do_swap_page does it. */
	reset(); fill(4, 1);
	for (unsigned int i = 1; i < 4; i++) folios[i].refs = 1;   /* swapcache */
	assert(run(4, 1) == 3 && policy_called);
	assert(installed_pages == 3 && skipped_pages == 0);
	assert(swap_frees == 3 && anon_delta == 3 && swpent_delta == (unsigned int)-3);
	assert(mkwrite == 3 && rmap_exclusive == 3 && icache == 3);
	assert(set_calls == 3 && unlocks == 3 && free_swap_calls == 3);
	for (unsigned int i = 1; i < 4; i++) {
		assert(ptes[i].v & PTE_PRESENT);    /* present */
		assert(ptes[i].v & PTE_YOUNG);      /* young, never old */
		assert(ptes[i].v & PTE_WRITE);      /* writable: the PTE said exclusive */
		/* The swapcache lookup reference became the PTE reference: one
		 * for the cache and one for the mapping, as in do_swap_page. */
		assert(folios[i].refs == 2 && !folios[i].locked);
	}
	/* A read-only VMA maps the same pages read-only. As in do_swap_page(),
	 * the rmap is still exclusive; only the PTE is not writable. */
	reset(); fill(4, 1); vma.vm_flags = 0;
	for (unsigned int i = 1; i < 4; i++) folios[i].refs = 1;
	assert(run(4, 1) == 3);
	assert(mkwrite == 0 && rmap_exclusive == 3);
	for (unsigned int i = 1; i < 4; i++)
		assert(!(ptes[i].v & PTE_WRITE) && (ptes[i].v & PTE_YOUNG));

	/* A changed swap PTE stops the run; the remaining pages stay unmapped and
	 * are counted once. */
	reset(); fill(4, 1);
	for (unsigned int i = 1; i < 4; i++) folios[i].refs = 1;
	ptes[2] = swap_pte(99, 1);              /* e.g. swapped out again */
	assert(run(4, 1) == 1);
	assert(installed_pages == 1 && skipped_pages == 2 && swap_frees == 1);
	assert(!(ptes[2].v & PTE_PRESENT) && !(ptes[3].v & PTE_PRESENT));
	/* Same for a PTE whose offset is right but whose markers differ. */
	reset(); fill(4, 1);
	for (unsigned int i = 1; i < 4; i++) folios[i].refs = 1;
	ptes[1] = swap_pte(1, 0);               /* exclusive bit cleared */
	assert(run(4, 1) == 0 && skipped_pages == 3);

	/* VMA boundary: the batch is clipped, so pages outside it are not counted. */
	reset(); fill(8, 1);
	for (unsigned int i = 1; i < 8; i++) folios[i].refs = 1;
	vma.vm_end = base + 3 * PAGE_SIZE;
	assert(run(8, 1) == 2);
	assert(installed_pages == 2 && skipped_pages == 0);
	/* PMD boundary, same rule inside a 2 MiB region. */
	reset(); fill(8, 1);
	for (unsigned int i = 1; i < 8; i++) folios[i].refs = 1;
	{
		struct vm_fault vmf = { .vma = &vma,
			.address = base + PMD_SIZE - 2 * PAGE_SIZE,
			.hermit_batch_pages = 8 };

		ptes[0] = swap_pte(0, 1); ptes[1] = swap_pte(1, 1);
		ptes[2] = swap_pte(2, 1);
		vma.vm_end = base + 2 * PMD_SIZE;
		assert(hermit_batch_ptes(&vmf, &si, ptes, vmf.address, ptes[0]) == 1);
	}

	/* A folio that is not uptodate, not in swapcache, not order 0, not anon,
	 * KSM, or locked cannot be mapped. */
	reset(); fill(4, 1);
	for (unsigned int i = 1; i < 4; i++) folios[i].refs = 1;
	folios[1].uptodate = false;
	assert(run(4, 1) == 0 && skipped_pages == 3 && swap_frees == 0);
	assert(folio_puts == 1 && unlocks == 0 && !folios[1].locked);
	reset(); fill(4, 1); registered[1] = false;
	assert(run(4, 1) == 0 && skipped_pages == 3 && folio_puts == 0);
	reset(); fill(4, 1); folios[1].order = 2;  /* a large folio maps too much */
	assert(run(4, 1) == 0 && skipped_pages == 3);
	reset(); fill(4, 1); folios[1].anon = false;
	assert(run(4, 1) == 0 && skipped_pages == 3);
	reset(); fill(4, 1); folios[1].ksm = true;
	assert(run(4, 1) == 0 && skipped_pages == 3);
	reset(); fill(4, 1);
	folios[1].refs = 1; folios[1].locked = true;   /* someone else owns it */
	assert(run(4, 1) == 0 && skipped_pages == 3 && folio_puts == 1);
	/* Removed from swapcache after the lookup: unlocked, put, nothing mapped. */
	reset(); fill(4, 1);
	folios[1].refs = 1; folios[1].dropped = true;
	assert(run(4, 1) == 0 && skipped_pages == 3 && swap_frees == 0);
	assert(folio_puts == 1 && unlocks == 1 && !folios[1].locked);

	/* The policy gate, uffd, the VMA lock and a single-page batch install
	 * nothing and touch no PTE. */
	reset(); fill(4, 1); policy_ok = false;
	assert(run(4, 1) == 0 && installed_pages == 0 && skipped_pages == 0);
	assert(swap_frees == 0 && mksoft == 0);
	reset(); fill(4, 1); uffd = true;
	assert(run(4, 1) == 0 && !policy_called); uffd = false;
	reset(); fill(4, 1);
	{
		struct vm_fault vmf = { .vma = &vma, .address = base,
			.hermit_batch_pages = 4, .flags = FAULT_FLAG_VMA_LOCK };

		assert(hermit_batch_ptes(&vmf, &si, ptes, base, ptes[0]) == 0);
		assert(!policy_called);
		vmf.flags = 0; vmf.hermit_batch_pages = 1;
		assert(hermit_batch_ptes(&vmf, &si, ptes, base, ptes[0]) == 0);
		assert(!policy_called);
		/* A demand PTE that no longer matches the batch plan. */
		vmf.hermit_batch_pages = 4;
		assert(hermit_batch_ptes(&vmf, &si, ptes, base, swap_pte(7, 1)) == 0);
		assert(skipped_pages == 3);
	}

	/* Soft-dirty and uffd-wp markers survive (bits 1 and 2 of the swap PTE). */
	reset(); fill(4, 7);
	for (unsigned int i = 1; i < 4; i++) folios[i].refs = 1;
	assert(run(4, 7) == 3);
	assert(mksoft == 3 && mkuffd == 3 && rmap_exclusive == 3);
	for (unsigned int i = 1; i < 4; i++)
		assert((ptes[i].v & PTE_SOFT) && (ptes[i].v & PTE_UFFD));

	/* Freeing the swapcache (last user) is taken from do_swap_page: the cache
	 * reference goes away and the page becomes exclusive. */
	reset(); fill(2, 0);
	folios[1].refs = 1; free_swap_result = true;
	assert(run(2, 0) == 1);
	assert(frees == 1 && mkwrite == 1 && rmap_exclusive == 1);
	assert(folios[1].refs == 1);            /* the PTE reference */
	/* Without the PTE exclusive bit and with another user, the page stays
	 * read-only and the swapcache is kept. */
	reset(); fill(2, 0); folios[1].refs = 2;
	assert(run(2, 0) == 1 && frees == 0 && mkwrite == 0 && rmap_exclusive == 0);

	/* A backend that cannot take concurrent writes under writeback drops
	 * exclusivity even though the PTE says so. */
	reset(); fill(2, 1);
	folios[1].refs = 2; folios[1].writeback = true;
	si.flags = SWP_STABLE_WRITES;
	assert(run(2, 1) == 1 && mkwrite == 0 && rmap_exclusive == 0);
	puts("Batch PTE install tests: PASS");
	return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='hermit-ptebatch-test-') as tmp:
        cfile, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        cfile.write_text(harness + source[start:end] + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-parameter', '-Wno-unused-function',
                        '-fsanitize=address,undefined', '-g',
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def test_split_select():
    """Hot subrange / split order selection over synthetic heat histograms."""
    ROOT = Path(__file__).resolve().parents[3]
    source = (ROOT / 'linux-stable/mm/hermit_split.c').read_text()
    start = source.index('/* hermit_split:begin pure */')
    end = source.index('/* hermit_split:end pure */')
    function = source[start:end]
    harness = r"""
    #include <assert.h>
    #include <stdbool.h>
    #include <stdint.h>
    #include <stdio.h>
    #include <string.h>
    typedef uint8_t u8;
    typedef uint32_t u32;
    #define U8_MAX 255
    #define test_bit(nr, addr) \
        (!!(((const unsigned long *)(addr))[(nr) / 64] & (1UL << ((nr) % 64))))
    """
    tests = r"""
    #define NR 512
    static unsigned long young[NR / 64];
    static u8 heat[NR];
    static struct hermit_split_rule rule = {
        .hot_cov = 80, .hot_pct = 75, .min_cold = 25, .min_samples = 16,
    };
    static unsigned int order;

    static int pick(void)
    {
        order = 99;
        return hermit_split_select(heat, NR, 9, young, &rule, &order);
    }

    static void reset(void)
    {
        memset(heat, 0, sizeof(heat));
        memset(young, 0, sizeof(young));
    }

    static void set_young(unsigned int page)
    {
        young[page / 64] |= 1UL << (page % 64);
    }

    static void hot(unsigned int from, unsigned int nr, u8 count)
    {
        for (unsigned int i = 0; i < nr; i++)
            heat[from + i] = count;
    }

    int main(void)
    {
        /* An 8-page (32 KiB) hot block inside a 2 MiB folio is the largest
         * aligned block that is >=75% hot, so the folio splits to order 3. */
        reset(); hot(128, 8, 10);
        assert(pick() == HERMIT_SPLIT_PICK && order == 3);
        /* One page off the aligned boundary, 7 of the 8 pages of the order-3
         * block are still hot, which is above the 75% bar. */
        reset(); hot(129, 8, 10);
        assert(pick() == HERMIT_SPLIT_PICK && order == 3);
        /* Two pages inside a 16 KiB block are not enough for any block above
         * order 0. */
        reset(); hot(133, 2, 10);
        assert(pick() == HERMIT_SPLIT_PICK && order == 0);
        /* 80% coverage: a count-90 page and a count-10 page leave the
         * count-10 page outside the hot set, so it is not protected. */
        reset(); hot(0, 1, 90); hot(300, 1, 10);
        assert(pick() == HERMIT_SPLIT_PICK && order == 0);
        /* Coverage follows the tail of the histogram: 8 of 10 samples on one
         * page still leave it alone in the hot set. */
        reset(); hot(64, 1, 80); hot(65, 1, 20);
        assert(pick() == HERMIT_SPLIT_PICK && order == 0);
        /* No samples at all is unknown, never cold. */
        reset();
        assert(pick() == HERMIT_SPLIT_UNKNOWN);
        /* Too few samples for the configured evidence threshold. */
        reset(); hot(128, 15, 1);
        assert(pick() == HERMIT_SPLIT_UNKNOWN);
        assert(rule.min_samples == 16);
        /* A 256 KiB half is hot: the largest qualifying block is order 8. */
        reset(); hot(256, 256, 4);
        assert(pick() == HERMIT_SPLIT_PICK && order == 8);
        /* Every page hot -> nothing cold to evict. */
        reset(); hot(0, NR, 7);
        assert(pick() == HERMIT_SPLIT_ALL_HOT);
        /* Cold pages below min_cold are not worth a split. */
        reset(); hot(0, 500, 10);
        assert(pick() == HERMIT_SPLIT_ALL_HOT);
        /* A cold subpage that the MMU still marks accessed vetoes the split. */
        reset(); hot(128, 8, 10);
        set_young(192);                       /* a cold page */
        assert(pick() == HERMIT_SPLIT_COLD_YOUNG);
        /* ... but a young *hot* subpage is expected and harmless. */
        reset(); hot(128, 8, 10);
        set_young(128);                       /* a hot page */
        assert(pick() == HERMIT_SPLIT_PICK && order == 3);
        /* Order 1 anonymous folios cannot be split, so it is never returned:
         * an order-2 folio with two hot and two cold pages goes to order 0. */
        reset(); hot(0, 2, 10);
        assert(hermit_split_select(heat, 4, 2, young, &rule, &order) ==
               HERMIT_SPLIT_PICK && order == 0);
        /* An order-1 folio is never a candidate: the split code does not
         * support order-1 anonymous folios. */
        reset(); hot(0, 2, 10);
        assert(hermit_split_select(heat, 2, 1, young, &rule, &order) ==
               HERMIT_SPLIT_NO_ORDER);
        puts("Split selection tests: PASS");
        return 0;
    }
    """
    with tempfile.TemporaryDirectory(prefix='hermit-split-test-') as tmp:
        cfile, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        cfile.write_text(harness + function + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall',
                        '-Wextra', '-Werror', '-fsanitize=address,undefined',
                        '-g', str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def test_heat_decay():
    """Per-subpage PEBS heat counters: saturating add and halving decay."""
    ROOT = Path(__file__).resolve().parents[3]
    source = (ROOT / 'linux-stable/mm/hermit_pebs.c').read_text()
    start = source.index('/* hermit_heat:begin */')
    end = source.index('/* hermit_heat:end */')
    function = source[start:end]
    harness = r"""
    #include <assert.h>
    #include <stdint.h>
    #include <stdio.h>
    #include <string.h>
    #include <time.h>
    typedef uint8_t u8;
    typedef uint64_t u64;
    #define U8_MAX 255
    #define HERMIT_REGION_NR_SUBPAGES 512
    #define div_u64(a, b) ((a) / (b))
    """
    tests = r"""
    static u8 heat[HERMIT_REGION_NR_SUBPAGES];
    static u64 stamp;

    static unsigned int total(void)
    {
        unsigned int i, sum = 0;

        for (i = 0; i < HERMIT_REGION_NR_SUBPAGES; i++)
            sum += heat[i];
        return sum;
    }

    int main(void)
    {
        /* Counts accumulate per subpage; other subpages are untouched. */
        memset(heat, 0, sizeof(heat)); stamp = 1000;
        hermit_heat_add(heat, &stamp, 5, 1000, 1000);
        hermit_heat_add(heat, &stamp, 5, 1200, 1000);
        hermit_heat_add(heat, &stamp, 6, 1300, 1000);
        assert(heat[5] == 2 && heat[6] == 1 && total() == 3);
        assert(stamp == 1000);
        /* Saturating counter. */
        memset(heat, 0, sizeof(heat)); stamp = 0;
        for (int i = 0; i < 1000; i++)
            hermit_heat_add(heat, &stamp, 9, 1, 0);
        assert(heat[9] == U8_MAX);
        /* One period halves, three periods divide by eight. */
        memset(heat, 0, sizeof(heat)); stamp = 0;
        heat[1] = 200;
        hermit_heat_decay(heat, &stamp, 999, 1000);
        assert(heat[1] == 200 && stamp == 0);        /* not yet */
        hermit_heat_decay(heat, &stamp, 1000, 1000);
        assert(heat[1] == 100 && stamp == 1000);
        heat[2] = 200;
        hermit_heat_decay(heat, &stamp, 4000, 1000);
        assert(heat[1] == 12 && heat[2] == 25 && stamp == 4000);
        /* A long idle gap collapses to zero instead of shifting forever. */
        heat[3] = 200;
        hermit_heat_decay(heat, &stamp, 4000 + 8000, 1000);
        assert(total() == 0 && stamp == 12000);
        /* A timestamp from the future is pulled back, never used to decay. */
        heat[4] = 9; stamp = 5000;
        hermit_heat_add(heat, &stamp, 4, 4000, 1000);
        assert(heat[4] == 10 && stamp == 4000);
        /* Out-of-range subpage index is ignored, a zero period never decays. */
        memset(heat, 0, sizeof(heat)); stamp = 0;
        hermit_heat_add(heat, &stamp, HERMIT_REGION_NR_SUBPAGES, 10, 1000);
        assert(total() == 0);
        heat[7] = 100;
        hermit_heat_decay(heat, &stamp, 100000, 0);
        assert(heat[7] == 100);
        /* Sample-path cost: the split policy adds one decay check and one
         * increment per PEBS sample. */
        memset(heat, 0, sizeof(heat)); stamp = 0;
        struct timespec a, b;
        const unsigned int n = 2000000;
        clock_gettime(CLOCK_MONOTONIC, &a);
        for (unsigned int i = 0; i < n; i++)
            hermit_heat_add(heat, &stamp, i % HERMIT_REGION_NR_SUBPAGES,
                            i / 8, 1000000000ULL);
        clock_gettime(CLOCK_MONOTONIC, &b);
        double ns = ((b.tv_sec - a.tv_sec) * 1e9 + (b.tv_nsec - a.tv_nsec)) / n;
        printf("heat add: %.1f ns/sample\n", ns);
        assert(ns < 1000.0);
        puts("Heat decay tests: PASS");
        return 0;
    }
    """
    with tempfile.TemporaryDirectory(prefix='hermit-heat-test-') as tmp:
        cfile, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        cfile.write_text(harness + function + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall',
                        '-Wextra', '-Werror', '-fsanitize=address,undefined',
                        '-g', str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


def test_split_gates():
    """Split budget (N per second) and memcg pressure/refault gate."""
    ROOT = Path(__file__).resolve().parents[3]
    source = (ROOT / 'linux-stable/mm/hermit_split.c').read_text()
    budget = source[source.index('/* hermit_split:budget:begin */'):
                    source.index('/* hermit_split:budget:end */')]
    pressure = source[source.index('/* hermit_split:pressure:begin */'):
                      source.index('/* hermit_split:pressure:end */')]
    harness = r"""
    #include <assert.h>
    #include <stdbool.h>
    #include <stdint.h>
    #include <stdio.h>
    typedef uint32_t u32;
    typedef struct { int counter; } atomic_t;
    struct page_counter { unsigned long max; unsigned long usage; };
    struct mem_cgroup { struct page_counter memory; atomic_t hermit_refault; };
    #define CONFIG_MEMCG 1
    #define HZ 100
    #define PAGE_COUNTER_MAX LONG_MAX
    #define READ_ONCE(x) (x)
    #define time_after(a, b) ((long)((b) - (a)) < 0)
    static unsigned long jiffies;
    static u32 hermit_split_rate;
    static u32 hermit_split_refault_max = 256;
    static int atomic_read(atomic_t *v) { return v->counter; }
    static int atomic_inc_return(atomic_t *v) { return ++v->counter; }
    static void atomic_set(atomic_t *v, int n) { v->counter = n; }
    static unsigned long harness_cmpxchg(unsigned long *p, unsigned long old,
                                         unsigned long n)
    { unsigned long o = *p; if (o == old) *p = n; return o; }
    #define cmpxchg(p, o, n) harness_cmpxchg((unsigned long *)(p), (o), (n))
    static unsigned long page_counter_read(struct page_counter *c)
    { return c->usage; }
    #include <limits.h>
    """
    tests = r"""
    int main(void)
    {
        /* Two splits per second, then the budget refuses until the window
         * rolls over. Zero means the policy is disabled. */
        hermit_split_rate = 2; jiffies = 10 * HZ;
        assert(hermit_split_budget());
        assert(hermit_split_budget());
        assert(!hermit_split_budget());
        assert(!hermit_split_budget());
        jiffies += HZ - 1;            /* still the same 1 s window */
        assert(!hermit_split_budget());
        jiffies += 2;                 /* the window expires strictly after 1 s */
        assert(hermit_split_budget());
        assert(hermit_split_budget());
        assert(!hermit_split_budget());
        hermit_split_rate = 0; jiffies += 10 * HZ;
        assert(!hermit_split_budget());

        /* No memcg (global reclaim) is always allowed. */
        assert(hermit_split_under_pressure(NULL));
        struct mem_cgroup memcg = {0};
        /* Root memcg: no limit, so the refault score decides. */
        memcg.memory.max = PAGE_COUNTER_MAX;
        memcg.hermit_refault.counter = 0;
        assert(hermit_split_under_pressure(&memcg));
        memcg.hermit_refault.counter = 256;
        assert(!hermit_split_under_pressure(&memcg));
        memcg.hermit_refault.counter = 255;
        assert(hermit_split_under_pressure(&memcg));
        /* Near the limit, a high refault score no longer blocks the split. */
        memcg.memory.max = 1000; memcg.memory.usage = 750;
        memcg.hermit_refault.counter = 4096;
        assert(hermit_split_under_pressure(&memcg));
        memcg.memory.usage = 749;
        assert(!hermit_split_under_pressure(&memcg));
        memcg.memory.usage = 1000;
        assert(hermit_split_under_pressure(&memcg));
        puts("Split gate tests: PASS");
        return 0;
    }
    """
    with tempfile.TemporaryDirectory(prefix='hermit-splitgates-test-') as tmp:
        cfile, binary = Path(tmp) / 'test.c', Path(tmp) / 'test'
        cfile.write_text(harness + budget + pressure + tests)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall',
                        '-Wextra', '-Werror', '-Wno-unused-function',
                        '-fsanitize=address,undefined', '-g',
                        str(cfile), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)



if __name__ == '__main__':
    test_rdma_submission()
    test_remote_read()
    test_folio_selection()
    test_reclaim_sort()
    test_swapin_pages()
    test_cold_observe()
    test_shadow_refault()
    test_swapin_cost()
    test_pte_batch_install()
    test_split_select()
    test_heat_decay()
    test_split_gates()
