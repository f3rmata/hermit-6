#!/usr/bin/env python3
"""Controlled PEBS page-selection experiment. Run explicitly; never calibrates peaks."""
import argparse
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import random
import selectors
import shutil
import signal
import subprocess
import time

from page_selection_metrics import D, WR, THP, read, write, dump, thp_settings
from page_selection_metrics import record_page_sizes, snapshot, deltas, table

HERE = Path(__file__).resolve().parent
SIZES = [4, 16, 32, 64, 128, 256, 512, 1024, 2048]
SCENARIOS = {'sparse': [1], 'density16': [16], 'density64': [64],
             'density256': [256], 'dense': [512], 'mixed': [0],
             'switch': [1, 512, 1]}
CASES = {'fixed-4k': (4, None), 'fixed-64k': (64, None),
         'fixed-512k': (512, None), 'fixed-2048k': (2048, None),
         'all-off': (None, None), 'sample-only': (None, 0), 'dynamic': (None, 1)}
CONTROLS = {'pebs_force_order': 0, 'pebs_adaptive': 0,
            'reclaim_mode': 1, 'reclaim_headroom_pages': 16384, 'sthd_cnt': 4,
            'bypass_swapcache': 'Y', 'speculative_io': 'Y', 'lazy_poll': 'N',
            'apt_reclaim': 'Y'}
LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.restype = ctypes.c_long


def syscall(n, *args):
    if LIBC.syscall(ctypes.c_long(n), *(ctypes.c_long(a) for a in args)) < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))


def cpulist(value):
    result = set()
    for item in value.split(','):
        ends = list(map(int, item.split('-')))
        if len(ends) == 1:
            result.add(ends[0])
        elif len(ends) == 2 and ends[0] <= ends[1]:
            result.update(range(ends[0], ends[1] + 1))
        else:
            raise ValueError('Invalid CPU range')
    if not result or min(result) < 0:
        raise ValueError('Invalid CPUs')
    return result


def plan(args):
    runs = []
    for repeat in range(1, args.repeats + 1):
        block = [(s, c) for s in args.scenarios.split(',') for c in args.cases.split(',')]
        random.Random(args.seed + repeat).shuffle(block)
        for scenario, case in block:
            runs.append(dict(repeat=repeat, scenario=scenario, case=case,
                             seed=args.seed + repeat, phases=SCENARIOS[scenario]))
    return runs


