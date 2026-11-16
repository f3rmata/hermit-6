#!/usr/bin/env python3
"""Sweep allocation ceilings and observe PEBS granularity and throughput.

Keep every smaller THP order eligible and preserve Hermit policy tunables.
--dry-run needs no sudo or host changes.
"""
import argparse
import csv
import fcntl
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any

from page_selection_metrics import table
from matrix_metrics import (WR_FIELDS, POLICY_FIELDS, counter_delta, wr_delta,
                            policy_delta, policy_activity, policy_masks, policy_suffix,
                            read_optional, write_comparisons)

RDMA = Path(__file__).resolve().parents[1]
DEBUG = Path('/sys/kernel/debug/hermit')
MEMORY_POLICY = 'fixed-max-v1'
PAGE_POLICY = 'max-allocation-v1'
WR_STATS = Path('/sys/module/rswap_client/parameters/wr_stats')
SAMPLING_CONTROLS = ['pebs_mode', 'pebs_load_period', 'pebs_store_period', 'pebs_adaptive']
SUITES = {
    'anon-1t-full': ('run_anon_swapout_sweep.sh', dict(BENCH_THREADS=1, BENCH_CPUS='0', ACCESS_RATIOS='100')),
    'anon-1t-chunk64k': ('run_anon_swapout_sweep.sh', dict(BENCH_THREADS=1, BENCH_CPUS='0', ACCESS_RATIOS='chunk64k')),
    'anon-8t-full': ('run_anon_swapout_sweep.sh', dict(BENCH_THREADS=8, BENCH_CPUS='0-7', ACCESS_RATIOS='100')),
    'anon-8t-chunk64k': ('run_anon_swapout_sweep.sh', dict(BENCH_THREADS=8, BENCH_CPUS='0-7', ACCESS_RATIOS='chunk64k')),
    'redis': ('run_redis_page_sweep.sh', dict(BENCH_CPUS='8-15', REDIS_SERVER_CPUS='0-7')),
    'ycsb': ('run_ycsb_page_sweep.sh', dict(BENCH_CPUS='8-15', SERVER_CPUS='0-7')),
    'xgboost': ('run_xgboost_page_sweep.sh', dict(BENCH_CPU='0-3')),
}
# Opt-in suites; the default plan stays the original seven.
DEFAULT_SUITES = list(SUITES)
# Access-density sweep: one thread, regions visited in random order, a fixed
# share of each 2 MiB region touched either as one contiguous run (high) or
# scattered with a coprime stride (low). Each suite is exactly one
# ratio/order/locality combination so the driver writes one summary row.
for _ratio, _locality in [('50', 'high'), ('25', 'high'), ('25', 'low'),
                          ('6.25', 'high'), ('6.25', 'low'), ('1p', 'high')]:
    SUITES[f'anon-1t-d{_ratio}-{_locality}'] = ('run_anon_swapout_sweep.sh', dict(
        BENCH_THREADS=1, BENCH_CPUS='0', ACCESS_RATIOS=_ratio,
        ACCESS_ORDERS='random', ACCESS_LOCALITIES=_locality))
# Graph kernels on a synthetic Kronecker graph; every trial is verified.
SUITES['gapbs-bfs'] = ('run_gapbs_page_sweep.sh', dict(GAPBS_KERNEL='bfs', GAPBS_TRIALS=16,
                                                       GAPBS_THREADS=8, BENCH_CPUS='0-7'))
SUITES['gapbs-pr'] = ('run_gapbs_page_sweep.sh', dict(GAPBS_KERNEL='pr', GAPBS_TRIALS=4,
                                                      GAPBS_THREADS=8, BENCH_CPUS='0-7'))
CONTROLS = '''remote_order_mask pebs_mode pebs_adaptive pebs_load_period
pebs_store_period apt_reclaim batch_account batch_io batch_swapout batch_tlb
bypass_swapcache lazy_poll reclaim_headroom_pages reclaim_mode speculative_io
speculative_lock sthd_cnt vaddr_swapout exclusive_swapout swap_thread prefetch_thread'''.split()
CONTROLS += '''effective_order_mask pebs_all pebs_window_ms pebs_history_ms
pebs_pc_max pebs_fixed_cost_ns pebs_region_max
policy_features policy_cold_us policy_cold_epochs policy_pages_max policy_swapin_max
pebs_predict_regions pebs_predict_samples'''.split()


def command(args, **kwargs):
    return subprocess.check_output([str(x) for x in args], text=True, **kwargs)


def read(path):
    return command(['sudo', '-n', 'cat', path]).strip()


def exists(path):
    return subprocess.run(['sudo', '-n', 'test', '-e', str(path)]).returncode == 0


