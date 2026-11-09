#!/usr/bin/env python3
"""Frozen-input Hermit off/static/policy matrix over THP allocation ceilings."""
import argparse
import contextlib
import csv
import ctypes
import fcntl
import hashlib
import json
import os
import platform
import pwd
import random
import re
import resource
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

from policy_app_worker import create_database

HERE = Path(__file__).resolve().parent
DEBUG = Path('/sys/kernel/debug/hermit')
THP = Path('/sys/kernel/mm/transparent_hugepage')
APPS = ('sort', 'sqlite-query', 'sqlite-update', 'gapbs-bfs', 'gapbs-pr')
PAGE = 4096
PAGE_SIZES_KB = (4, 16, 32, 64, 128, 256, 512, 1024, 2048)
PAGE_POLICY = 'max-allocation-v1'
SAMPLING_CONTROLS = ('pebs_mode', 'pebs_all', 'pebs_adaptive',
                     'pebs_load_period', 'pebs_store_period')
PAIR_KEYS = ('application', 'concurrency', 'ratio', 'max_page_kb', 'repeat')
# debugfs reads can execute test hooks such as pebs_ring_test.
# Snapshot only known passive counters and configuration.
HERMIT_SNAPSHOT_FILES = (
    'order_stats', 'pebs_order_stats', 'policy_stats',
    'remote_order_mask', 'effective_order_mask',
    'bypass_swapcache', 'batch_swapout', 'batch_tlb', 'batch_io',
    'batch_account', 'vaddr_swapout', 'speculative_io', 'speculative_lock',
    'lazy_poll', 'apt_reclaim', 'sthd_cnt', 'reclaim_mode',
    'reclaim_headroom_pages', 'reclaim_stop_headroom_pages',
    'pebs_enabled', 'pebs_all', 'pebs_mode',
    'pebs_adaptive', 'pebs_load_period', 'pebs_store_period',
    'pebs_window_ms', 'pebs_history_ms', 'pebs_pc_max',
    'pebs_fixed_cost_ns', 'pebs_region_max',
    'pebs_predict_regions', 'pebs_predict_samples',
)
DRAM_SNAPSHOT_FILES = ('stores', 'loads', 'load_misses', 'errors')


def dump(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')
    temp.replace(path)


def read(path):
    return Path(path).read_text().strip()


def write(path, value):
    Path(path).write_text(str(value) + '\n')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def cpulist(text):
    ids = set()
    for part in text.split(','):
        bounds = [int(x) for x in part.split('-')]
        if len(bounds) == 1:
            ids.add(bounds[0])
        elif len(bounds) == 2 and bounds[1] >= bounds[0]:
            ids.update(range(bounds[0], bounds[1] + 1))
        else:
            raise ValueError('invalid CPU list: ' + text)
    if not ids:
        raise ValueError('empty CPU list')
    return sorted(ids)


def syscall(number, *args):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    if libc.syscall(ctypes.c_long(number), *(ctypes.c_long(x) for x in args)) < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))


