#!/usr/bin/env python3
"""Compile the production submission function with a deterministic fake transport.

No RDMA hardware or kernel module is needed. Tests request sizes/offsets and
partial-submission draining; this does not validate DMA or completion concurrency.
"""
import os
from pathlib import Path
import subprocess
import tempfile

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
