"""Microbench counters and mapping-only folio census.

Compound extent walk adapted from pebs_perf_matrix.py; keep physical folios
separate from allocation requests and actual RDMA WR sizes.
"""
import json
import os
import re
import time
from pathlib import Path

D = Path('/sys/kernel/debug/hermit')
WR = Path('/sys/module/rswap_client/parameters/wr_stats')
THP = Path('/sys/kernel/mm/transparent_hugepage')

def read(p):
    return Path(p).read_text()

def write(p, value):
    Path(p).write_text(str(value) + '\n')

def dump(p, value):
    Path(p).write_text(json.dumps(value, indent=2) + '\n')

def thp_settings():
    return {str(p): re.search(r'\[([^]]+)\]', read(p))[1]
            for p in [THP/'enabled', *sorted(THP.glob('hugepages-*kB/enabled'))]}

def folio_extent(pfn, flags):
    """Resolve compound extent from kpageflags; None means inconsistent snapshot."""
    head, tail = 1 << 15, 1 << 16
    value = flags(pfn)
    if not value & (head | tail):
        return pfn, 1
    first = pfn
    while flags(first) & tail:
        first -= 1
        if first < 0 or pfn-first > 262144:
            return None
    if not flags(first) & head:
        return None
    end = first+1
    while flags(end) & tail:
        end += 1
        if end-first > 262144:
            return None
    count = end-first
    if count < 2 or count & (count-1) or not first <= pfn < end:
        return None
    return first, count