def load_config(path):
    c = json.loads(Path(path).read_text())
    for name in ('sort', 'sqlite', 'gapbs', 'user', 'cpus'):
        if name not in c:
            raise ValueError('missing config field: ' + name)
    c.setdefault('seed', 73)
    c.setdefault('repeats', 3)
    c.setdefault('ratios', [90, 70, 50])
    c.setdefault('concurrency', [1, 8])
    c.setdefault('page_sizes_kb', list(PAGE_SIZES_KB))
    c.setdefault('modes', ['off', 'static', 'policy'])
    c.setdefault('periods', [19997, 1999, 199])
    c.setdefault('store_period', 1500003)
    c.setdefault('timeout_seconds', 1800)
    c.setdefault('controls', {})
    owned = set(SAMPLING_CONTROLS) | {'pebs_enabled', 'remote_order_mask'}
    if owned & c['controls'].keys():
        raise ValueError('controls must not override sampling or backend orders; use modes/periods/store_period: '
                         + ', '.join(sorted(owned & c['controls'].keys())))
    if not c['page_sizes_kb'] or set(c['page_sizes_kb']) - set(PAGE_SIZES_KB):
        raise ValueError('page_sizes_kb must select from ' + str(PAGE_SIZES_KB))
    if 'off' not in c['modes'] or set(c['modes']) - {'off', 'static', 'policy'}:
        raise ValueError('modes must include the off baseline and may include static/policy')
    if not c['periods'] or min(c['periods'] + [c['store_period']]) < 1:
        raise ValueError('sampling periods must be positive')
    for key in ('page_sizes_kb', 'modes', 'periods', 'concurrency', 'ratios'):
        if len(c[key]) != len(set(c[key])):
            raise ValueError('duplicate selections in ' + key)
    if c['repeats'] < 1 or c['timeout_seconds'] <= 0:
        raise ValueError('repeats and timeout must be positive')
    if not c['ratios'] or any(not 0 < n < 100 for n in c['ratios']):
        raise ValueError('ratios must be percentages between 0 and 100')
    if not c['concurrency'] or any(n < 1 or n > len(cpulist(c['cpus'])) for n in c['concurrency']):
        raise ValueError('concurrency must fit the selected CPUs')
    if c['sort']['rows'] <= 0 or c['sort']['record_bytes'] < 18:
        raise ValueError('sort requires positive rows and record_bytes >= 18')
    if any(c['sqlite'][key] <= 0 for key in ('rows', 'operations', 'transaction_size')):
        raise ValueError('SQLite sizes must be positive')
    if c['gapbs']['trials'] < 1:
        raise ValueError('GAPBS trials must be positive')
    for group, keys in [('sort', ['binary']), ('gapbs', ['bfs', 'pr', 'graph'])]:
        for key in keys:
            c[group][key] = str(Path(c[group][key]).expanduser().resolve())
    c['python'] = str(Path(sys.executable).resolve())
    return c


def identity(config):
    import _sqlite3
    paths = [config['sort']['binary'], config['gapbs']['bfs'], config['gapbs']['pr'],
             config['python'], _sqlite3.__file__, str(HERE / 'policy_app_worker.py'),
             str(Path(__file__).resolve())]
    return {p: sha(p) for p in paths}


def environment():
    paths = [Path('/sys/kernel/mm/transparent_hugepage/enabled'),
             Path('/sys/kernel/mm/transparent_hugepage/defrag'),
             Path('/sys/module/zswap/parameters/enabled'), Path('/proc/sys/vm/swappiness')]
    paths += sorted(Path('/sys/kernel/mm/transparent_hugepage').glob('hugepages-*/enabled'))
    return {str(p): read(p) for p in paths if p.exists()}


def thp_settings():
    paths = [THP / 'enabled'] + sorted(THP.glob('hugepages-*kB/enabled'))
    return {str(p): re.search(r'\[([^]]+)\]', read(p))[1] for p in paths if p.exists()}


def page_settings(kb, saved):
    """Same max-allocation-v1 semantics as sweep_common.sh, never force_order."""
    selected = THP / f'hugepages-{kb}kB/enabled'
    if kb != 4 and str(selected) not in saved and not (kb == 2048 and str(THP / 'enabled') in saved):
        raise ValueError(f'{kb} KiB THP allocation ceiling is unavailable')
    settings = {str(THP / 'enabled'): 'always' if kb == 2048 else 'never'}
    for path in saved:
        match = re.search(r'hugepages-(\d+)kB/enabled$', path)
        if match:
            settings[path] = 'always' if 16 <= int(match[1]) <= kb else 'never'
    return settings


def snapshot(cg):
    paths = {f'cgroup/{name}': cg / name for name in
             ('memory.current', 'memory.peak', 'memory.high', 'memory.max',
              'memory.swap.current', 'memory.swap.peak', 'memory.events', 'memory.stat',
              'memory.pressure', 'cpu.stat', 'cpu.pressure', 'io.stat', 'io.pressure')}
    paths.update({f'hermit/{name}': DEBUG / name for name in HERMIT_SNAPSHOT_FILES
                  if (DEBUG / name).is_file()})
    paths.update({f'mglru/{name}': Path('/sys/kernel/mm/lru_gen') / name
                  for name in ('enabled', 'min_ttl_ms')
                  if (Path('/sys/kernel/mm/lru_gen') / name).is_file()})
    dram = Path('/sys/kernel/debug/rswap_dram')
    paths.update({f'dram/{name}': dram / name for name in DRAM_SNAPSHOT_FILES
                  if (dram / name).is_file()})
    paths.update({'vmstat': Path('/proc/vmstat'), 'system_cpu': Path('/proc/stat'),
                  'memory_psi': Path('/proc/pressure/memory'),
                  'io_psi': Path('/proc/pressure/io'), 'cpu_psi': Path('/proc/pressure/cpu'),
                  'wr_stats': Path('/sys/module/rswap_client/parameters/wr_stats')})
    data = {'monotonic_ns': time.monotonic_ns(), 'values': {}, 'errors': {},
            'thp': thp_settings()}
    for key, path in paths.items():
        try:
            data['values'][key] = read(path)
        except OSError as error:
            data['errors'][key] = str(error)
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            if read(proc / 'comm') == 'hermit_pebsd':
                data['values']['sampler_stat'] = read(proc / 'stat')
                data['values']['sampler_affinity'] = sorted(os.sched_getaffinity(int(proc.name)))
        except (OSError, ValueError):
            pass
    return data


