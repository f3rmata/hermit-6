#!/usr/bin/env python3
"""Baseline-matched PEBS matrix. --dry-run never needs sudo or changes the host."""
import argparse
import csv
import fcntl
import hashlib
import itertools
import json
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

RDMA = Path(__file__).resolve().parents[1]
DEBUG = Path('/sys/kernel/debug/hermit')
SUITES = {
    'anon-1t-full': ('run_anon_swapout_sweep.sh', dict(BENCH_THREADS=1, BENCH_CPUS='0', ACCESS_RATIOS='100')),
    'anon-1t-chunk64k': ('run_anon_swapout_sweep.sh', dict(BENCH_THREADS=1, BENCH_CPUS='0', ACCESS_RATIOS='chunk64k')),
    'anon-8t-full': ('run_anon_swapout_sweep.sh', dict(BENCH_THREADS=8, BENCH_CPUS='0-7', ACCESS_RATIOS='100')),
    'anon-8t-chunk64k': ('run_anon_swapout_sweep.sh', dict(BENCH_THREADS=8, BENCH_CPUS='0-7', ACCESS_RATIOS='chunk64k')),
    'redis': ('run_redis_page_sweep.sh', dict(BENCH_CPUS='8-15', REDIS_SERVER_CPUS='0-7')),
    'ycsb': ('run_ycsb_page_sweep.sh', dict(BENCH_CPUS='8-15', SERVER_CPUS='0-7')),
    'xgboost': ('run_xgboost_page_sweep.sh', dict(BENCH_CPU='0-3')),
}
CONTROLS = '''remote_order_mask pebs_mode pebs_force_order pebs_adaptive pebs_load_period
pebs_store_period apt_reclaim batch_account batch_io batch_swapout batch_tlb
bypass_swapcache lazy_poll reclaim_headroom_pages reclaim_mode speculative_io
speculative_lock sthd_cnt vaddr_swapout exclusive_swapout swap_thread prefetch_thread'''.split()


def command(args, **kwargs):
    return subprocess.check_output([str(x) for x in args], text=True, **kwargs)


def read(path):
    return command(['sudo', '-n', 'cat', path]).strip()


def exists(path):
    return subprocess.run(['sudo', '-n', 'test', '-e', str(path)]).returncode == 0


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


def snapshot(path):
    data = {'time_ns': time.time_ns(), 'uname': list(platform.uname()),
            'controls': {k: read(DEBUG/k) for k in CONTROLS if k.startswith('pebs_')} |
                        {'pebs_enabled':read(DEBUG/'pebs_enabled'), 'pebs_order_stats':read(DEBUG/'pebs_order_stats')},
            'order_stats': read(DEBUG/'order_stats'),
            'perf': {p.name: p.read_text().strip() for p in Path('/proc/sys/kernel').glob('perf_*')},
            'cpu_stat': Path('/proc/stat').read_text(),
            'swaps': Path('/proc/swaps').read_text(), 'vmstat': Path('/proc/vmstat').read_text()}
    save_json(path, data)
    return data


def plan(args):
    cases = [('off', None, 0)]
    for period in args.periods:
        cases.extend([(f'static-{period}', 0, period), (f'policy-{period}', 1, period)])
    rng = random.Random(args.seed)
    rows = []
    for repeat, suite, page in itertools.product(range(1, args.repeats+1), args.suites, args.pages):
        block = cases.copy()
        rng.shuffle(block)
        for label, mode, period in block:
            rows.append(dict(suite=suite, page_kb=page, repeat=repeat, case=label,
                             mode=mode, load_period=period, store_period=args.store_period))
    return rows