def executable(path):
    """os.access() reports False for a missing or unreadable path instead of
    raising; keep the CLI value validation in one place.
    """
    try:
        return os.access(path, os.X_OK)
    except (OSError, TypeError, ValueError):
        return False


def write(path, value):
    subprocess.run(['sudo', '-n', 'tee', str(path)], input=str(value)+'\n',
                   text=True, stdout=subprocess.DEVNULL, check=True)


def sampler(mode):
    # Never call custom syscall numbers until the Hermit interfaces are verified.
    code = '''import ctypes,sys
lib=ctypes.CDLL(None,use_errno=True)
lib.syscall.restype=ctypes.c_long
r=lib.syscall(ctypes.c_long(int(sys.argv[1])),ctypes.c_long(-1),ctypes.c_long(int(sys.argv[2])))
if r<0: raise OSError(ctypes.get_errno(),"Hermit PEBS syscall failed")
'''
    command(['sudo', '-n', sys.executable, '-c', code, 474 if mode is None else 473, mode or 0])


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n')


def snapshot_sources(destination):
    """Keep relative imports usable when running the archived workload scripts."""
    paths = [p for p in RDMA.iterdir() if p.suffix in ('.sh', '.py', '.c', '.h')]
    paths += [Path(__file__).resolve(), RDMA/'pebs/enable_pebs_pid.py',
              RDMA/'pebs/page_selection_metrics.py', RDMA/'pebs/matrix_metrics.py']
    for path in paths:
        target = destination/path.relative_to(RDMA)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    with (destination/'common.sh').open('a') as f:
        f.write('\nsudo() { command sudo -n "$@"; }\n')
    return {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(destination.rglob('*')) if p.is_file()}


def policy_settings() -> dict[str, Any]:
    return {'controls': {k: read(DEBUG/k) for k in CONTROLS if exists(DEBUG/k)},
            'mglru': {name: read_optional(Path('/sys/kernel/mm/lru_gen')/name)
                      for name in ('enabled', 'min_ttl_ms')},
            'thp': {str(p): p.read_text().strip() for p in sorted(
                Path('/sys/kernel/mm/transparent_hugepage').glob('**/enabled'))}}


def policy_changes(baseline, current, owned=()):
    changes = []
    for section in ('controls', 'thp', 'mglru'):
        old, new = baseline.get(section, {}), current.get(section, {})
        for key in old.keys() | new.keys():
            if section == 'controls' and key in SAMPLING_CONTROLS + ['pebs_enabled', 'pebs_order_stats'] + list(owned):
                continue
            if old.get(key) != new.get(key):
                changes.append(f'policy configuration changed: {section}/{key}')
    return sorted(changes)


def snapshot(path) -> dict[str, Any]:
    settings = policy_settings()
    data = {'time_ns': time.time_ns(), 'uname': list(platform.uname()),
            'controls': settings['controls'] |
                        {'pebs_enabled':read(DEBUG/'pebs_enabled'), 'pebs_order_stats':read(DEBUG/'pebs_order_stats')},
            'thp': settings['thp'], 'mglru': settings['mglru'], 'wr_stats': read(WR_STATS),
            'order_stats': read(DEBUG/'order_stats'),
            'perf': {p.name: p.read_text().strip() for p in Path('/proc/sys/kernel').glob('perf_*')},
            'cpu_stat': Path('/proc/stat').read_text(),
            'swaps': Path('/proc/swaps').read_text(), 'vmstat': Path('/proc/vmstat').read_text()}
    data['policy_stats'] = read(DEBUG/'policy_stats') if exists(DEBUG/'policy_stats') else None
    save_json(path, data)
    return data


def plan(args):
    # None = preserve the kernel's current mask (historical behaviour).
    masks = policy_masks(args.policy_features) if getattr(args, 'policy_features', None) else [(None, None)]
    cases: list[tuple[str, int | None, int | None, int | None]] = [('off', None, 0, None)]
    for period in args.periods:
        cases.append((f'static-{period}', 0, period, None))
        for label, mask in masks:
            cases.append((f'policy-{period}{policy_suffix(label)}', 1, period, mask))
    rng = random.Random(args.seed)
    rows = []
    pages = args.pages if 4 in args.pages else [4, *args.pages]
    for repeat, suite, page in itertools.product(range(1, args.repeats+1), args.suites, pages):
        block = cases.copy() if page in args.pages else [cases[0]]
        rng.shuffle(block)
        for label, mode, period, mask in block:
            rows.append(dict(suite=suite, page_kb=page, max_page_kb=page, repeat=repeat, case=label,
                             baseline_role='4KiB-only' if page == 4 and label == 'off' else '',
                             mode=mode, load_period=period, store_period=args.store_period,
                             policy_features=mask))
    return rows