def counters(text):
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                result[parts[0].rstrip(':')] = int(parts[1])
            except ValueError:
                pass
    return result


def wr_totals(text):
    """Legacy power-of-two WR rows and independent arbitrary SG rows are additive."""
    totals = dict(write_wrs=0, read_wrs=0, write_bytes=0, read_bytes=0)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 5 and all(p.isdigit() for p in parts):
            for key, count in zip(totals, parts[1:]):
                totals[key] += int(count)
    return totals


def deltas(before, after):
    result = {}
    for key in ('vmstat', 'cgroup/memory.stat', 'cgroup/memory.events', 'cgroup/cpu.stat', 'hermit/policy_stats'):
        a = counters(before['values'].get(key, ''))
        b = counters(after['values'].get(key, ''))
        result[key] = {k: b[k] - a[k] for k in a.keys() & b.keys()}
    if 'wr_stats' in before['values'] and 'wr_stats' in after['values']:
        a, b = wr_totals(before['values']['wr_stats']), wr_totals(after['values']['wr_stats'])
        result['wr_stats'] = {k: b[k] - a[k] for k in a}
    for key in ('memory_psi', 'cpu_psi', 'io_psi', 'cgroup/memory.pressure', 'cgroup/cpu.pressure'):
        def totals(snapshot):
            return {line.split()[0]: int(re.search(r'total=(\d+)', line)[1])
                    for line in snapshot['values'].get(key, '').splitlines() if 'total=' in line}
        a, b = totals(before), totals(after)
        result[key] = {k: b[k] - a[k] for k in a.keys() & b.keys()}
    return result