def environment(args):
    # Do not inherit accidental benchmark overrides from a previous shell run.
    env = {k: v for k, v in os.environ.items() if k in
           ('HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH',
            'PYTHONPATH', 'VIRTUAL_ENV', 'JAVA_HOME', 'SSH_AUTH_SOCK')}
    env.update({k: str(v) for k, v in dict(
        MODE='cgroup-hermit', HERMIT6_ROOT=RDMA.parents[1], BENCH_REPEATS=1,
        WORKSET_MB=16384, REDIS_WORKSET_MB=16384, LOCAL_RATIO_PCT=70,
        BYPASS_SWAPCACHE='Y', LAZY_POLL='N', RSWAP_REQUIRED_BACKEND='rdma',
        RECLAIM_MODE=0, RECLAIM_HEADROOM_PAGES=2048, STHD_CNT=16,
        RESTORE_THP=1, BENCH_SOCKET=0, BENCH_NUMA_NODE=0, BENCH_NUMACTL=1,
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
    if int(record['page_kb']) != row['page_kb']:
        issues.append('page size mismatch')
    for key, value in record.items():
        if ('errors' in key or key == 'checksum_errors') and float(value) != 0:
            issues.append(f'{key}={value}')
    if row['suite'] == 'ycsb':
        for key in ('read_ops', 'read_ok'):
            if float(record[key]) != 8192: issues.append(f'{key}={record[key]}, expected 8192')
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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, default=RDMA/'results'/'pebs'/time.strftime('%Y%m%d-%H%M%S-workloads'))
    ap.add_argument('--suites', nargs='+', choices=SUITES, default=list(SUITES))
    ap.add_argument('--pages', nargs='+', type=int, choices=[4,16,32,64,128,256,512,1024,2048], default=[4,16,32,64,128,256,512,1024,2048])
    ap.add_argument('--periods', nargs='+', type=int, default=[19997,1999,199])
    ap.add_argument('--store-period', type=int, default=1500003)
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--seed', type=int, default=20261007)
    ap.add_argument('--timeout', type=int, default=10800, help='seconds per sample')
    ap.add_argument('--redis', default=str(Path.home()/'redis/src/redis-server'))
    ap.add_argument('--ycsb', default=str(Path.home()/'ycsb-0.17.0/bin/ycsb'))
    ap.add_argument('--higgs', default=str(RDMA/'data/HIGGS.csv/HIGGS.csv'))
    ap.add_argument('--port', type=int, default=6399)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if min(args.periods+[args.store_period,args.repeats,args.timeout]) < 1:
        ap.error('periods, repeats and timeout must be positive')
    for values in (args.periods, args.suites, args.pages):
        if len(set(values)) != len(values): ap.error('duplicate selections are not allowed')
    rows, env = plan(args), environment(args)
    if args.dry_run:
        print(json.dumps({'samples':len(rows), 'environment':env, 'plan':rows}, indent=2))
        return
    if os.geteuid() == 0: ap.error('run as the benchmark user after sudo -v')
    if platform.machine() != 'x86_64' or 'hermit-pebs' not in platform.release():
        ap.error('requires x86_64 hermit-pebs kernel')
    if args.output.exists(): ap.error('output must be a new directory; completed results are never overwritten')
    command(['sudo','-n','true'])
    lock = open(f'/tmp/hermit-pebs-workloads-{os.getuid()}.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for key in CONTROLS[:6]+['pebs_enabled','pebs_order_stats','order_stats']:
        read(DEBUG/key)
    if read(DEBUG/'pebs_enabled') != '0': ap.error('an existing PEBS session is active')
    if not Path('/sys/module/rswap_client/parameters/sip').exists(): ap.error('RDMA rswap client is not loaded')
    if len(Path('/proc/swaps').read_text().splitlines()) < 2: ap.error('no active swap')
    for name in ('redis-server','memcached','mutilate','hermit_pebsd'):
        if subprocess.run(['pgrep','-x',name], stdout=subprocess.DEVNULL).returncode == 0:
            ap.error(f'existing {name}; stop other benchmarks first')
    if not set(range(16)).issubset(os.sched_getaffinity(0)): ap.error('baseline requires CPUs 0-15')
    for binary in ('cc','numactl','taskset','sudo','python3'):
        if not shutil.which(binary): ap.error(f'missing {binary}')
    if {'redis','ycsb'} & set(args.suites):
        if not os.access(args.redis,os.X_OK): ap.error(f'missing executable {args.redis}')
        with socket.socket() as sock: sock.bind(('127.0.0.1',args.port))
    if 'ycsb' in args.suites and not os.access(args.ycsb,os.X_OK): ap.error(f'missing executable {args.ycsb}')
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
    saved = {str(DEBUG/k):read(DEBUG/k) for k in CONTROLS if exists(DEBUG/k)}
    for p in Path('/sys/kernel/mm/transparent_hugepage').glob('**/enabled'):
        saved[str(p)] = re.search(r'\[([^]]+)\]',p.read_text())[1]
    args.output.mkdir(parents=True)
    source = args.output/'source'; source.mkdir()
    for p in RDMA.iterdir():
        if p.suffix in ('.sh','.py','.c'): shutil.copy2(p,source/p.name)
    shutil.copy2(__file__,source/'run_workload_matrix.py')
    shutil.copy2(RDMA.parent/'qemu-dram/src/hermit_swap_stats.c',source/'hermit_swap_stats.c')
    helper = args.output.resolve()/'hermit_swap_stats'
    command(['cc','-O2','-Wall','-Wextra','-Werror',RDMA.parent/'qemu-dram/src/hermit_swap_stats.c','-o',helper])
    with (source/'common.sh').open('a') as f:
        f.write('\nsudo() { command sudo -n "$@"; }\nhermit_stats_helper() { printf "%s\\n" "$PEBS_STATS_HELPER"; }\n')
    env['PEBS_STATS_HELPER'] = str(helper)
    save_json(args.output/'plan.json', {'samples':len(rows),'seed':args.seed,'environment':env,'plan':rows,
        'baseline':'20260924-prepebs-full',
        'policy_tunables': {k:read(DEBUG/k) for k in ['pebs_fixed_cost_ns','pebs_bw_mibps','pebs_min_f','pebs_hysteresis','pebs_cooling_period','pebs_region_max']},
        'dataset': {'path':args.higgs,'size':Path(args.higgs).stat().st_size} if 'xgboost' in args.suites else None, 'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}})
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
            name = f'{index:04d}-{row["suite"]}-{row["page_kb"]}k-r{row["repeat"]}-{row["case"]}'
            folder = args.output/name; folder.mkdir()
            print(f'[{index}/{len(rows)}] {name}', flush=True)
            cg = Path(f'/sys/fs/cgroup/hermit-pebs-{os.getpid()}-{index}')
            if cg.exists(): raise RuntimeError(f'cgroup already exists: {cg}')
            for k,v in dict(pebs_mode=row['mode'] or 0,pebs_force_order=0,
                            pebs_adaptive=0,pebs_load_period=row['load_period'],
                            pebs_store_period=row['store_period']).items(): write(DEBUG/k,v)
            before = snapshot(folder/'before.json')
            if row['mode'] is not None:
                sampler(row['mode']); active=True
            script, suite_env = SUITES[row['suite']]
            case_env = env | {k:str(v) for k,v in suite_env.items()} | dict(
                PAGE_SIZES_KB=str(row['page_kb']), RESULT_DIR=str(folder.resolve()/'results'),
                RUN_ID=name, PEBS_CGROUP=str(cg), PEBS_BENCH_MODE=row['case'])
            save_json(folder/'case.json',row | {'environment':case_env})
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
            valid = rc == 0 and not issues and (row['mode'] is None or delta['sampled'] > 0)
            save_json(folder/'status.json',dict(exit_code=rc,elapsed_sec=time.monotonic()-start,
                usable=valid,validation_issues=issues,pebs_delta=delta,perf_rate_changed=before['perf']!=after['perf']))
            if not valid: raise RuntimeError(f'{name}: failed workload or zero PEBS samples; see run.log')
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
        save_json(args.output/'completion.json',dict(completed=not failed,restore_errors=restore_errors))
        if restore_errors: print('RESTORE ERRORS: '+str(restore_errors),file=sys.stderr)
    if restore_errors: raise RuntimeError('environment restore incomplete; see completion.json and restore.json')


if __name__ == '__main__':
    main()