def configure_pages(kib):
    for path in thp_settings():
        write(path, 'never')
    for size in ([kib] if kib else SIZES[1:]):
        if size != 4:
            write(THP/f'hugepages-{size}kB/enabled', 'madvise')
    if kib == 2048 or kib is None:
        write(THP/'enabled', 'madvise')
    mask = (1 | (1 << ((kib // 4).bit_length() - 1))) if kib else 0x3fd
    write(D/'remote_order_mask', hex(mask))
    if int(read(D/'effective_order_mask').strip(), 0) & mask != mask:
        raise RuntimeError('Effective backend mask does not support requested orders')
    return thp_settings()


def response(process, event, log, timeout):
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        data = b''
        while b'\n' not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError('Worker response timeout: ' + event)
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Worker exited waiting for ' + event)
            data += chunk
    log.write(data.decode()); log.flush()
    try:
        result = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError('Malformed worker response') from exc
    if result['event'] != event:
        raise RuntimeError('Unexpected worker response')
    return result


def command(process, text, event, log, timeout):
    process.stdin.write((text + '\n').encode()); process.stdin.flush()
    return response(process, event, log, timeout)


def quiet(timeout):
    """Drain posted IO before ending pageout interval; this is not a peak search."""
    start = time.monotonic()
    previous = read(WR)
    stable = 0
    while stable < 3:
        if time.monotonic() - start > timeout:
            raise TimeoutError('RDMA counters did not settle; possible other workload')
        time.sleep(.1)
        current = read(WR)
        stable = stable + 1 if current == previous else 0
        previous = current
    return time.monotonic() - start


def bind_sampler(cpus):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        found = []
        for p in Path('/proc').glob('[0-9]*'):
            try:
                if read(p/'comm').strip() == 'hermit_pebsd':
                    found.append(int(p.name))
            except FileNotFoundError:
                pass
        if len(found) == 1:
            os.sched_setaffinity(found[0], cpus)
            if os.sched_getaffinity(found[0]) != cpus:
                raise RuntimeError('Sampler affinity mismatch')
            return found[0]
        time.sleep(.05)
    raise RuntimeError('Expected one hermit_pebsd')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--user', required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--workset-mb', type=int, default=8192)
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--epochs', type=int, default=5, help='Cycles per density phase')
    ap.add_argument('--passes', type=int, default=4, help='Access passes per cycle')
    ap.add_argument('--warmup-passes', type=int, default=8)
    ap.add_argument('--seed', type=int, default=20261012)
    ap.add_argument('--cpu', type=int, default=0, help='One workload thread')
    ap.add_argument('--sampler-cpus', default='0', help='Default shares workload CPU')
    ap.add_argument('--node', type=int, default=0)
    ap.add_argument('--load-period', type=int, default=1999)
    ap.add_argument('--store-period', type=int, default=1500003)
    ap.add_argument('--timeout', type=int, default=600)
    ap.add_argument('--cases', default=','.join(CASES))
    ap.add_argument('--scenarios', default=','.join(SCENARIOS))
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    if min(args.workset_mb, args.repeats, args.epochs, args.passes, args.warmup_passes,
           args.seed, args.load_period, args.store_period, args.timeout) <= 0:
        ap.error('Numeric experiment parameters must be positive')
    if args.workset_mb % 2 or min(args.cpu, args.node) < 0:
        ap.error('Workset must be a multiple of 2 MiB; CPU/node must be nonnegative')
    for text, options in [(args.cases, CASES), (args.scenarios, SCENARIOS)]:
        selected = text.split(',')
        if len(set(selected)) != len(selected) or not set(selected) <= options.keys():
            ap.error('Unknown or duplicate case/scenario: ' + text)
    runs = plan(args)
    if args.dry_run:
        print(json.dumps({'args': vars(args) | {'output': str(args.output)}, 'runs': runs,
                          'measurement_epochs': sum(len(r['phases']) * args.epochs for r in runs)}, indent=2))
        return
    if os.geteuid() != 0:
        ap.error('Use sudo; workload drops to --user')
    for tool in ['cc', 'numactl', 'taskset', 'pgrep']:
        if not shutil.which(tool):
            ap.error('Missing executable: ' + tool)
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0:
        ap.error('--user must be an unprivileged account')
    if args.output.exists():
        ap.error('Output must be a new directory')
    lock = os.open('/run/lock/hermit-pebs-matrix.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    sampler_cpus = cpulist(args.sampler_cpus)
    if not (sampler_cpus | {args.cpu}) <= os.sched_getaffinity(0):
        ap.error('Requested CPU unavailable')
    if os.sysconf('SC_PAGE_SIZE') != 4096 or os.uname().machine != 'x86_64':
        ap.error('Requires the x86_64 Hermit kernel with 4 KiB base pages')
    if read(D/'pebs_enabled').strip() != '0':
        ap.error('Existing PEBS session; refuse to disturb it')
    for name in ['memcached', 'mutilate', 'redis-server', 'hermit_pebsd', 'page-selection']:
        if subprocess.run(['pgrep', '-x', name], stdout=subprocess.DEVNULL).returncode == 0:
            ap.error('Conflicting process: ' + name)
    if not WR.exists() or not Path('/sys/module/rswap_client').exists():
        ap.error('RDMA backend WR counters unavailable')
    if Path('/sys/kernel/debug/rswap_dram').exists():
        ap.error('DRAM backend is not an RDMA experiment')
    swaps = read('/proc/swaps').splitlines()[1:]
    if len(swaps) != 1 or swaps[0].split()[1] != 'partition':
        ap.error('Requires exactly one active block swap device')
    free_swap_kib = int(swaps[0].split()[2]) - int(swaps[0].split()[3])
    if free_swap_kib < args.workset_mb * 1024:
        ap.error('Insufficient free swap for complete workset')
    zswap = Path('/sys/module/zswap/parameters/enabled')
    if zswap.exists() and read(zswap).strip().lower() in ('y', '1'):
        ap.error('zswap enabled')
    if 'memory' not in read('/sys/fs/cgroup/cgroup.subtree_control').split():
        ap.error('Root cgroup memory controller must already be enabled')
    available_kib = int(next(line.split()[1] for line in read('/proc/meminfo').splitlines()
                             if line.startswith('MemAvailable:')))
    if available_kib < (args.workset_mb + 1024) * 1024:
        ap.error('Insufficient available RAM to isolate explicit pageout from capacity pressure')
    saved_thp = thp_settings()
    needed_sizes = set()
    for c in args.cases.split(','):
        kib, _ = CASES[c]
        needed_sizes.update([kib] if kib else SIZES[1:])
    for size in needed_sizes - {4}:
        if not (THP/f'hugepages-{size}kB/enabled').exists():
            ap.error(f'Missing {size} KiB THP control')
    settings = CONTROLS | {'pebs_load_period': args.load_period,
                           'pebs_store_period': args.store_period}
    saved = {k: read(D/k).strip() for k in [*settings, 'remote_order_mask', 'pebs_mode']}
    out = args.output.resolve(); out.mkdir(parents=True)
    out.chmod(0o755)
    source = out/'source'; source.mkdir()
    for p in HERE.glob('*page_selection*'):
        if p.is_file():
            shutil.copy2(p, source/p.name)
    binary = out/'page-selection'
    subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-std=c11',
                    str(HERE/'page_selection_workset.c'), '-o', str(binary)], check=True)
    binary.chmod(0o755)
    manifest = {'schema': 'page-selection-v1', 'args': vars(args) | {'output': str(out)},
                'runs': runs, 'kernel': os.uname().release, 'controls_before': saved,
                'thp_before': saved_thp, 'swap': read('/proc/swaps'),
                'event_scope': 'all online CPUs; sampled is global, not target-attributed',
                'protocol': 'explicit MADV_PAGEOUT then accesses; no memory.high pressure, no peak calibration',
                'allocation_tunables': {str(p): read(p).strip() for p in
                    [THP/'defrag', THP/'khugepaged'/'defrag'] if p.exists()},
                'inherited_hermit_flags': {k: read(D/k).strip() for k in
                    ['batch_swapout', 'batch_tlb', 'batch_io', 'batch_account', 'vaddr_swapout',
                     'speculative_lock', 'exclusive_swapout', 'swap_thread', 'prefetch_thread']
                    if (D/k).exists()},
                'sources_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()},
                'policy_tunables': {p.name: read(p).strip() for p in D.glob('pebs_*')
                                    if p.name in ['pebs_fixed_cost_ns', 'pebs_bw_mibps', 'pebs_min_f',
                                                  'pebs_hysteresis', 'pebs_cooling_period', 'pebs_region_max']}}
    dump(out/'manifest.json', manifest)
    write(out/'lscpu.txt', subprocess.check_output(['lscpu', '-e'], text=True))
    write(out/'status', 'running')
    process = None; cg = None; active = False
    def interrupted(signum, _frame):
        raise InterruptedError(f'Signal {signum}')
    for sig in [signal.SIGINT, signal.SIGTERM, signal.SIGHUP]:
        signal.signal(sig, interrupted)
    def drop():
        assert cg is not None
        write(cg/'cgroup.procs', os.getpid())
        os.initgroups(args.user, account.pw_gid)
        os.setgid(account.pw_gid); os.setuid(account.pw_uid)
    expected_thp = saved_thp
    perf_reference = {p.name: read(p).strip() for p in Path('/proc/sys/kernel').glob('perf_*')}
    try:
        for k, v in settings.items():
            write(D/k, v)
        for index, spec in enumerate(runs, 1):
            if thp_settings() != expected_thp:
                raise RuntimeError('External THP change detected')
            label = f'{index:04d}-r{spec["repeat"]}-{spec["scenario"]}-{spec["case"]}'
            print(label, flush=True); write(out/'progress', label)
            run = out/label; run.mkdir()
            kib, mode = CASES[spec['case']]
            expected_thp = configure_pages(kib)
            write(D/'pebs_mode', mode or 0)
            cg = Path('/sys/fs/cgroup')/f'hermit-page-selection-{os.getpid()}'
            cg.mkdir()
            write(cg/'memory.max', (args.workset_mb + 512) * 1024**2)
            write(cg/'memory.high', 'max'); write(cg/'memory.swap.max', 'max')
            with (run/'worker.jsonl').open('w') as log, (run/'stderr.log').open('w') as err:
                worker = subprocess.Popen(['numactl', f'--membind={args.node}', 'taskset', '-c',
                    str(args.cpu), str(binary), str(args.workset_mb), str(spec['seed']),
                    'base' if kib == 4 else 'huge'], preexec_fn=drop,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, start_new_session=True)
                process = worker
                response(worker, 'boot', log, args.timeout)
                ready = command(worker, 'init', 'ready', log, args.timeout)
                def census(folder):
                    return record_page_sizes(folder, worker.pid, ready['address'], ready['bytes'])
                census(run/'pages-loaded')
                sampler_pid = None
                if mode is not None:
                    syscall(473, worker.pid, mode); active = True
                    sampler_pid = bind_sampler(sampler_cpus)
                config = spec | {'requested_kib': kib, 'mode': mode, 'thp': expected_thp,
                                 'mapping': ready, 'sampler_pid': sampler_pid,
                                 'controls': {k: read(D/k).strip() for k in saved}}
                dump(run/'config.json', config)
                first = spec['phases'][0]
                warmup = command(process, f'access {first or 1} {args.warmup_passes} {int(first == 0)}',
                                 'access', log, args.timeout)
                dump(run/'warmup.json', warmup)
                if warmup['checksum_errors']:
                    raise RuntimeError('Warmup checksum failed')
                for phase, density in enumerate(spec['phases'], 1):
                    for epoch in range(1, args.epochs + 1):
                        cycle = run/f'p{phase}-e{epoch}'; cycle.mkdir()
                        before_pages = census(cycle/'pages-before')
                        snapshot(cycle/'before', cg, process.pid)
                        pageout = command(process, 'pageout', 'pageout', log, args.timeout)
                        pageout['settle_seconds'] = quiet(args.timeout)
                        snapshot(cycle/'after-pageout', cg, process.pid)
                        evicted_pages = census(cycle/'pages-evicted')
                        snapshot(cycle/'before-access', cg, process.pid)
                        access = command(process, f'access {density or 1} {args.passes} {int(density == 0)}',
                                         'access', log, args.timeout)
                        snapshot(cycle/'after-access', cg, process.pid)
                        after_pages = census(cycle/'pages-after')
                        out_delta = deltas(cycle/'before', cycle/'after-pageout')
                        in_delta = deltas(cycle/'before-access', cycle/'after-access')
                        warnings = []
                        if json.loads(read(cycle/'before-access'/'perf.json')) != perf_reference:
                            warnings.append('Perf controls differ from campaign start')
                        if sum(v['write_bytes'] for v in in_delta['wr'].values()):
                            warnings.append('RDMA writes during access; not an isolated load interval')
                        for tag, delta in [('pageout', out_delta), ('access', in_delta)]:
                            if delta['pebs']['lost'] or delta['pebs']['throttled']:
                                warnings.append(tag + ': sampling lost/throttled')
                            if delta['perf_controls_changed']:
                                warnings.append(tag + ': perf controls changed')
                        if mode is not None and in_delta['pebs']['sampled'] == 0:
                            warnings.append('No global samples during access; target sampling unverified')
                        if not sum(v['read_bytes'] for v in in_delta['wr'].values()):
                            warnings.append('No RDMA reads during access')
                        if evicted_pages['swapped_mapped_bytes'] < ready['bytes']:
                            warnings.append('Pageout is incomplete; inspect resident/swapped distribution')
                        if any(p['unknown_resident_bytes'] for p in [before_pages, evicted_pages, after_pages]):
                            warnings.append('Unknown resident folio sizes')
                        events = dict(line.split() for line in read(cg/'memory.events').splitlines())
                        valid = not access['checksum_errors'] and not int(events['oom_kill'])
                        valid &= not any(v['errors'] for d in [out_delta, in_delta] for v in d['order'].values())
                        row = spec | {'phase': phase, 'epoch': epoch, 'density_pages': density,
                                      'pageout': pageout, 'access': access, 'pageout_delta': out_delta,
                                      'access_delta': in_delta, 'warnings': warnings, 'valid': bool(valid)}
                        dump(cycle/'result.json', row)
                        print(f'  p{phase} e{epoch}: {access["seconds"]:.6f}s; warnings={len(warnings)}', flush=True)
                        for key, value in settings.items():
                            if read(D/key).strip() != str(value):
                                raise RuntimeError('External Hermit control change: ' + key)
                        if read(D/'pebs_enabled').strip() != str(int(mode is not None)):
                            raise RuntimeError('Unexpected PEBS enabled state')
                        if not valid:
                            raise RuntimeError('Invalid measurement; inspect ' + str(cycle))
                        if thp_settings() != expected_thp:
                            raise RuntimeError('External THP change detected')
                        if sampler_pid and os.sched_getaffinity(sampler_pid) != sampler_cpus:
                            raise RuntimeError('Sampler affinity changed')
                if active:
                    syscall(474, 0); active = False
                assert process is not None and process.stdin is not None
                process.stdin.write(b'quit\n'); process.stdin.flush()
                if process.wait(timeout=30):
                    raise RuntimeError('Worker failed at exit')
                process = None
            cg.rmdir(); cg = None
        write(out/'status', 'completed')
    except BaseException as exc:
        write(out/'status', 'failed: ' + repr(exc))
        raise
    finally:
        # Each restoration is independent so one failure cannot skip the rest.
        errors = []
        def attempt(label, fn):
            try:
                fn()
            except Exception as exc:
                errors.append(label + ': ' + repr(exc))
        if active:
            attempt('stop PEBS', lambda: syscall(474, 0))
        if process is not None and process.poll() is None:
            attempt('kill worker', lambda: os.killpg(process.pid, signal.SIGKILL))
            attempt('wait worker', lambda: process.wait(timeout=30))
        if cg is not None:
            attempt('remove cgroup', cg.rmdir)
        for k, v in saved.items():
            attempt('restore ' + k, lambda k=k, v=v: write(D/k, v))
        for p, v in saved_thp.items():
            attempt('restore ' + p, lambda p=p, v=v: write(p, v))
        restored: dict[str, object] = {'errors': errors}
        attempt('read controls', lambda: restored.update(controls={k: read(D/k).strip() for k in saved}))
        attempt('read THP', lambda: restored.update(thp=thp_settings()))
        attempt('read PEBS', lambda: restored.update(pebs_enabled=read(D/'pebs_enabled').strip()))
        if restored.get('controls') != saved or restored.get('thp') != saved_thp or restored.get('pebs_enabled') != '0':
            errors.append('Restored state differs from saved state')
        dump(out/'restore.json', restored)
        for p in [out, *out.rglob('*')]:
            os.chown(p, account.pw_uid, account.pw_gid)
        if errors:
            write(out/'status', 'restore failed')
            raise RuntimeError(str(errors))


if __name__ == '__main__':
    main()
