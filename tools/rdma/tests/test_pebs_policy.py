#!/usr/bin/env python3
"""Execute the production PEBS window/table/model code, without a PMU or boot.

Only allocation, locks and kernel services are shimmed. Lists and rbtrees use
Linux's tools implementations; the model and table functions come from mm/.
"""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[3]
KERNEL = ROOT / 'linux-stable'


def without_includes(text):
    return '\n'.join(line for line in text.splitlines() if not line.startswith('#include')) + '\n'


def section(text, begin, end):
    return text[text.index(begin):text.index(end, text.index(begin))]


def test_policy():
    source = (KERNEL / 'mm/hermit_pebs.c').read_text()
    public = without_includes((KERNEL / 'include/linux/hermit_pebs.h').read_text())
    core = without_includes((KERNEL / 'mm/hermit_pebs_policy.h').read_text())
    setup = r'''
#include <assert.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <limits.h>
#include <linux/list.h>
#include <linux/rbtree.h>
#include <linux/bitops.h>
#define PAGE_SHIFT 12
#define PAGE_SIZE 4096UL
#define NSEC_PER_MSEC 1000000ULL
#define U32_MAX UINT32_MAX
#define U64_MAX UINT64_MAX
#define GFP_NOWAIT 0
#define GFP_ATOMIC 0
#define __GFP_NOWARN 0
#define div64_u64(a, b) ((u64)(a) / (b))
#define DIV_ROUND_UP_ULL(a, b) (((u64)(a) + (b) - 1) / (b))
#define cmpxchg(p, old, value) __sync_val_compare_and_swap(p, old, value)
typedef int spinlock_t;
#define spin_lock_init(p) (*(p) = 0)
#define spin_lock(p) assert(!(*(p))++)
#define spin_unlock(p) assert((*(p))-- == 1)
#define atomic_long_inc(p) (++(p)->counter)
static bool fail_alloc;
static unsigned int allocations;
static void *kzalloc(size_t n, int flags)
{ (void)flags; if (fail_alloc) return NULL; allocations++; return calloc(1, n); }
static void kfree(void *p) { if (p) allocations--; free(p); }
static unsigned long hash_long(unsigned long x, unsigned int bits)
{ return (x * 0x61c8864680b583ebUL) >> (64 - bits); }
static u64 clock_ns;
static u64 ktime_get_ns(void) { return clock_ns; }
struct hermit_pebs_state;
struct mm_struct { struct hermit_pebs_state *hermit_pebs; bool hermit_pebs_enabled; };
struct vm_area_struct { struct mm_struct *vm_mm; };
struct vm_fault {
    struct vm_area_struct *vma;
    unsigned int hermit_policy_order;
    bool hermit_policy_used;
};
struct perf_event_header { u32 type; u16 misc, size; };
struct perf_buffer { unsigned int nr_pages; void *data_pages[2]; };
#define page_order(rb) 0
static unsigned long transport_mask = 0x3fd;
static unsigned long hermit_backend_effective_order_mask(void) { return transport_mask; }
'''
    tests = r'''
static const unsigned long pc_a = 0x1000, pc_b = 0x2000;
static const u64 ms = NSEC_PER_MSEC;
static unsigned long address(unsigned int region, unsigned int page)
{ return ((unsigned long)region << HERMIT_REGION_SHIFT) + page * PAGE_SIZE; }

static void ring_tests(void)
{
    unsigned char data[2][PAGE_SIZE];
    struct perf_buffer rb = { .nr_pages = 2, .data_pages = {data[0], data[1]} };
    struct hermit_pebs_sample sample;
    /* Perf ABI: header, IP, TID, TIME, ADDR, independent of parser layout. */
    u64 wire[5] = {40ULL << 48, 0x1234, (7ULL << 32) | 7, 123456789, 0xabcdef};
    const unsigned int starts[] = {0, PAGE_SIZE - 8, 2 * PAGE_SIZE - 8};
    assert(sizeof(sample) == 40);
    for (unsigned int i = 0; i < ARRAY_SIZE(starts); i++) {
        unsigned char *bytes = (unsigned char *)wire;
        for (unsigned int j = 0; j < sizeof(wire); j++) {
            unsigned int offset = (starts[i] + j) % (2 * PAGE_SIZE);
            data[offset / PAGE_SIZE][offset % PAGE_SIZE] = bytes[j];
        }
        hermit_ring_copy(&rb, starts[i], &sample, sizeof(sample));
        assert(sample.header.size == 40 && sample.ip == 0x1234);
        assert(sample.pid == 7 && sample.tid == 7);
        assert(sample.time == 123456789 && sample.addr == 0xabcdef);
    }
}

static void window_tests(void)
{
    struct hermit_footprint_window w;
    hermit_window_start(&w, address(1, 7), pc_a, 100, 10, 1);
    assert(hermit_window_sample(&w, address(1, 8), 109, 115, false));
    assert(hermit_window_sample(&w, address(1, 9), 101, 116, true));
    assert(!hermit_window_sample(&w, address(1, 10), 110, 116, false));
    assert(!hermit_window_sample(&w, address(1, 10), 99, 116, false));
    assert(!hermit_window_sample(&w, address(1, 10), 109, 120, false));
    assert(w.loads == 1 && w.load[0] == ((1ULL << 7) | (1ULL << 8)));
    assert(w.store[0] == (1ULL << 9));
    hermit_window_start(&w, address(1, 11), pc_b, 110, 10, 1);
    assert(w.loads == 0 && w.load[0] == (1ULL << 11) && !w.store[0]);
}

static void model_tests(void)
{
    struct hermit_pc_history h = { .last_order = -1 };
    struct hermit_cost_params p = hermit_cost_params_read();
    bool confident;
    u64 cost[10] = {};
    for (unsigned int i = 0; i < 8; i++) {
        h.slot[i].end_ns = 1;
        h.slot[i].region = i;
        h.slot[i].loads = 8;
        h.slot[i].load[0] = 1;
    }
#define CHOOSE(off, mask, wrs) hermit_footprint_choose(&h, off, mask, wrs, 2, &p, cost, &confident)
    assert(CHOOSE(0, 0x3fd, 0x3fd) == 0 && confident);
    /* Dense 64 KiB, including nonzero fault offsets and mask holes. */
    for (unsigned int i = 0; i < 8; i++) h.slot[i].load[0] = 0xffff;
    assert(CHOOSE(7, 0x3fd, 0x3fd) == 4);
    assert(CHOOSE(7, 0xd, 0x3fd) == 3);
    assert(CHOOSE(100, 0x3fd, 0x3fd) == 7); /* interval includes hot prefix */
    for (unsigned int i = 0; i < 8; i++)
        memset(h.slot[i].load, 0xff, sizeof(h.slot[i].load));
    assert(CHOOSE(0, 0x3fd, 0x3fd) == 9);
    assert(CHOOSE(511, BIT(0) | BIT(4), 0x3fd) == 4);
    /* Actual read splitting: large unsupported orders become many WRs. */
    p.wr_ns = 100000;
    assert(CHOOSE(0, 0x3fd, BIT(0) | BIT(4)) == 4);
    p.wr_ns = 0;
    /* No training from four repetitions of the same region. */
    for (unsigned int i = 0; i < 8; i++) h.slot[i].region = 1;
    assert(CHOOSE(0, 0x3fd, 0x3fd) == 0 && !confident);
    for (unsigned int i = 0; i < 8; i++) h.slot[i].region = i;
    p.min_samples = 65;
    assert(CHOOSE(0, 0x3fd, 0x3fd) == 0 && !confident);
    p.min_samples = 32; p.bw_mibps = 0;
    assert(CHOOSE(0, 0x3fd, 0x3fd) == 0 && !confident);
    p.bw_mibps = 12288; p.ttl_ns = 1;
    assert(CHOOSE(0, 0x3fd, 0x3fd) == 0 && !confident);
    p = hermit_cost_params_read();
    /* Previous extent/order must not bypass missing evidence. */
    h.last_order = 9; p.min_samples = 1000;
    assert(CHOOSE(0, 0x3fd, 0x3fd) == 0);
    p = hermit_cost_params_read(); h.last_order = -1;
    p.read_ns = 0; p.bw_mibps = 1;
    assert(CHOOSE(0, 0x3fd, 0x3fd) == 0); /* exact per-page tie */
    h.last_order = 9;
    assert(CHOOSE(0, 0x3fd, 0x3fd) == 0); /* tie beats hysteresis */
    p.read_ns = 1; h.last_order = 4;
    assert(CHOOSE(0, 0x3fd, 0x3fd) == 4); /* <10% improvement */
    assert(CHOOSE(0, BIT(0), 0x3fd) == 0); /* previous order illegal */
    p.read_ns = U32_MAX; p.wr_ns = U32_MAX; p.bw_mibps = 1;
    assert(CHOOSE(0, 0x3fd, BIT(0)) >= 0); /* arithmetic bounds */
    assert(CHOOSE(512, 0x3fd, 0x3fd) == 0 && !confident);
#undef CHOOSE
}

static void train(struct mm_struct *mm, unsigned int region, unsigned int pages,
                  unsigned long pc)
{
    clock_ns += 30 * ms;
    hermit_pebs_fault(mm, address(region, 0), pc, clock_ns);
    for (unsigned int i = 0; i < pages; i++)
        hermit_region_sample(mm, address(region, i), clock_ns + 1, false);
    /* count is independent of number of unique touched pages */
    for (unsigned int i = 0; i < 8; i++)
        hermit_region_sample(mm, address(region, 0), clock_ns + 1, false);
    clock_ns += 20 * ms;
    (void)hermit_swapin_order(mm, address(99, 0), pc, 0x3fd);
}

static void table_tests(void)
{
    struct mm_struct a = {}, b = {}, other = {};
    struct hermit_pebs_observation snapshot;
    struct hermit_pc_entry *p;
    clock_ns = ms;
    hermit_pebs_epoch = clock_ns;
    hermit_pebs_enabled = 1;
    hermit_pebs_mode = HERMIT_PEBS_MODE_POLICY;
    assert(hermit_swapin_order(&a, address(1, 0), pc_a, 0x3fd) == -1);
    hermit_pebs_mm_enable(&a, true);
    assert(hermit_swapin_order(&a, address(1, 0), pc_a, 0x3fd) == 0);
    assert(hermit_swapin_order(&a, address(1, 0), 0, 0x3fd) == 0);
    fail_alloc = true;
    hermit_pebs_fault(&a, address(1, 0), pc_a, clock_ns);
    assert(!a.hermit_pebs);
    fail_alloc = false;
    for (unsigned int i = 1; i <= 4; i++) train(&a, i, 16, pc_a);
    assert(hermit_swapin_order(&a, address(99, 0), pc_a, 0x3fd) == 4);
    assert(hermit_swapin_order(&a, address(99, 1), pc_a, 0x3fd) == 0);
    assert(hermit_swapin_order(&a, address(99, 0), pc_b, 0x3fd) == 0);
    hermit_pebs_all = 1;
    assert(hermit_swapin_order(&b, address(99, 0), pc_a, 0x3fd) == 0);
    for (unsigned int i = 1; i <= 4; i++) train(&b, i, 512, pc_a);
    assert(hermit_swapin_order(&b, address(99, 0), pc_a, 0x3fd) == 9);
    assert(hermit_swapin_order(&a, address(99, 0), pc_a, 0x3fd) == 4);
    /* Force applies only to legal candidates, even without mm tracking. */
    hermit_pebs_all = 0; hermit_pebs_force_order = U32_MAX;
    assert(hermit_swapin_order(&other, 0, 0, BIT(4)) == 4);
    hermit_pebs_force_order = 0;
    assert(hermit_swapin_order(&other, 0, 0, BIT(4)) == -1);
    /* Active region: old bits cannot live forever. Adjacent windows accept
     * out-of-order samples only within their respective event-time range. */
    clock_ns += 30 * ms;
    u64 start = clock_ns;
    hermit_pebs_fault(&a, address(20, 0), pc_a, start);
    hermit_region_sample(&a, address(20, 15), start + ms, false);
    clock_ns = start + 10 * ms;
    hermit_pebs_fault(&a, address(20, 0), pc_b, clock_ns);
    hermit_region_sample(&a, address(20, 31), start + 9 * ms, false);
    hermit_region_sample(&a, address(20, 3), start + 10 * ms, true);
    assert(hermit_pebs_snapshot(&a, address(20, 0), &snapshot));
    assert(snapshot.load[0] == 1 && snapshot.store[0] == 8);
    clock_ns = start + 20 * ms;
    hermit_region_sample(&a, address(20, 50), start + ms, false);
    p = hermit_pc_get(a.hermit_pebs, pc_a, 0, false);
    assert(p);
    unsigned int last = (p->history.next + 7) % 8;
    assert(p->history.slot[last].load[0] == (1 | BIT(15) | BIT(31)));
    assert(p->history.slot[last].loads == 2);
    /* Session reset, disabling/re-enabling and TTL all invalidate history. */
    hermit_pebs_epoch = ++clock_ns;
    assert(hermit_swapin_order(&a, address(99, 0), pc_a, 0x3fd) == 0);
    for (unsigned int i = 1; i <= 4; i++) train(&a, i, 16, pc_a);
    hermit_pebs_mm_enable(&a, false);
    assert(hermit_swapin_order(&a, 0, pc_a, 0x3fd) == -1);
    hermit_pebs_mm_enable(&a, true);
    assert(hermit_swapin_order(&a, 0, pc_a, 0x3fd) == 0);
    for (unsigned int i = 1; i <= 4; i++) train(&a, i, 16, pc_a);
    clock_ns += 1000 * ms;
    assert(hermit_swapin_order(&a, 0, pc_a, 0x3fd) == 0);
    hermit_pebs_mm_exit(&a); hermit_pebs_mm_exit(&b);
    /* Tiny capacities exercise rb removal and LRU reuse of pending windows. */
    hermit_pebs_region_max = 2; hermit_pebs_pc_max = 1;
    for (unsigned int i = 0; i < 32; i++) {
        train(&a, i, 16, i % 2 ? pc_a : pc_b);
        assert(a.hermit_pebs->nr_regions <= 2 && a.hermit_pebs->nr_pcs <= 1);
    }
    for (unsigned int i = 40; i < 50; i++)
        hermit_pebs_fault(&a, address(i, 0), pc_a, ++clock_ns);
    clock_ns += 20 * ms;
    hermit_windows_finish(a.hermit_pebs, clock_ns);
    assert(!rb_first_cached(&a.hermit_pebs->pending));
    hermit_pebs_mm_exit(&a);
    assert(allocations == 0);
}

int main(void)
{
    ring_tests(); window_tests(); model_tests(); table_tests();
    puts("PEBS production window/table/model tests: PASS");
}
'''
    code = (setup + public + core +
            section(source, 'struct hermit_pebs_sample {', 'static struct task_struct *hermit_pebsd;') +
            section(source, 'static void hermit_ring_copy(', 'static int hermit_pebs_read_events(') +
            section(source, 'u32 hermit_pebs_enabled;', '/* ---------- sampling period') +
            section(source, '/* global counters */', '/* ---------- sampling ---------- */') +
            section(source, '/* ---------- swap-in policy', '/* ---------- syscalls') +
            section(source, '/* ---------- mm history lifetime', '/* ---------- debugfs') + tests)
    with tempfile.TemporaryDirectory(prefix='hermit-pebs-test-') as tmp:
        cfile, binary = Path(tmp) / 'policy.c', Path(tmp) / 'policy'
        cfile.write_text(code)
        subprocess.run([os.environ.get('CC', 'cc'), '-std=gnu11', '-Wall', '-Wextra',
                        '-Werror', '-Wno-unused-variable', '-Wno-unused-function',
                        '-Wno-unused-parameter',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-g',
                        '-I', str(KERNEL / 'tools/include'),
                        str(cfile), str(KERNEL / 'tools/lib/rbtree.c'),
                        str(KERNEL / 'tools/lib/hweight.c'), '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    test_policy()
