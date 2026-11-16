#!/usr/bin/env python3
"""Execute the production PEBS window/table/predictor code, without a PMU or boot.

Only allocation, locks and kernel services are shimmed. Lists and rbtrees use
Linux's tools implementations; the predictor and table functions come from mm/.
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
#define GFP_NOWAIT 0
#define GFP_ATOMIC 0
#define __GFP_NOWARN 0
#define cmpxchg(p, old, value) __sync_val_compare_and_swap(p, old, value)
typedef int spinlock_t;
#define spin_lock_init(p) (*(p) = 0)
#define spin_lock(p) assert(!(*(p))++)
#define spin_unlock(p) assert((*(p))-- == 1)
#define atomic_long_inc(p) (++(p)->counter)
#define atomic_long_add(n, p) ((p)->counter += (n))
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
typedef struct { long counter; } atomic64_t;
#define atomic64_inc(p) (++(p)->counter)
#define atomic64_read(p) ((p)->counter)
struct mm_struct {
    struct hermit_pebs_state *hermit_pebs; bool hermit_pebs_enabled;
    atomic64_t hermit_vma_generation;
};
#define HERMIT_SWAPIN_MAX 64
static bool hermit_pebs_tracks(struct mm_struct *mm);
static bool hermit_policy_active(struct mm_struct *mm);
struct vm_area_struct { struct mm_struct *vm_mm; unsigned long vm_start, vm_end; };
struct vm_fault {
    struct vm_area_struct *vma;
    unsigned long address, hermit_fault_pc;
};
struct perf_event_header { u32 type; u16 misc, size; };
struct perf_buffer { unsigned int nr_pages; void *data_pages[2]; };
#define page_order(rb) 0
'''
    # Same predicate as mm/hermit_policy.c, defined after the PEBS globals.
    active = r'''
static bool hermit_policy_active(struct mm_struct *mm)
{
    return hermit_pebs_enabled && hermit_pebs_mode == HERMIT_PEBS_MODE_POLICY &&
           (hermit_pebs_all || mm->hermit_pebs_enabled);
}
'''
    tests = r'''
static const unsigned long pc_a = 0x1000, pc_b = 0x2000;
static const u64 ms = NSEC_PER_MSEC;
static unsigned long address(unsigned int region, unsigned int page)
{ return ((unsigned long)region << HERMIT_REGION_SHIFT) + page * PAGE_SIZE; }

static unsigned int predict(struct mm_struct *mm, unsigned long addr, unsigned long pc)
{
    struct vm_area_struct vma = { .vm_mm = mm, .vm_end = ULONG_MAX };
    struct vm_fault vmf = { .vma = &vma, .address = addr, .hermit_fault_pc = pc };
    return hermit_pebs_predict_pages(&vmf);
}

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
    (void)predict(mm, address(99, 0), pc);
}

static void table_tests(void)
{
    struct mm_struct a = {}, b = {};
    struct hermit_pebs_observation snapshot;
    struct hermit_pc_entry *p;
    clock_ns = ms;
    hermit_pebs_epoch = clock_ns;
    hermit_pebs_enabled = 1;
    hermit_pebs_mode = HERMIT_PEBS_MODE_POLICY;
    assert(predict(&a, address(1, 0), pc_a) == 1);
    hermit_pebs_mm_enable(&a, true);
    assert(predict(&a, address(1, 0), pc_a) == 1);
    assert(predict(&a, address(1, 0), 0) == 1);
    hermit_pebs_mm_exit(&a);
    fail_alloc = true;
    assert(predict(&a, address(1, 0), pc_a) == 1);
    hermit_pebs_fault(&a, address(1, 0), pc_a, clock_ns);
    assert(!a.hermit_pebs);
    fail_alloc = false;
    for (unsigned int i = 1; i <= 4; i++) train(&a, i, 16, pc_a);
    assert(predict(&a, address(99, 0), pc_a) == 16);
    assert(predict(&a, address(99, 1), pc_a) == 1);
    assert(predict(&a, address(99, 0), pc_b) == 1);
    hermit_pebs_all = 1;
    assert(predict(&b, address(99, 0), pc_a) == 1);
    for (unsigned int i = 1; i <= 4; i++) train(&b, i, 512, pc_a);
    assert(predict(&b, address(99, 0), pc_a) == HERMIT_SWAPIN_MAX);
    assert(predict(&a, address(99, 0), pc_a) == 16);
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
    hermit_pebs_all = 0;
    /* Session reset, disabling/re-enabling and TTL all invalidate history. */
    hermit_pebs_epoch = ++clock_ns;
    assert(predict(&a, address(99, 0), pc_a) == 1);
    for (unsigned int i = 1; i <= 4; i++) train(&a, i, 16, pc_a);
    hermit_pebs_mm_enable(&a, false);
    assert(predict(&a, 0, pc_a) == 1);
    hermit_pebs_mm_enable(&a, true);
    assert(predict(&a, 0, pc_a) == 1);
    for (unsigned int i = 1; i <= 4; i++) train(&a, i, 16, pc_a);
    clock_ns += 1000 * ms;
    assert(predict(&a, 0, pc_a) == 1);
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

/* 4 KiB predictor: footprints are shared across threads of one mm, empty
 * windows never evict evidence, repeated regions count once, and gates are
 * reported through the new phase counters. */
static void predict_tests(void)
{
    struct mm_struct mm = { .hermit_pebs_enabled = true };
    hermit_pebs_mode = HERMIT_PEBS_MODE_POLICY; hermit_pebs_enabled = 1;
    hermit_pebs_all = 0; hermit_pebs_pc_max = 4096;
    hermit_pebs_region_max = HERMIT_PEBS_MAX_REGIONS;
    clock_ns += ms; hermit_pebs_epoch = clock_ns;
    long calls = hermit_policy_counters[HPC_PRED_CALLS].counter;
    assert(predict(&mm, address(99, 0), pc_a) == 1);
    assert(hermit_policy_counters[HPC_PRED_CALLS].counter == calls + 1);
    /* Cross-region footprints are shared within this mm. */
    train(&mm, 1, 16, pc_a);
    train(&mm, 2, 16, pc_a);
    long extended = hermit_policy_counters[HPC_PRED_EXTENDED].counter;
    assert(predict(&mm, address(99, 0), pc_a) == 16);
    assert(hermit_policy_counters[HPC_PRED_EXTENDED].counter == extended + 1);
    /* Empty windows (demand only, no PEBS load) do not push out evidence. */
    for (unsigned int i = 0; i < 16; i++) {
        clock_ns += 30 * ms;
        hermit_pebs_fault(&mm, address(40 + i, 0), pc_a, clock_ns);
        clock_ns += 20 * ms;
        (void)predict(&mm, address(98, 0), pc_a);
    }
    assert(predict(&mm, address(99, 0), pc_a) == 16);
    /* Retraining the same region repeatedly is one region of evidence. */
    struct mm_struct one = { .hermit_pebs_enabled = true };
    for (unsigned int i = 0; i < 4; i++) train(&one, 5, 16, pc_b);
    long regions = hermit_policy_counters[HPC_PRED_FEW_REGIONS].counter;
    assert(predict(&one, address(99, 0), pc_b) == 1);
    assert(hermit_policy_counters[HPC_PRED_FEW_REGIONS].counter == regions + 1);
    /* Votes stop at the first page without a 75% majority. */
    struct mm_struct gap = { .hermit_pebs_enabled = true };
    train(&gap, 1, 4, pc_b); train(&gap, 2, 4, pc_b); train(&gap, 3, 16, pc_b);
    assert(predict(&gap, address(99, 0), pc_b) == 4);
    /* Insufficient samples and static/off modes never add a speculative page. */
    hermit_pebs_predict_samples = U32_MAX;
    long samples = hermit_policy_counters[HPC_PRED_FEW_SAMPLES].counter;
    assert(predict(&gap, address(99, 0), pc_b) == 1);
    assert(hermit_policy_counters[HPC_PRED_FEW_SAMPLES].counter == samples + 1);
    hermit_pebs_predict_samples = 8;
    hermit_pebs_mode = HERMIT_PEBS_MODE_STATIC;
    assert(predict(&gap, address(99, 0), pc_b) == 1);
    hermit_pebs_mode = HERMIT_PEBS_MODE_POLICY;
    hermit_pebs_enabled = 0;
    assert(predict(&gap, address(99, 0), pc_b) == 1);
    hermit_pebs_enabled = 1;
    hermit_pebs_mm_exit(&mm); hermit_pebs_mm_exit(&one); hermit_pebs_mm_exit(&gap);
    assert(allocations == 0);
    puts("PEBS 4 KiB predictor tests: PASS");
}

static void accounting_tests(void)
{
    struct mm_struct mm = {};
    hermit_pebs_enabled = 1; hermit_pebs_all = 0;
    hermit_pebs_mode = HERMIT_PEBS_MODE_STATIC;
    long before = hermit_actual_orders[4].counter;
    hermit_pebs_account_swapin(&mm, 4);
    assert(hermit_actual_orders[4].counter == before);
    mm.hermit_pebs_enabled = true;
    hermit_pebs_account_swapin(&mm, 4);
    hermit_pebs_account_swapin(&mm, HERMIT_PEBS_MAX_ORDER + 1);
    assert(hermit_actual_orders[4].counter == before + 1);
    assert(!mm.hermit_pebs); /* Counting actual sizes must not create history. */
    /* Phase B reads a 16/32/64 KiB extent as one folio instead of 4 KiB pages.
     * The tiers stay separate, so a run can compare medium_used_orderN with the
     * order-0 it costs on the batched path. */
    for (unsigned int order = 2; order <= 4; order++)
        hermit_pebs_account_swapin(&mm, order);
    assert(hermit_actual_orders[2].counter == 1);
    assert(hermit_actual_orders[3].counter == 1);
    assert(hermit_actual_orders[4].counter == before + 2);
    assert(hermit_actual_orders[0].counter == 0);
    hermit_pebs_enabled = 0;
    hermit_pebs_account_swapin(&mm, 4);
    assert(hermit_actual_orders[4].counter == before + 2);
}

int main(void)
{
    ring_tests(); window_tests(); table_tests(); predict_tests(); accounting_tests();
    puts("PEBS production window/table/predictor tests: PASS");
}
'''
    code = (setup + public + core +
            section(source, 'struct hermit_pebs_sample {', 'static struct task_struct *hermit_pebsd;') +
            section(source, 'static void hermit_ring_copy(', 'static int hermit_pebs_read_events(') +
            section(source, 'u32 hermit_pebs_enabled;', '/* ---------- sampling period') +
            active +
            section(source, '/* global counters */', '/* ---------- sampling ---------- */') +
            section(source, '/* ---------- base-page swap-in prediction', '/* ---------- syscalls') +
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
                        '-o', str(binary)], check=True)
        subprocess.run([str(binary)], check=True)


if __name__ == '__main__':
    test_policy()