def record_page_sizes(folder, pid, address, length):
    """Read-only, non-atomic resident folio census; never faults pages in."""
    import struct
    start = time.monotonic()
    folder.mkdir()
    maps = read(f'/proc/{pid}/maps')
    (folder/'maps').write_text(maps)
    (folder/'smaps').write_text(read(f'/proc/{pid}/smaps'))
    settings = thp_settings()
    page = os.sysconf('SC_PAGE_SIZE')
    counts = {'resident_mapped_bytes': 0, 'swapped_mapped_bytes': 0,
              'not_present_mapped_bytes': 0, 'unknown_resident_bytes': 0}
    histogram = {}; extents = {}; blocks = {}
    with open(f'/proc/{pid}/pagemap', 'rb', buffering=0) as pm, open('/proc/kpageflags', 'rb', buffering=0) as kf:
        def flags(pfn):
            block, offset = divmod(pfn, 4096)
            if block not in blocks:
                kf.seek(block*4096*8)
                blocks[block] = kf.read(4096*8)
            data = blocks[block]
            if (offset+1)*8 > len(data):
                raise RuntimeError('Short kpageflags read')
            return struct.unpack_from('=Q', data, offset*8)[0]
        for line in maps.splitlines():
            lo, hi = (int(v,16) for v in line.split()[0].split('-'))
            lo, hi = max(lo, address), min(hi, address + length)
            if lo >= hi:
                continue
            pm.seek(lo//page*8)
            for base in range(lo, hi, page*4096):
                n = min(4096, (hi-base)//page)
                data = pm.read(n*8)
                if len(data) != n*8:
                    raise RuntimeError('Process mappings changed or pagemap read incomplete')
                for (entry,) in struct.iter_unpack('=Q', data):
                    if not entry & (1 << 63):
                        counts['swapped_mapped_bytes' if entry & (1 << 62) else 'not_present_mapped_bytes'] += page
                        continue
                    counts['resident_mapped_bytes'] += page
                    pfn = entry & ((1 << 55)-1)
                    if not pfn:
                        counts['unknown_resident_bytes'] += page
                        continue
                    if pfn not in extents:
                        extent = folio_extent(pfn, flags)
                        if extent:
                            first, size = extent
                            for member in range(first, first+size):
                                extents[member] = extent
                        else:
                            extents[pfn] = None
                    extent = extents[pfn]
                    if extent is None:
                        counts['unknown_resident_bytes'] += page
                        continue
                    first, size = extent
                    key = str(size*page//1024)
                    item = histogram.setdefault(key, {'mapped_bytes': 0, 'folios': set()})
                    item['mapped_bytes'] += page
                    item['folios'].add(first)
    covered = (counts['resident_mapped_bytes'] + counts['swapped_mapped_bytes'] +
               counts['not_present_mapped_bytes'])
    if covered != length:
        raise RuntimeError('Benchmark mapping census is incomplete')
    for item in histogram.values():
        item['observed_folios'] = len(item.pop('folios'))
    result = dict(address=address, length=length, method='pagemap+kpageflags-compound-extent-v1', thp_settings=settings,
                  base_page_bytes=page, folio_size_kib=histogram, **counts,
                  elapsed_seconds=time.monotonic()-start,
                  limitations='Non-atomic snapshot restricted to the benchmark mapping. '
                  'Swapped/nonresident pages have no inferred folio size. Concurrent split/reclaim may race. '
                  'This is physical folio size, not malloc/slab size or RDMA WR size.')
    (folder/'page-sizes.json').write_text(json.dumps(result, indent=2)+'\n')
    return result


def table(text):
    lines = text.splitlines()
    names = lines[0].split()[1:]
    result = {}
    for line in lines[1:]:
        cells = line.split()
        if len(cells) != len(names) + 1:
            raise ValueError('Malformed counter table')
        result[cells[0]] = dict(zip(names, map(int, cells[1:])))
    if not result:
        raise ValueError('Empty counter table')
    return result


def snapshot(folder, cg, pid):
    folder.mkdir()
    start = time.monotonic()
    for name in ['order_stats', 'pebs_order_stats', 'effective_order_mask',
                 'remote_order_mask', 'pebs_enabled', 'pebs_mode']:
        write(folder/name, read(D/name).strip())
    write(folder/'wr_stats', read(WR).strip())
    for name in ['memory.current', 'memory.swap.current', 'memory.events', 'memory.stat']:
        write(folder/name, read(cg/name).strip())
    for name in ['stat', 'status']:
        write(folder/('process-' + name), read(f'/proc/{pid}/{name}').strip())
    dump(folder/'perf.json', {p.name: read(p).strip()
                            for p in Path('/proc/sys/kernel').glob('perf_*')})
    write(folder/'vmstat', read('/proc/vmstat').strip())
    dump(folder/'time.json', {'start': start, 'end': time.monotonic()})


def deltas(before, after):
    result = {}
    for filename, key in [('wr_stats', 'wr'), ('order_stats', 'order')]:
        a, b = [table(read(p/filename)) for p in (before, after)]
        if a.keys() != b.keys():
            raise ValueError('Counter orders changed')
        result[key] = {}
        for order, values in b.items():
            result[key][order] = {}
            for name, value in values.items():
                # order_stats size column is metadata, not a counter.
                if name in ('bytes', 'size', 'size_bytes'):
                    if value != a[order][name]:
                        raise ValueError('Order size changed')
                    continue
                delta = value - a[order][name]
                if delta < 0:
                    raise ValueError('Counter reset: ' + filename + '/' + name)
                result[key][order][name] = delta
    def pebs(p):
        return {k: int(v) for k, v in re.findall(r'^\s*(\w+): (\d+)$',
                                                read(p/'pebs_order_stats'), re.M)}
    a, b = pebs(before), pebs(after)
    if not {'sampled', 'lost', 'throttled', 'unknown'} <= a.keys() or a.keys() != b.keys():
        raise ValueError('Missing PEBS counters')
    result['pebs'] = {k: b[k] - a[k] for k in a}
    if min(result['pebs'].values()) < 0:
        raise ValueError('PEBS counters reset')
    result['perf_controls_changed'] = read(before/'perf.json') != read(after/'perf.json')
    result['snapshot_seconds'] = (json.loads(read(after/'time.json'))['end'] -
                                  json.loads(read(before/'time.json'))['end'])
    return result