def granularity(before, after):
    def tables(state):
        rows = {}
        for name in ('hermit/order_stats', 'wr_stats'):
            header = []
            for line in state['values'].get(name, '').splitlines():
                parts = line.split()
                if parts and parts[0] in ('order', 'pages'):
                    header = parts
                elif header and len(parts) == len(header) and all(p.isdigit() for p in parts):
                    values = dict(zip(header, map(int, parts)))
                    size = (PAGE << values['order']) if 'order' in values else PAGE * values['pages']
                    row = rows.setdefault(size // 1024, {})
                    for key, value in values.items():
                        if key in ('order', 'pages', 'size_bytes'):
                            continue
                        key = 'folio_' + key if name == 'hermit/order_stats' else key.removeprefix('batch_')
                        row[key] = row.get(key, 0) + value
        return rows
    a, b = tables(before), tables(after)
    return [dict(size_kib=size, **{key: b[size].get(key, 0) - a[size].get(key, 0)
                                for key in a[size].keys() | b[size].keys()})
            for size in sorted(a.keys() & b.keys())]


def command(config, app, concurrency, run):
    env = {**os.environ, 'LC_ALL': 'C', 'OMP_NUM_THREADS': str(concurrency),
           'OMP_PROC_BIND': 'true', 'OMP_PLACES': 'cores'}
    if app == 'sort':
        c = config['sort']
        cmd = [c['binary'], '--parallel=' + str(concurrency), '-S', str(c['buffer_bytes']),
               '-T', str(run), '-o', str(run / 'sorted.txt'), c['input']]
    elif app.startswith('sqlite-'):
        cmd = [config['python'], str(HERE / 'policy_app_worker.py'), '--config', str(run / 'config.json'),
               '--mode', app.split('-')[1], '--threads', str(concurrency), '--output', str(run / 'application.json')]
    else:
        cmd = [config['gapbs'][app.split('-')[1]], '-f', config['gapbs']['graph'],
               '-n', str(config['gapbs']['trials']), '-v']
    return cmd, env


def check_result(config, app, run, expected=None):
    if app == 'sort':
        # Independent order verification, outside the measured process/counters.
        subprocess.run([config['sort']['binary'], '-c', str(run / 'sorted.txt')],
                       env={**os.environ, 'LC_ALL': 'C'}, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=config['timeout_seconds'])
        digest = sha(run / 'sorted.txt')
        return dict(valid=expected is None or digest == expected, sha256=digest)
    if app.startswith('sqlite-'):
        return json.loads((run / 'application.json').read_text())
    log = (run / 'application.log').read_text(errors='replace')
    verified = re.findall(config['gapbs'].get('verification_regex', r'Verification\s*:\s*PASS'), log)
    return dict(valid=bool(verified) and not re.search(r'Verification\s*:\s*FAIL', log, re.I),
                verification_passes=len(verified),
                kernel_seconds=[float(x) for x in re.findall(r'(?:Trial Time|Average Time):\s*([0-9.eE+-]+)', log)])


def require_host(config):
    if os.geteuid() != 0:
        raise PermissionError('prepare/calibration and run require root for cgroup and Hermit controls')
    if os.sysconf('SC_PAGE_SIZE') != PAGE:
        raise RuntimeError('this matrix requires a 4 KiB base-page kernel')
    if not set(cpulist(config['cpus'])).issubset(os.sched_getaffinity(0)):
        raise ValueError('requested CPUs are unavailable to this process')
    if 'memory' not in read('/sys/fs/cgroup/cgroup.subtree_control').split():
        raise RuntimeError('enable the cgroup v2 memory controller before running; script will not alter parent controllers')
    if not Path('/sys/module/rswap_client').is_dir():
        raise RuntimeError('load the matching rswap backend before running the matrix')
    if len(read('/proc/swaps').splitlines()) < 2:
        raise RuntimeError('no swap device is active; configure existing swap before running')
    force_local = Path('/sys/module/rswap_client/parameters/force_local')
    if force_local.exists() and read(force_local).lower() in ('y', '1'):
        raise RuntimeError('rswap force_local is enabled; remote-swap measurements would be invalid')
    if read(DEBUG / 'pebs_enabled') != '0':
        raise RuntimeError('a PEBS session is already active; script will not stop somebody else\'s session')
    required = ((1 << (max(config['page_sizes_kb']) // 4).bit_length()) - 1) & ~2
    if int(read(DEBUG / 'effective_order_mask'), 0) & required != required:
        raise RuntimeError(f'backend orders exclude selected ceilings; configure remote_order_mask/backend '
                           f'to support {required:#x} first (the matrix preserves this mask)')
    saved = thp_settings()
    if str(THP / 'enabled') not in saved:
        raise RuntimeError('THP allocation controls are unavailable')
    for kb in config['page_sizes_kb']:
        page_settings(kb, saved)
    if config['user'] == '':
        raise ValueError('config.user must name the application account')
    pwd.getpwnam(config['user'])
    for path in [config['sort']['binary'], config['gapbs']['bfs'], config['gapbs']['pr']]:
        if not os.access(path, os.X_OK):
            raise FileNotFoundError('executable not available: ' + path)
    if not Path(config['gapbs']['graph']).is_file():
        raise FileNotFoundError(config['gapbs']['graph'])


@contextlib.contextmanager
def lock_host():
    with open('/tmp/hermit-policy-matrix.lock', 'a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@contextlib.contextmanager
def controls(config):
    saved, thp = {}, thp_settings()
    errors = []
    try:
        for key in list(config['controls']) + list(SAMPLING_CONTROLS):
            if '/' in key or not (DEBUG / key).is_file():
                raise ValueError('unavailable Hermit control: ' + key)
            saved[key] = read(DEBUG / key)
        for key, value in config['controls'].items():
            write(DEBUG / key, value)
        if read(DEBUG / 'bypass_swapcache') not in ('Y', '1'):
            raise RuntimeError('off/static reference paths require bypass_swapcache=Y, as in the existing matrix')
        yield {'debugfs': saved, 'thp': thp}
    finally:
        for key, value in reversed(list(saved.items())):
            try:
                write(DEBUG / key, value)
            except OSError as error:
                errors.append(f'restore {key}: {error}')
        for path, value in thp.items():
            try:
                write(path, value)
            except OSError as error:
                errors.append(f'restore {path}: {error}')
        if errors:
            raise RuntimeError('; '.join(errors))


def sampling_settings(cell):
    return dict(pebs_mode=0 if cell['mode'] is None else cell['mode'],
                pebs_all=0 if cell['mode'] is None else 1, pebs_adaptive=0,
                pebs_load_period=cell['load_period'], pebs_store_period=cell['store_period'])


@contextlib.contextmanager
def case_controls(cell, thp):
    started = False
    if read(DEBUG / 'pebs_enabled') != '0':
        raise RuntimeError('sampling must be stopped between matrix cells')
    for path, value in page_settings(cell['max_page_kb'], thp).items():
        write(path, value)
    for key, value in sampling_settings(cell).items():
        write(DEBUG / key, value)
    try:
        if cell['mode'] is not None:
            syscall(473, -1, cell['mode'])
            started = True
        yield
    finally:
        if started:
            syscall(474, -1)


def verify_case(state, cell):
    if state['thp'] != page_settings(cell['max_page_kb'], state['thp']):
        raise RuntimeError('THP allocation ceiling changed during the cell')
    expected = sampling_settings(cell) | {'pebs_enabled': int(cell['mode'] is not None)}
    for key, value in expected.items():
        if int(state['values'].get('hermit/' + key, '-1'), 0) != value:
            raise RuntimeError('sampling configuration mismatch: ' + key)


def kill_group(proc, cg):
    if (cg / 'cgroup.kill').exists():
        write(cg / 'cgroup.kill', 1)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait()


def execute(config, cell, run, budget=None, expected=None):
    run.mkdir(parents=True, exist_ok=False)
    account = pwd.getpwnam(config['user'])
    os.chown(run, account.pw_uid, account.pw_gid)
    dump(run / 'config.json', config)
    cg = Path('/sys/fs/cgroup') / f'hermit-app-{os.getpid()}'
    cg.mkdir()
    proc = None
    result = dict(cell=cell, success=False, status='starting')
    gate_r, gate_w = os.pipe()
    try:
        write(cg / 'memory.max', budget if budget is not None else 'max')
        write(cg / 'memory.high', 'max')
        write(cg / 'memory.swap.max', 'max' if budget is not None else 0)
        write(cg / 'memory.oom.group', 1)
        cmd, env = command(config, cell['application'], cell['concurrency'], run)
        result.update(command=cmd, budget_bytes=budget)
        dump(run / 'result.json', result)

        def enter():
            write(cg / 'cgroup.procs', os.getpid())
            os.sched_setaffinity(0, cpulist(config['cpus']))
            os.initgroups(config['user'], account.pw_gid)
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)

        before = snapshot(cg)
        dump(run / 'before.json', before)
        verify_case(before, cell)
        usage0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        with (run / 'application.log').open('w') as log:
            proc = subprocess.Popen([config['python'], str(Path(__file__).resolve()), '_exec',
                                     str(gate_r)] + cmd, env=env, preexec_fn=enter,
                                    pass_fds=(gate_r,), stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, cwd=run)
            os.close(gate_r)
            gate_r = -1
            start = time.monotonic()
            os.write(gate_w, b'1')
            os.close(gate_w)
            gate_w = -1
            try:
                proc.wait(timeout=config['timeout_seconds'])
                result['status'] = 'exited'
            except subprocess.TimeoutExpired:
                result['status'] = 'timeout'
                kill_group(proc, cg)
            result['process_seconds'] = time.monotonic() - start
        usage1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        after = snapshot(cg)
        dump(run / 'after.json', after)
        result.update(exit_code=proc.returncode, resident_peak_bytes=int(read(cg / 'memory.peak')),
                      child_cpu_seconds=usage1.ru_utime + usage1.ru_stime - usage0.ru_utime - usage0.ru_stime,
                      counters=deltas(before, after), granularity=granularity(before, after))
        verify_case(after, cell)
        for key in ('remote_order_mask', 'effective_order_mask'):
            if before['values'].get('hermit/' + key) != after['values'].get('hermit/' + key):
                raise RuntimeError('backend order mask changed during the cell: ' + key)
        if proc.returncode == 0:
            result['application'] = check_result(config, cell['application'], run, expected)
            result['success'] = bool(result['application']['valid'])
            result['status'] = 'ok' if result['success'] else 'verification_failed'
            if result['success'] and cell['application'] == 'sort':
                (run / 'sorted.txt').unlink()
    except KeyboardInterrupt:
        result.update(status='interrupted')
        raise
    except Exception as error:
        result.update(status='failed', error=f'{type(error).__name__}: {error}')
    finally:
        for fd in (gate_r, gate_w):
            if fd >= 0:
                os.close(fd)
        if proc is not None and proc.poll() is None:
            kill_group(proc, cg)
        try:
            # Swapcache charges may outlive the process; removing this dedicated
            # group reparents them through the normal cgroup lifecycle.
            cg.rmdir()
        except OSError as error:
            result['cleanup_error'] = str(error)
            result['success'] = False
        dump(run / 'result.json', result)
    return result


def prepare(args):
    c = load_config(args.config)
    work = args.workdir.resolve()
    c['sort']['input'] = str(work / 'inputs' / 'sort.txt')
    c['sqlite']['database'] = str(work / 'inputs' / 'sqlite.db')
    if args.dry_run:
        print(json.dumps(dict(action='prepare', config=c, manifest=str(args.manifest),
                              calibration=[dict(application=a, concurrency=n, **cases(c)[0],
                                                max_page_kb=max(c['page_sizes_kb']))
                                           for a in APPS for n in c['concurrency']],
                              cases=cases(c), page_policy=PAGE_POLICY,
                              cells_per_run=len(APPS) * len(c['concurrency']) * len(c['ratios'])
                              * len(c['page_sizes_kb']) * len(cases(c)) * c['repeats'],
                              writes=False, workload_execution=False), indent=2))
        return
    require_host(c)
    if args.manifest.exists() or work.exists():
        raise FileExistsError('manifest/workdir must be new; frozen inputs are never overwritten')
    with lock_host():
        inputs = work / 'inputs'
        inputs.mkdir(parents=True)
        rng = random.Random(c['seed'])
        width = c['sort']['record_bytes']
        with Path(c['sort']['input']).open('wb') as f:
            for _ in range(c['sort']['rows']):
                f.write(f'{rng.getrandbits(64):016x}'.encode() + b'x' * (width - 17) + b'\n')
        create_database(c['sqlite']['database'], c['sqlite']['rows'], c['sqlite']['payload_bytes'])
        for path in inputs.iterdir():
            path.chmod(0o444)
        fingerprints = identity(c)
        inputs_sha = {p: sha(p) for p in (c['sort']['input'], c['sqlite']['database'], c['gapbs']['graph'])}
        reference, budgets, sort_hash = {}, {}, None
        with controls(c) as saved:
            dump(work / 'saved-controls.json', saved)
            calibration = cases(c)[0] | {'max_page_kb': max(c['page_sizes_kb'])}
            with case_controls(calibration, saved['thp']):
                for app in APPS:
                    for n in c['concurrency']:
                        key = f'{app}-c{n}'
                        print('CALIBRATE off ' + key, flush=True)
                        cell = dict(application=app, concurrency=n, **calibration)
                        result = execute(c, cell, work / 'calibration' / key,
                                         expected=sort_hash if app == 'sort' else None)
                        reference[key] = result
                        dump(work / 'calibration-progress.json', reference)
                        if not result['success']:
                            raise RuntimeError('calibration failed; inspect ' + str(work / 'calibration' / key))
                        if app == 'sort':
                            sort_hash = result['application']['sha256']
                        peak = result['resident_peak_bytes']
                        budgets[key] = {str(r): max(PAGE, (peak * r // 100) // PAGE * PAGE) for r in c['ratios']}
        manifest = dict(version=2, created_ns=time.time_ns(), config=c, inputs_sha256=inputs_sha,
                        binaries_sha256=fingerprints, budgets_bytes=budgets, reference=reference,
                        sort_sha256=sort_hash, environment=environment(), calibration_kernel=platform.release(),
                        sqlite_version=sqlite3.sqlite_version, baseline_case='off', page_policy=PAGE_POLICY,
                        calibration_case=calibration,
                        capacity_basis='off at largest selected allocation ceiling, unlimited-memory, swap-disabled cgroup memory.peak; includes charged file cache and kernel memory',
                        counter_scope='whole process including load and verification; SQLite operation latency excludes load/verification')
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        dump(args.manifest, manifest)
        print('FROZEN ' + str(args.manifest), flush=True)


def cases(config):
    rows = [dict(case='off', strategy='off', mode=None, load_period=0, store_period=config['store_period'])]
    for period in config['periods']:
        for strategy, mode in [('static', 0), ('policy', 1)]:
            if strategy in config['modes']:
                rows.append(dict(case=f'{strategy}-{period}', strategy=strategy, mode=mode,
                                 load_period=period, store_period=config['store_period']))
    return rows


def schedule(manifest):
    c = manifest['config']
    cells, rng = [], random.Random(c['seed'])
    for repeat in range(1, c['repeats'] + 1):
        for app in APPS:
            for n in c['concurrency']:
                for ratio in c['ratios']:
                    for kb in c['page_sizes_kb']:
                        # Keep matched off/static/policy cells in one randomized block.
                        block = cases(c)
                        rng.shuffle(block)
                        for case in block:
                            cells.append(dict(application=app, concurrency=n, ratio=ratio, repeat=repeat,
                                              max_page_kb=kb, **case,
                                              budget_bytes=manifest['budgets_bytes'][f'{app}-c{n}'][str(ratio)]))
    return cells


def write_csv(path, rows):
    fields = sorted({key for row in rows for key in row})
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(rows)


def run_matrix(args):
    m = json.loads(args.manifest.read_text())
    if m['version'] != 2:
        raise ValueError('requires a version-2 off/static/policy manifest; prepare with the updated config')
    c, cells = m['config'], schedule(m)
    if args.dry_run:
        print(json.dumps(dict(label=args.label, manifest_sha256=sha(args.manifest), cells_per_run=len(cells),
                              baseline_case='off', page_policy=PAGE_POLICY, cells=cells,
                              common_controls=c['controls'], sampling_controls='per case; off never starts the sampler',
                              writes=False, workload_execution=False), indent=2))
        return
    require_host(c)
    if environment() != m['environment']:
        raise RuntimeError('initial THP/zswap/swappiness differs from calibration; restore the frozen settings before running')
    if identity(c) != m['binaries_sha256']:
        raise RuntimeError('application/script binaries changed since prepare; use the frozen files')
    for path, digest in m['inputs_sha256'].items():
        if sha(path) != digest:
            raise RuntimeError('frozen input changed: ' + path)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    header = dict(version=2, label=args.label, baseline_case='off', page_policy=PAGE_POLICY,
                  kernel=platform.release(), uname=list(platform.uname()),
                  manifest_sha256=sha(args.manifest), manifest=str(args.manifest.resolve()),
                  environment=environment(), started_ns=time.time_ns(), schedule=cells,
                  boot_id=read('/proc/sys/kernel/random/boot_id'),
                  kernel_version=read('/proc/version'),
                  backend='rdma' if Path('/sys/module/rswap_client/parameters/wr_stats').exists() else 'dram')
    dump(out / 'run.json', header)
    results = []
    try:
        with lock_host(), controls(c) as saved:
            dump(out / 'saved-controls.json', saved)
            for cell in cells:
                name = '{application}-c{concurrency}-m{ratio}-max{max_page_kb}k-r{repeat}-{case}'.format(**cell)
                print(f'RUN {args.label} {name}', flush=True)
                try:
                    with case_controls(cell, saved['thp']):
                        result = execute(c, cell, out / name, cell['budget_bytes'], m['sort_sha256'])
                except Exception as error:
                    # A failed sampler/control transition cannot silently become an off run.
                    folder = out / name
                    folder.mkdir(exist_ok=True)
                    path = folder / 'result.json'
                    result = json.loads(path.read_text()) if path.exists() else dict(cell=cell)
                    result.update(success=False, status='control_failed', error=str(error))
                    dump(path, result)
                    results.append(result)
                    dump(out / 'results.json', results)
                    raise
                results.append(result)
                dump(out / 'results.json', results)
                print(f"  {result['status']}", flush=True)
    finally:
        header['finished_ns'] = time.time_ns()
        header['successful'] = sum(r['success'] for r in results)
        header['failed'] = len(results) - header['successful']
        header['uncompleted'] = len(cells) - len(results)
        dump(out / 'run.json', header)
        write_csv(out / 'granularity.csv', [r['cell'] | dict(success=r['success']) | g
                                          for r in results for g in r.get('granularity', [])])
    if header['failed']:
        raise SystemExit(f"{header['failed']} cells failed; retained in {out}")


def compare(args):
    header = json.loads((args.run / 'run.json').read_text())
    if header.get('version') != 2 or header.get('baseline_case') != 'off':
        raise ValueError('compare requires one off/static/policy matrix run')
    results = {}
    for path in args.run.glob('*/result.json'):
        result = json.loads(path.read_text())
        key = tuple(result['cell'][k] for k in PAIR_KEYS) + (result['cell']['case'],)
        if key in results:
            raise ValueError('duplicate matrix cell: ' + str(key))
        results[key] = result
    rows = []
    for cell in header['schedule']:
        if cell['case'] == 'off':
            continue
        key = tuple(cell[k] for k in PAIR_KEYS)
        baseline = results.get(key + ('off',))
        case = results.get(key + (cell['case'],))
        row = {k: cell[k] for k in PAIR_KEYS + ('case', 'strategy', 'load_period', 'store_period', 'budget_bytes')}
        row.update(baseline_case='off', baseline_status=baseline['status'] if baseline else 'missing',
                   case_status=case['status'] if case else 'missing', valid_pair=False)
        if baseline and case:
            if baseline['cell']['budget_bytes'] != cell['budget_bytes'] or case['cell']['budget_bytes'] != cell['budget_bytes']:
                raise ValueError('paired cells have different frozen memory budgets')
            row['valid_pair'] = baseline['success'] and case['success']
        if row['valid_pair']:
            row['process_speedup'] = baseline['process_seconds'] / case['process_seconds']
            for prefix, result in [('baseline', baseline), ('case', case)]:
                row[prefix + '_seconds'] = result['process_seconds']
                row[prefix + '_cpu_seconds'] = result['child_cpu_seconds']
                for counter in ('pswpin', 'pswpout'):
                    row[prefix + '_' + counter] = result['counters']['vmstat'].get(counter)
                for counter, count in result['counters'].get('wr_stats', {}).items():
                    row[prefix + '_' + counter] = count
                for metric in ('throughput_ops_s', 'latency_p50_us', 'latency_p95_us', 'latency_p99_us'):
                    row[prefix + '_' + metric] = result['application'].get(metric)
            for metric in ('pswpin', 'pswpout', 'read_wrs', 'write_wrs', 'read_bytes', 'write_bytes',
                           'latency_p99_us', 'cpu_seconds'):
                b, v = row.get('baseline_' + metric), row.get('case_' + metric)
                row[metric + '_reduction_pct'] = 100 * (1 - v / b) if b is not None and b > 0 and v is not None else None
        rows.append(row)
    args.output.mkdir(parents=True, exist_ok=False)
    dump(args.output / 'paired.json', rows)
    write_csv(args.output / 'paired.csv', rows)
    groups = {}
    for row in rows:
        key = f"{row['application']}-c{row['concurrency']}-m{row['ratio']}-max{row['max_page_kb']}k-{row['case']}"
        groups.setdefault(key, []).append(row)
    summary = {}
    metrics = ['process_speedup', 'read_wrs_reduction_pct', 'write_wrs_reduction_pct',
               'pswpin_reduction_pct', 'pswpout_reduction_pct', 'latency_p99_us_reduction_pct',
               'cpu_seconds_reduction_pct']
    for key, group in groups.items():
        summary[key] = dict(pairs=len(group), valid_pairs=sum(r['valid_pair'] for r in group))
        for metric in metrics:
            values = [r[metric] for r in group if r.get(metric) is not None]
            summary[key]['median_' + metric] = statistics.median(values) if values else None
    dump(args.output / 'summary.json', summary)
    print(str(args.output / 'paired.csv'))


def main():
    # The child joins its cgroup and drops privileges before reaching this gate.
    if len(sys.argv) > 1 and sys.argv[1] == '_exec':
        fd = int(sys.argv[2])
        if os.read(fd, 1) != b'1':
            raise SystemExit('launch gate closed')
        os.close(fd)
        os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    a = sub.add_parser('prepare', help='generate inputs and calibrate once with policy off')
    a.add_argument('--config', type=Path, required=True)
    a.add_argument('--workdir', type=Path, required=True)
    a.add_argument('--manifest', type=Path, required=True)
    a.add_argument('--dry-run', action='store_true')
    a = sub.add_parser('run', help='run the frozen matrix on the currently installed kernel')
    a.add_argument('--manifest', type=Path, required=True)
    a.add_argument('--label', default='matrix', help='free-form run label; the baseline is always the off case')
    a.add_argument('--output', type=Path, required=True)
    a.add_argument('--dry-run', action='store_true')
    a = sub.add_parser('compare')
    a.add_argument('--run', type=Path, required=True, help='one completed or partial matrix directory')
    a.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f'interrupted by signal {signum}')
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    {'prepare': prepare, 'run': run_matrix, 'compare': compare}[args.action](args)


if __name__ == '__main__':
    main()