def validate_backend_orders(max_kb, mask):
    required = ((1 << (max_kb // 4).bit_length()) - 1) & ~2  # no anonymous order-1 THP
    if mask & required != required:
        raise ValueError(f'effective_order_mask={mask:#x} excludes sizes below the {max_kb} KiB ceiling; '
                         f'prepare Hermit with all required backend orders ({required:#x}) before running; '
                         'the matrix does not rewrite remote_order_mask')


def validate_swapin_path(bypass_swapcache):
    # With ordinary block-device swap, alloc_swap_folio() reaches the PEBS
    # selector only on Hermit's direct path. Sampling alone is not sufficient.
    if bypass_swapcache not in ('Y', '1'):
        raise ValueError(f'bypass_swapcache={bypass_swapcache}: the PEBS matrix requires direct swap-in; '
                         'set /sys/kernel/debug/hermit/bypass_swapcache to Y before running; '
                         'the matrix preserves this setting for off/static/policy')


def check_policy_activity(row, measurement):
    application = [r for r in measurement['granularity'] if r['phase'] == 'application']
    counts = [r['pebs_decisions'] for r in application if r['order'] is not None and r['order'] != 1]
    decisions = sum(counts) if counts and all(v is not None for v in counts) else None
    policy = measurement.get('policy', {}).get('application')
    wrs = {r['size_kib']: r for r in application} if policy is not None else {}
    return policy_activity(row['mode'], row['page_kb'], decisions, policy, wrs)


def environment(args):
    # Do not inherit accidental benchmark overrides from a previous shell run.
    env = {k: v for k, v in os.environ.items() if k in
           ('HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH',
            'PYTHONPATH', 'VIRTUAL_ENV', 'JAVA_HOME', 'SSH_AUTH_SOCK')}
    env.update({k: str(v) for k, v in dict(
        MODE='cgroup-hermit', HERMIT6_ROOT=RDMA.parents[1], BENCH_REPEATS=1,
        WORKSET_MB=16384, REDIS_WORKSET_MB=16384, LOCAL_RATIO_PCT=70,
        PAGE_SIZE_POLICY='max', ACCESS_REGION_KB=2048, RSWAP_REQUIRED_BACKEND='rdma',
        BENCH_SOCKET=0, BENCH_NUMA_NODE=0, BENCH_NUMACTL=1,
        SWAPOUT_TRIGGER='parallel-fault', ACCESS_ORDERS='sequential', ACCESS_LOCALITIES='high',
        REDIS_SERVER_BIN=args.redis, REDIS_PORT=args.port, REDIS_VALUE_SIZE=2097152,
        REDIS_SCAN_CHUNK=65536, REDIS_ACTIVE_RATIOS=100, REDIS_ACCESS_ORDER='sequential',
        REDIS_CHECKSUM='Y', REDIS_CLIENTS=1, REDIS_INSTANCES=1,
        BINDING='redis', YCSB_BIN=args.ycsb, YCSB_RECORDCOUNT=8192,
        YCSB_OPERATIONCOUNT=8192, YCSB_FIELDLENGTH=2097152, YCSB_FIELDCOUNT=1,
        YCSB_READALLFIELDS='true', YCSB_REQUESTDISTRIBUTION='uniform',
        XGB_DATA_FILE=args.higgs, XGB_DATA_FORMAT='csv', XGB_NTHREAD=4, XGB_ROUNDS=30,
        XGB_EXPECTED_METRIC_MIN=0.80, XGB_EXPECTED_METRIC_MAX=0.85,
    ).items()})
    if any(s.startswith('gapbs-') for s in getattr(args, 'suites', ())):
        env.update(GAPBS_DIR=str(args.gapbs), GAPBS_SCALE=str(args.gapbs_scale), GAPBS_DEGREE='16')
    return env


def validate_results(folder, row):
    issues = []
    files = list(folder.glob('*summary.csv'))
    if len(files) != 1:
        return ['expected exactly one workload summary CSV']
    with files[0].open() as f:
        records = list(csv.DictReader(f))
    if len(records) != 1:
        return [f'expected one workload sample, found {len(records)}']
    record = records[0]
    if record['page_kb'] != str(row['page_kb']):
        issues.append('page size mismatch')
    budgets = list(folder.rglob('memory-max-before-swapin.txt'))
    if len(budgets) != 1:
        issues.append('expected one fixed memory budget snapshot pair')
    else:
        budget_dir = budgets[0].parent
        policy = budget_dir/'memory-policy.txt'
        if not policy.exists() or policy.read_text().strip() != MEMORY_POLICY:
            issues.append('missing or incorrect memory policy')
        limit_mb = record.get('limit_mb', '')
        if not limit_mb.isdigit() or int(limit_mb) <= 0:
            issues.append('missing or invalid limit_mb')
        else:
            expected = str(int(limit_mb) * 1024 * 1024)
            for phase in ('before-swapin', 'after-swapin'):
                path = budget_dir/f'memory-max-{phase}.txt'
                if not path.exists() or path.read_text().strip() != expected:
                    issues.append(f'memory.max mismatch: {phase}, expected {expected}')
    for key, value in record.items():
        if ('errors' in key or key == 'checksum_errors') and float(value) != 0:
            issues.append(f'{key}={value}')
    if row['suite'] == 'ycsb':
        for key in ('read_ops', 'read_ok'):
            if float(record[key]) != 8192: issues.append(f'{key}={record[key]}, expected 8192')
    if row['suite'].startswith('gapbs-'):
        trials, verified = record.get('trials', ''), record.get('verified_trials', '')
        if not trials.isdigit() or int(trials) < 1 or verified != trials:
            issues.append(f'verified_trials={verified} of trials={trials}')
    # Check every order, not just the requested folio order.
    for before in folder.rglob('order-before*.txt'):
        after = before.with_name(before.name.replace('before','after',1))
        if not after.exists():
            issues.append(f'missing {after.name}')
            continue
        def errors(path):
            return sum(int(line.split()[5]) for line in path.read_text().splitlines()[1:] if line.strip())
        if errors(after) != errors(before): issues.append(f'backend errors: {after}')
    for p in folder.rglob('memory-events*.txt'):
        counters = dict(line.split() for line in p.read_text().splitlines())
        if int(counters.get('oom_kill',0)): issues.append(f'OOM: {p}')
    return issues


def table_delta(before, after):
    a, b = table(before.read_text()), table(after.read_text())
    if a.keys() != b.keys():
        raise ValueError('order table changed during measurement')
    result = {}
    for order in a:
        if a[order].get('size_bytes') != b[order].get('size_bytes'):
            raise ValueError('order size changed during measurement')
        result[order] = counter_delta({k:v for k,v in a[order].items() if k != 'size_bytes'},
                                      {k:v for k,v in b[order].items() if k != 'size_bytes'})
    return result


def collect_measurement(folder, row) -> dict[str, Any]:
    """Pair application performance with phase deltas, never with a requested size."""
    with next(folder.glob('*summary.csv')).open() as f:
        record, = list(csv.DictReader(f))
    suite = row['suite']
    if suite.startswith('anon-'):
        metric, unit, duration = 'accessed_scan_gib_per_sec', 'page-coverage GiB/s', 'swapin_scan_sec'
    elif suite == 'redis':
        metric, unit, duration = 'get_qps', 'GET/s', 'bench_sec'
    elif suite == 'ycsb':
        metric, unit, duration = 'throughput_ops', 'ops/s', 'run_sec'
    elif suite.startswith('gapbs-'):
        metric, unit, duration = 'trials_per_sec', 'trials/s', 'trial_sec_total'
    else:
        metric, unit, duration = 'rounds_per_sec', 'rounds/s', 'train_sec'
    seconds = float(record[duration])
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('invalid application duration')
    throughput = float(record['rounds']) / seconds if suite == 'xgboost' else float(record[metric])
    if not math.isfinite(throughput) or throughput <= 0:
        raise ValueError('invalid application throughput')
    performance = dict(metric=metric, unit=unit, throughput=throughput, duration_sec=seconds)
    histogram = []
    allocation_settings = {}
    policy = {}
    swap = {}
    for phase, suffix in [('populate', ''), ('application', '-swapin')]:
        paths = list(folder.rglob(f'order-before{suffix}.txt'))
        if len(paths) != 1:
            raise ValueError(f'expected one {phase} counter snapshot pair')
        before = paths[0]
        after = before.with_name(before.name.replace('before', 'after', 1))
        folios = table_delta(before, after)
        wrs = wr_delta(Path(str(before)+'.wr').read_text(), Path(str(after)+'.wr').read_text())
        policy[phase] = policy_delta(read_optional(str(before)+'.policy'), read_optional(str(after)+'.policy'))
        snapshots = [read_optional(str(p)+'.vmstat') for p in (before, after)]
        present = [text for text in snapshots if text is not None]
        if present:
            if len(present) != len(snapshots):
                raise ValueError('missing vmstat snapshot')
            swap[phase] = counter_delta(*[{k: int(v) for k, v in
                                          (line.split() for line in text.splitlines())
                                          if k in ('pswpin', 'pswpout')} for text in present])
        thp_before, thp_after = (Path(str(p)+'.thp').read_text() for p in (before, after))
        if not thp_before.strip() or thp_before != thp_after:
            raise ValueError('THP settings missing or changed during measurement')
        allocation_settings[phase] = dict(line.rsplit('=', 1) for line in thp_before.splitlines())
        for path, setting in allocation_settings[phase].items():
            match = re.search(r'hugepages-(\d+)kB/enabled$', path)
            size = int(match[1]) if match else 2048
            expected = 'always' if 16 <= size <= row['page_kb'] else 'never'
            if setting != expected:
                raise ValueError(f'THP allocation ceiling mismatch: {path}={setting}')
        pebs = []
        for path in (before, after):
            text = Path(str(path)+'.pebs').read_text()
            pebs.append({k:int(v) for k,v in re.findall(r'^\s*(actual_order_\d+|\d+):\s*(\d+)\s*$', text, re.M)})
        if not pebs[0] or not pebs[1]:
            raise ValueError('missing PEBS order counters')
        decisions = counter_delta(*pebs)
        total_read = sum(v['read_bytes'] for v in wrs.values())
        folios_by_size = {4 << int(order): values for order, values in folios.items()}
        for size_kib in sorted(folios_by_size.keys() | wrs.keys()):
            pages = size_kib // 4
            order = pages.bit_length() - 1 if pages & (pages - 1) == 0 else None
            f, w = folios_by_size.get(size_kib, {}), wrs.get(size_kib, {})
            size = size_kib * 1024
            histogram.append(dict(phase=phase, order=order, size_kib=size_kib,
                folio_stores=f.get('stores', 0), folio_loads=f.get('loads', 0),
                folio_store_bytes=f.get('stores', 0)*size, folio_load_bytes=f.get('loads', 0)*size,
                folio_fallback_4k=f.get('fallback_4k', 0), folio_errors=f.get('errors', 0),
                write_wrs=w.get('write_wrs', 0), read_wrs=w.get('read_wrs', 0),
                write_bytes=w.get('write_bytes', 0), read_bytes=w.get('read_bytes', 0),
                **{'batch_' + k: w.get('batch_' + k, 0) for k in WR_FIELDS},
                read_byte_share=w.get('read_bytes', 0)/total_read if total_read else 0,
                pebs_decisions=decisions.get(str(order)), pebs_actual_swapins=decisions.get('actual_order_'+str(order))))
    return dict(page_policy=PAGE_POLICY, max_page_kb=row['page_kb'], performance=performance,
                workload=record, allocation_settings=allocation_settings, granularity=histogram,
                policy=policy, swap=swap)


def append_csv(path, rows):
    with path.open('a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        if f.tell() == 0:
            writer.writeheader()
        writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, default=RDMA/'results'/'pebs'/time.strftime('%Y%m%d-%H%M%S-workloads'))
    ap.add_argument('--suites', nargs='+', choices=SUITES, default=DEFAULT_SUITES,
                    help='default: the original seven; access-density (anon-1t-d*) and gapbs-* suites are opt-in')
    ap.add_argument('--pages', nargs='+', type=int, choices=[4,16,32,64,128,256,512,1024,2048],
                    default=[4,16,32,64,128,256,512,1024,2048],
                    help='maximum allocation sizes in KiB; smaller orders stay eligible; always includes a 4KiB/off reference')
    ap.add_argument('--periods', nargs='+', type=int, default=[19997,1999,199])
    ap.add_argument('--store-period', type=int, default=1500003)
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--seed', type=int, default=20261007)
    ap.add_argument('--timeout', type=int, default=10800, help='seconds per sample')
    ap.add_argument('--redis', default=str(Path.home()/'redis/src/redis-server'))
    ap.add_argument('--ycsb', default=str(Path.home()/'ycsb-0.17.0/bin/ycsb'))
    ap.add_argument('--higgs', default=str(RDMA/'data/HIGGS.csv/HIGGS.csv'))
    ap.add_argument('--port', type=int, default=6399)
    ap.add_argument('--gapbs', default=str(Path.home()/'pebs-apps-20261031/gapbs-1.5'),
                    help='GAPBS checkout (src/ is compiled with gapbs_hermit_hook.h)')
    ap.add_argument('--gapbs-scale', type=int, default=24, help='Kronecker graph scale (2^scale vertices)')
    ap.add_argument('--policy-features', nargs='+', default=None,
                    help='policy_features ablations for policy cells: preset names (default, no-pebs, '
                         'no-batch), raw masks (0x1bd) or comma-separated lists; omitted preserves the '
                         'kernel mask and keeps the historical off/static/policy cells')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if min(args.periods+[args.store_period,args.repeats,args.timeout]) < 1:
        ap.error('periods, repeats and timeout must be positive')
    if not 10 <= args.gapbs_scale <= 30: ap.error('--gapbs-scale must be in [10, 30]')
    if args.policy_features:
        try:
            policy_masks(args.policy_features)
        except ValueError as exc:
            ap.error(str(exc))
    for values in (args.periods, args.suites, args.pages):
        if len(set(values)) != len(values): ap.error('duplicate selections are not allowed')
    rows, env = plan(args), environment(args)
    if args.dry_run:
        print(json.dumps({'samples':len(rows), 'memory_policy':MEMORY_POLICY, 'page_policy':PAGE_POLICY,
                          'environment':env, 'plan':rows}, indent=2))
        return
    if os.geteuid() == 0: ap.error('run as the benchmark user after sudo -v')
    if platform.machine() != 'x86_64' or 'hermit-pebs' not in platform.release():
        ap.error('requires x86_64 hermit-pebs kernel')
    if args.output.exists(): ap.error('output must be a new directory; completed results are never overwritten')
    command(['sudo','-n','true'])
    try:
        lock = open(f'/tmp/hermit-pebs-workloads-{os.getuid()}.lock', 'w')
    except OSError as exc:
        ap.error(f'cannot create the matrix lock file: {exc}')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for key in ['remote_order_mask', *SAMPLING_CONTROLS, 'pebs_enabled', 'pebs_order_stats', 'order_stats']:
        read(DEBUG/key)
    if read(DEBUG/'pebs_enabled') != '0': ap.error('an existing PEBS session is active')
    try:
        validate_backend_orders(max(args.pages), int(read(DEBUG/'effective_order_mask'), 0))
        validate_swapin_path(read(DEBUG/'bypass_swapcache'))
    except ValueError as exc:
        ap.error(str(exc))
    read(WR_STATS)
    if not Path('/sys/module/rswap_client/parameters/sip').exists(): ap.error('RDMA rswap client is not loaded')
    if len(Path('/proc/swaps').read_text().splitlines()) < 2: ap.error('no active swap')
    for name in ('redis-server','memcached','mutilate','hermit_pebsd'):
        if subprocess.run(['pgrep','-x',name], stdout=subprocess.DEVNULL).returncode == 0:
            ap.error(f'existing {name}; stop other benchmarks first')
    if not set(range(16)).issubset(os.sched_getaffinity(0)): ap.error('baseline requires CPUs 0-15')
    for binary in ('cc','numactl','taskset','sudo','python3'):
        if not shutil.which(binary): ap.error(f'missing {binary}')
    if {'redis','ycsb'} & set(args.suites):
        if not executable(args.redis): ap.error(f'missing executable {args.redis}')
        try:
            with socket.socket() as sock: sock.bind(('127.0.0.1',args.port))
        except OSError as exc:
            ap.error(f'cannot bind 127.0.0.1:{args.port} for memcached: {exc}')
    if 'ycsb' in args.suites and not executable(args.ycsb): ap.error(f'missing executable {args.ycsb}')
    if any(s.startswith('gapbs-') for s in args.suites):
        for name in ('bfs.cc', 'pr.cc', 'benchmark.h'):
            if not (Path(args.gapbs)/'src'/name).is_file(): ap.error(f'missing GAPBS source: {Path(args.gapbs)/"src"/name}')
        if not shutil.which('g++'): ap.error('missing g++ for the GAPBS build')
    if 'xgboost' in args.suites:
        if not Path(args.higgs).is_file(): ap.error(f'missing HIGGS CSV: {args.higgs}')
        try:
            command(['python3','-c','import xgboost, numpy'], stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as exc:
            ap.error('XGBoost dependency check failed in python3 from PATH; '
                     'activate the benchmark Python environment. Details: ' + exc.output.strip())
    for cg in Path('/sys/fs/cgroup').glob('hermit-*'):
        if (cg/'cgroup.procs').exists() and read(cg/'cgroup.procs'):
            ap.error(f'busy benchmark cgroup: {cg}')
    # Restore only sampling controls and THP ceilings owned by this experiment.
    saved = {str(DEBUG/k):read(DEBUG/k) for k in SAMPLING_CONTROLS}
    baseline = policy_settings()
    saved.update({p:match[1] for p, value in baseline['thp'].items()
                  if (match := re.search(r'\[([^]]+)\]', value))})
    # The ablation matrix owns policy_features per cell; anything else is drift.
    owned = set()
    if args.policy_features:
        if not exists(DEBUG/'policy_features'):
            ap.error('running kernel has no policy_features control')
        saved[str(DEBUG/'policy_features')] = read(DEBUG/'policy_features')
        # Reload config: the per-cell mask must not be reported as external drift.
        owned.add('policy_features')
    args.output.mkdir(parents=True)
    source = args.output/'source'
    source_hashes = snapshot_sources(source)
    save_json(args.output/'plan.json', {'samples':len(rows),'seed':args.seed,'environment':env,'plan':rows,
        'workload_parameters_from':'20260924-prepebs-full', 'memory_policy':MEMORY_POLICY,
        'page_policy':PAGE_POLICY, 'initial_policy_settings':baseline,
        'policy_presets':None if not args.policy_features else policy_masks(args.policy_features),
        'dataset': {'path':args.higgs,'size':Path(args.higgs).stat().st_size} if 'xgboost' in args.suites else None, 'source_sha256':source_hashes})
    save_json(args.output/'restore.json',saved)
    save_json(args.output/'host.json', {
        'version':Path('/proc/version').read_text(), 'cmdline':Path('/proc/cmdline').read_text(),
        'modules':Path('/proc/modules').read_text(),
        'rswap':{p.name:p.read_text().strip() for p in Path('/sys/module/rswap_client/parameters').iterdir()},
        'rswap_srcversion':Path('/sys/module/rswap_client/srcversion').read_text().strip(),
        'python':command(['python3','--version']).strip(),
        'xgboost':command(['python3','-c','import xgboost; print(xgboost.__version__)']).strip() if 'xgboost' in args.suites else None,
        'redis':command([args.redis,'--version']).strip() if {'redis','ycsb'} & set(args.suites) else None,
    })
    snapshot(args.output/'before.json')
    (args.output/'lscpu.txt').write_text(command(['lscpu']))
    (args.output/'dmesg-before.txt').write_text(command(['sudo','-n','dmesg']))
    active = False
    child = None
    cg = None
    failed = True
    restore_errors = []
    def interrupted(sig, frame): raise InterruptedError(f'signal {sig}')
    for sig in (signal.SIGTERM,signal.SIGHUP): signal.signal(sig,interrupted)
    try:
        for index, row in enumerate(rows,1):
            name = f'{index:04d}-{row["suite"]}-max{row["page_kb"]}k-r{row["repeat"]}-{row["case"]}'
            folder = args.output/name; folder.mkdir()
            print(f'[{index}/{len(rows)}] {name}', flush=True)
            cg = Path(f'/sys/fs/cgroup/hermit-pebs-{os.getpid()}-{index}')
            if cg.exists(): raise RuntimeError(f'cgroup already exists: {cg}')
            if row['mode'] is not None:
                for k,v in dict(pebs_mode=row['mode'],pebs_adaptive=0,pebs_load_period=row['load_period'],
                                pebs_store_period=row['store_period']).items(): write(DEBUG/k,v)
            if row['policy_features'] is not None:
                write(DEBUG/'policy_features', hex(row['policy_features']))
                if int(read(DEBUG/'policy_features'), 0) != row['policy_features']:
                    raise RuntimeError(f'policy_features did not take the requested mask {row["policy_features"]:#x}')
            before = snapshot(folder/'before.json')
            changes = policy_changes(baseline, before, owned)
            if changes:
                raise RuntimeError('; '.join(changes))
            if row['mode'] is not None:
                sampler(row['mode']); active=True
            script, suite_env = SUITES[row['suite']]
            case_env = env | {k:str(v) for k,v in suite_env.items()} | dict(
                PAGE_SIZES_KB=str(row['page_kb']), RESULT_DIR=str(folder.resolve()/'results'),
                RUN_ID=name, PEBS_CGROUP=str(cg), PEBS_BENCH_MODE=row['case'])
            save_json(folder/'case.json',row | {'environment':case_env, 'memory_policy':MEMORY_POLICY,
                                               'page_policy':PAGE_POLICY})
            start=time.monotonic()
            with (folder/'run.log').open('w') as log:
                child=subprocess.Popen(['bash',str(source/script)],env=case_env,stdout=log,stderr=subprocess.STDOUT,preexec_fn=os.setpgrp)
                while True:
                    try: rc=child.wait(timeout=30); break
                    except subprocess.TimeoutExpired:
                        command(['sudo','-n','-v'])
                        if time.monotonic()-start > args.timeout: raise TimeoutError(name)
            child=None
            after=snapshot(folder/'after.json')
            if active: sampler(None); active=False
            if cg.exists(): command(['sudo','-n','rmdir',cg])
            cg=None
            def count(state,key):
                match=re.search(rf'^{key}:\s*(\d+)',state['controls']['pebs_order_stats'],re.M)
                if not match: raise RuntimeError(f'missing PEBS counter {key}')
                return int(match[1])
            delta={key:count(after,key)-count(before,key) for key in ('sampled','lost','throttled')}
            issues = validate_results(folder/'results', row) if rc == 0 else ['workload failed']
            issues += policy_changes(baseline, after, owned)
            measurement = None
            policy_activity = None
            if rc == 0:
                try:
                    measurement = collect_measurement(folder/'results', row)
                    save_json(folder/'measurement.json', measurement)
                    policy_activity, policy_issues = check_policy_activity(row, measurement)
                    issues += policy_issues
                except (ValueError, KeyError, OSError, StopIteration) as exc:
                    issues.append(f'incomplete measurement: {exc}')
            if row['mode'] is not None and delta['sampled'] <= 0:
                issues.append('no PEBS samples collected during the run')
            valid = rc == 0 and not issues
            save_json(folder/'status.json',dict(exit_code=rc,elapsed_sec=time.monotonic()-start,
                usable=valid,validation_issues=issues,pebs_delta=delta,policy_activity=policy_activity,
                perf_rate_changed=before['perf']!=after['perf']))
            if measurement is not None:
                identity = dict(run_id=name, suite=row['suite'], max_page_kb=row['page_kb'],
                                repeat=row['repeat'], case=row['case'], usable=valid,
                                baseline_role=row['baseline_role'],
                                policy_features=None if row['policy_features'] is None else
                                hex(row['policy_features']))
                activity_columns = {'application_policy_decisions': policy_activity['application_decisions'],
                                    'policy_path_exercised': policy_activity['path_exercised']} if policy_activity else {
                                    'application_policy_decisions': None, 'policy_path_exercised': None}
                counters: dict[str, Any] = {
                    key: sum(r[key] for r in measurement['granularity'] if r['phase'] == 'application')
                    for key in WR_FIELDS}
                counters.update({'policy_' + k: (measurement['policy']['application'] or {}).get(k)
                                 for k in POLICY_FIELDS})
                counters.update({k: measurement['swap'].get('application', {}).get(k) for k in ('pswpin', 'pswpout')})
                counters.update({k: measurement['workload'].get(k) for k in ('read_avg_us', 'read_p95_us', 'read_p99_us')})
                append_csv(args.output/'summary.csv', [identity | measurement['performance'] | delta | activity_columns | counters])
                append_csv(args.output/'granularity.csv', [identity | r for r in measurement['granularity']])
            if not valid: raise RuntimeError(f'{name}: {"; ".join(issues)}; see status.json and run.log')
        failed=False
    finally:
        # Cleanup must not be interrupted by a second terminal signal.
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP): signal.signal(sig,signal.SIG_IGN)
        if child is not None:
            try: os.killpg(child.pid,signal.SIGTERM)
            except ProcessLookupError: pass
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid,signal.SIGKILL); child.wait()
        def attempt(label,fn):
            try: fn()
            except Exception as exc: restore_errors.append(f'{label}: {exc}')
        if active: attempt('stop sampler',lambda:sampler(None))
        if cg is not None and cg.exists():
            # Only the unique cgroup owned by this invocation may be killed.
            if (cg/'cgroup.kill').exists(): attempt('kill owned cgroup',lambda:write(cg/'cgroup.kill',1))
            attempt('remove owned cgroup',lambda:command(['sudo','-n','rmdir',cg]))
        for path,value in saved.items(): attempt(path,lambda p=path,v=value:write(p,v))
        for path,value in saved.items():
            def verify(p=path,v=value):
                actual=read(p)
                match=re.search(r'\[([^]]+)\]',actual)
                if match: actual=match[1]
                # Numeric debugfs values may be rendered as decimal or hex.
                if actual != v:
                    try: equal=int(actual,0)==int(v,0)
                    except ValueError: equal=False
                    if not equal: raise RuntimeError(f'expected {v}, got {actual}')
            attempt('verify '+path,verify)
        attempt('final snapshot',lambda:snapshot(args.output/'after.json'))
        attempt('dmesg',lambda:(args.output/'dmesg-after.txt').write_text(command(['sudo','-n','dmesg'])))
        attempt('paired comparison',lambda:write_comparisons(args.output))
        save_json(args.output/'completion.json',dict(completed=not failed,restore_errors=restore_errors))
        if restore_errors: print('RESTORE ERRORS: '+str(restore_errors),file=sys.stderr)
    if restore_errors: raise RuntimeError('environment restore incomplete; see completion.json and restore.json')


if __name__ == '__main__':
    main()
