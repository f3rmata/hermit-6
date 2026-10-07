#!/usr/bin/env python3
"""Check production remote-read dispatch with backend-loss fault injection."""
import os
from pathlib import Path
import subprocess
import tempfile

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
