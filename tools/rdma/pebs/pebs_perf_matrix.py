#!/usr/bin/env python3
"""Run as root; applications drop to --user. No kernel install or reboot."""
# The benchmark parses optional text fields from mutilate output; keep the
# research script permissive instead of annotating every regex lookup.
# pyright: reportOptionalSubscript=false, reportArgumentType=false, reportAttributeAccessIssue=false
import argparse, ctypes, csv, fcntl, json, os, pwd, random, re, signal, socket, subprocess, time, hashlib, shutil, resource
from pathlib import Path
D = Path('/sys/kernel/debug/hermit')
LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.restype = ctypes.c_long

def syscall(number, *args):
    if LIBC.syscall(ctypes.c_long(number), *(ctypes.c_long(a) for a in args)) < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))

def read(p): return Path(p).read_text()
def write(p, value): Path(p).write_text(str(value)+'\n')
def stats(port, timeout=5):
    deadline=time.monotonic()+timeout
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as s:
        s.settimeout(max(.001,deadline-time.monotonic()))
        s.sendall(b'stats\r\n'); data=b''
        while not data.endswith(b'END\r\n'):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('stats response deadline exceeded')
            s.settimeout(remaining)
            chunk=s.recv(65536)
            if not chunk: raise RuntimeError('stats closed')
            data+=chunk
    return data.decode()
def collect_final_stats(port, run, server):
    attempts=[]
    for attempt in range(1,4):
        start=time.monotonic()
        record={'attempt':attempt,'timeout_seconds':20}
        try:
            if server.poll() is not None:raise RuntimeError('Memcached exited before stats collection')
            response=stats(port,timeout=20)
            if not re.search(r'^STAT evictions \d+\r?$',response,re.M):
                raise RuntimeError('stats response missing evictions counter')
            (run/'memcached.stats').write_text(response)
            record['success']=True
        except (OSError,RuntimeError) as error:
            record.update(success=False,error=f'{type(error).__name__}: {error}')
        record['elapsed_seconds']=time.monotonic()-start
        attempts.append(record)
        (run/'stats-collection.json').write_text(json.dumps(attempts,indent=2)+'\n')
        if record['success']:return
        print(f"STATS attempt={attempt}/3 failed: {record['error']}",flush=True)
        if server.poll() is not None:break
    raise RuntimeError(f'Memcached stats unavailable after {len(attempts)} attempts; '
                       f'measurement is incomplete, inspect {run}/stats-collection.json')

def capture(folder, cg, pid):
    folder.mkdir()
    for f in ['order_stats','pebs_order_stats','remote_order_mask','effective_order_mask','pebs_mode','pebs_force_order','pebs_enabled']:
        (folder/f).write_text(read(D/f))
    for f in ['memory.current','memory.high','memory.max','memory.swap.current','memory.events','memory.stat']:
        (folder/f).write_text(read(cg/f))
    for f in ['stat','status','smaps_rollup','limits']:
        (folder/('server-'+f)).write_text(read(f'/proc/{pid}/{f}'))
    (folder/'perf-controls.json').write_text(json.dumps({p.name:read(p).strip() for p in Path('/proc/sys/kernel').glob('perf_*')}))
    (folder/'system-stat').write_text(read('/proc/stat'))
    wr=Path('/sys/module/rswap_client/parameters/wr_stats')
    if wr.exists(): (folder/'wr_stats').write_text(read(wr))
    for process in Path('/proc').glob('[0-9]*'):
        try:
            if read(process/'comm').strip() == 'hermit_pebsd':
                (folder/'sampler-stat').write_text(read(process/'stat'))
                (folder/'sampler-status').write_text(read(process/'status'))
                (folder/'sampler-affinity.json').write_text(json.dumps(sorted(os.sched_getaffinity(int(process.name)))))
        except (FileNotFoundError,ProcessLookupError): pass
    (folder/'vmstat').write_text(read('/proc/vmstat'))
    write(folder/'time',time.monotonic())
def latency(log):
    result={}
    for kind in ['read','update']:
        m=re.search(r'^'+kind+r'\s+(.+)$', log, re.M)
        if not m: raise RuntimeError('missing latency result')
        a=m[1].split();result[kind+'_avg_us']=float(a[0]);result[kind+'_p99_us']=float(a[7])
    result['qps']=float(re.search(r'Total QPS = ([\d.]+)',log)[1])
    total=re.search(r'Total QPS = [\d.]+ \((\d+) / ([\d.]+)s\)',log)
    result['total_requests']=int(total[1]);result['client_seconds']=float(total[2])
    for kind,pattern in [('miss_pct',r'Misses = \d+ \(([\d.]+)%\)'),('skipped_pct',r'Skipped TXs = \d+ \(([\d.]+)%\)')]:
        result[kind]=float(re.search(pattern,log)[1])
    return result

def thp_settings():
    root = Path('/sys/kernel/mm/transparent_hugepage')
    return {str(p): re.search(r'\[([^]]+)\]', read(p))[1] for p in root.rglob('enabled')}


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


def record_page_sizes(folder, pid):
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
            if '[vsyscall]' in line:
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
    for item in histogram.values():
        item['observed_folios'] = len(item.pop('folios'))
    result = dict(method='pagemap+kpageflags-compound-extent-v1', thp_settings=settings,
                  base_page_bytes=page, folio_size_kib=histogram, **counts,
                  elapsed_seconds=time.monotonic()-start,
                  limitations='Non-atomic snapshot of all server mappings, including libraries/shared pages. '
                  'Swapped/nonresident pages have no inferred folio size. Concurrent split/reclaim may race. '
                  'This is physical folio size, not malloc/slab size or RDMA WR size.')
    (folder/'page-sizes.json').write_text(json.dumps(result, indent=2)+'\n')
    print('PAGE-SIZES '+json.dumps({'stage':folder.name, **result}), flush=True)
    return result


def parse_cpu_list(value):
    cpus=set()
    for part in value.split(','):
        if '-' in part:
            lo,hi=map(int,part.split('-'))
            if lo>hi: raise ValueError('reversed CPU range')
            cpus.update(range(lo,hi+1))
        else: cpus.add(int(part))
    if not cpus or min(cpus)<0: raise ValueError('invalid CPU list')
    return sorted(cpus)


def bind_sampler(cpus):
    deadline=time.monotonic()+3
    while True:
        pids=[]
        for process in Path('/proc').glob('[0-9]*'):
            try:
                if read(process/'comm').strip()=='hermit_pebsd':pids.append(int(process.name))
            except (FileNotFoundError,ProcessLookupError):pass
        if len(pids)==1:break
        if time.monotonic()>deadline:raise RuntimeError(f'Expected one hermit_pebsd, found {pids}')
        time.sleep(.05)
    os.sched_setaffinity(pids[0],set(cpus))
    actual=sorted(os.sched_getaffinity(pids[0]))
    if actual!=sorted(cpus):raise RuntimeError('Sampler CPU affinity did not take effect')
    return {'pid':pids[0],'cpus':actual}


def configure_connection_limits(client_threads, connections):
    # Reserve for per-thread connections, epoll/event descriptors and control sockets.
    expected = client_threads * connections
    server_max = max(32768, expected + 1024)
    target = max(65536, 2 * server_max)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    new_hard = hard if hard == resource.RLIM_INFINITY else max(hard, target)
    new_soft = soft if soft == resource.RLIM_INFINITY else max(soft, target)
    resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, new_hard))
    actual = resource.getrlimit(resource.RLIMIT_NOFILE)
    if actual[0] != resource.RLIM_INFINITY and actual[0] < target:
        raise RuntimeError('Failed to raise test-process RLIMIT_NOFILE')
    return {'before': [soft, hard], 'effective': list(actual),
            'estimated_client_connections': expected, 'server_max_connections': server_max}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--user',required=True);ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--repeats',type=int,default=5);ap.add_argument('--duration',type=int,default=180)
    ap.add_argument('--warmup',type=int,default=30);ap.add_argument('--qps',type=int,default=30000)
    ap.add_argument('--thp',choices=['preserve'],default='preserve',help='Compatibility option; never writes THP settings')
    ap.add_argument('--port',type=int,default=11329);ap.add_argument('--require-fixed',action='store_true')
    ap.add_argument('--memory-mb',type=int,default=640)
    ap.add_argument('--records',type=int,default=800000)
    ap.add_argument('--server-cpus',help='CPU list, e.g. 0-7; default first four available CPUs')
    ap.add_argument('--client-cpus',help='CPU list; must not share physical cores with server')
    ap.add_argument('--connections',type=int,default=64)
    ap.add_argument('--sampler-cpus',help='hermit_pebsd affinity; default same CPUs as Memcached (same socket required)')

    ap.add_argument('--cases',default='',help='Comma separated subset; off disables PEBS; all cases preserve allocation settings; empty runs all cases')
    ap.add_argument('--diagnostic',action='store_true',help='Allow zero samples but explicitly mark sampling ineffective')
    args=ap.parse_args()
    if os.geteuid()!=0: ap.error('sudo is needed for cgroup and PEBS syscalls')
    if min(args.repeats,args.duration,args.warmup,args.memory_mb,args.records,args.connections)<=0 or args.qps<0: ap.error('parameters must be positive')
    lock=os.open('/run/lock/hermit-pebs-matrix.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    account=pwd.getpwnam(args.user);home=Path(account.pw_dir)
    if args.output.exists(): ap.error('output must be a new directory')
    if not (D/'pebs_enabled').exists(): ap.error('running kernel lacks PEBS interfaces, regardless of its release name')
    if read(D/'pebs_enabled').strip()!='0': ap.error('existing PEBS session; stop it before testing')
    if not Path('/sys/module/rswap_client').exists(): ap.error('RDMA backend is not loaded')
    if len(read('/proc/swaps').splitlines())<2: ap.error('no active swap device')
    if not Path('/sys/module/rswap_client/parameters/wr_stats').exists(): ap.error('loaded backend lacks RDMA WR counters')
    controls=['remote_order_mask','pebs_mode','pebs_force_order','reclaim_mode','reclaim_headroom_pages','sthd_cnt','bypass_swapcache','speculative_io','lazy_poll','apt_reclaim']
    fixed=all((D/f).exists() for f in ['pebs_adaptive','pebs_load_period','pebs_store_period'])
    if args.require_fixed and not fixed: ap.error('running kernel has no fixed-period controls; install the patched kernel first')
    if fixed: controls+=['pebs_adaptive','pebs_load_period','pebs_store_period']
    saved={f:read(D/f).strip() for f in controls}
    thp_control=Path('/sys/kernel/mm/transparent_hugepage/enabled')
    thp_saved={str(p):re.search(r'\[([^]]+)\]',read(p))[1] for p in thp_control.parent.rglob('enabled')}
    thp_before=re.search(r'\[([^]]+)\]',read(thp_control))[1]
    for name in ['memcached','mutilate','redis-server','hermit_pebsd']:
        if subprocess.run(['pgrep','-x',name],stdout=subprocess.DEVNULL).returncode==0:
            ap.error(f'existing {name}; refuse a conflicting test')
    cpus=sorted(os.sched_getaffinity(0))
    if len(cpus)<8: ap.error('at least 8 available CPUs required')
    try:
        server_ids=parse_cpu_list(args.server_cpus) if args.server_cpus else cpus[:4]
        client_ids=parse_cpu_list(args.client_cpus) if args.client_cpus else cpus[4:8]
        sampler_ids=parse_cpu_list(args.sampler_cpus) if args.sampler_cpus else server_ids
    except ValueError as exc: ap.error(str(exc))
    if not set(server_ids+client_ids+sampler_ids)<=set(cpus):ap.error('CPU selection outside allowed affinity')
    topology={}
    for line in subprocess.check_output(['lscpu','-p=CPU,CORE,SOCKET'],text=True).splitlines():
        if not line.startswith('#'):
            cpu,core,sock=map(int,line.split(','));topology[cpu]=(sock,core)
    if {topology[c] for c in server_ids}&{topology[c] for c in client_ids}:
        ap.error('server/client CPU sets overlap physical cores, including SMT siblings')
    sockets={topology[c][0] for c in server_ids}
    if len(sockets)!=1 or {topology[c][0] for c in sampler_ids}!=sockets:
        ap.error('Memcached and sampler CPUs must be on the same single socket')
    if {topology[c] for c in sampler_ids}&{topology[c] for c in client_ids}:
        ap.error('sampler/client CPU sets share physical cores')
    server_cpus=','.join(map(str,server_ids));client_cpus=','.join(map(str,client_ids))
    try: connection_limits=configure_connection_limits(len(client_ids),args.connections)
    except (ValueError,OSError) as exc: ap.error(f'Cannot raise file descriptor limits: {exc}')
    with socket.socket() as s: s.bind(('127.0.0.1',args.port))
    for binary in [home/'memcached/memcached',home/'mutilate/mutilate']:
        if not os.access(binary,os.X_OK): ap.error(f'missing {binary}')
    out=args.output;out.mkdir(parents=True)
    shutil.copyfile(__file__,out/'driver.py')
    manifest={'args':vars(args)|{'output':str(out)},'kernel':os.uname().release,'controls_before':saved,
              'server_cpus':server_cpus,'client_cpus':client_cpus,'fixed_controls_available':fixed,
              'records':args.records,'valuesize':1024,'memory_high':args.memory_mb*1024**2,'memory_max':max(2048,args.memory_mb)*1024**2,'sampling':'no NumaMMa'}
    manifest['connection_limits']=connection_limits
    manifest['policy_tunables']={k:read(D/k).strip() for k in ['pebs_fixed_cost_ns','pebs_bw_mibps','pebs_min_f','pebs_hysteresis','pebs_cooling_period','pebs_region_max']}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    for command,name in [(['lscpu','-e'],'cpu-topology.txt'),(['uname','-a'],'uname.txt')]:
        (out/name).write_text(subprocess.check_output(command,text=True))
    thp=Path('/sys/kernel/mm/transparent_hugepage')
    (out/'thp.json').write_text(json.dumps({str(p):read(p) for p in thp.rglob('enabled')},indent=2))
    (out/'modules.txt').write_text(read('/proc/modules'))
    (out/'backend-symbols.txt').write_text('\n'.join(l for l in read('/proc/kallsyms').splitlines() if 'rswap_rdma' in l))
    cases=[('off-original-mask',None,None),('off',None,None),('off-same-pages',None,None),('static-adaptive',0,None),('policy-adaptive',1,None)]
    if fixed:
        for label,period in [('high',199),('medium',1999),('low',19997)]:
            # Vary L3-miss period alone; keep stores period fixed to isolate it.
            for mode in [0,1]: cases.append((f'{["static","policy"][mode]}-{label}',mode,period))
    if args.cases:
        selected=args.cases.split(',')
        if set(selected)-{c[0] for c in cases}: ap.error('unknown case')
        cases=[c for c in cases if c[0] in selected]
    manifest['cases']=cases
    manifest['sampling_cost_reference']='off-same-pages'
    manifest['sampler_cpus']=sampler_ids
    manifest['pebs_event_scope']='all online CPUs; affinity only constrains hermit_pebsd'
    manifest['server_sampler_socket']=next(iter(sockets))
    manifest['off_baseline']='natural-pages-v3'
    manifest['off_description']='System THP/mTHP unchanged, remote_order_mask=0x1, PEBS disabled; NOT a pure 4 KiB baseline'
    manifest['reclaim']={'mode':1,'headroom_pages':16384,'workers':4}
    manifest['binaries_sha256']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [home/'memcached/memcached',home/'mutilate/mutilate']}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    (out/'dmesg-before.txt').write_text(subprocess.check_output(['dmesg'],text=True))
    rows=[];active=False;server=None;cg=None
    def interrupted(signum, frame):
        raise InterruptedError(f'test interrupted by signal {signum}')
    for signum in [signal.SIGTERM,signal.SIGHUP]:signal.signal(signum,interrupted)
    def drop(group=None):
        def fn():
            if group: write(group/'cgroup.procs',os.getpid())
            os.initgroups(args.user,account.pw_gid);os.setgid(account.pw_gid);os.setuid(account.pw_uid)
        return fn
    def client(command, logfile, timeout):
        before=resource.getrusage(resource.RUSAGE_CHILDREN);start=time.monotonic()
        with logfile.open('w') as log:
            try:
                subprocess.run(['taskset','-c',client_cpus]+command,preexec_fn=drop(),stdout=log,stderr=subprocess.STDOUT,check=True,timeout=timeout)
            except subprocess.CalledProcessError as exc:
                log.flush()
                tail=logfile.read_text()[-2500:]
                raise RuntimeError(f'Client failed (exit {exc.returncode}); log: {logfile}\n{tail}') from exc
        after=resource.getrusage(resource.RUSAGE_CHILDREN)
        return {'cpu_seconds':after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,'wall_seconds':time.monotonic()-start}
    try:
        for key,value in {'bypass_swapcache':'Y','speculative_io':'Y','lazy_poll':'N','apt_reclaim':'Y'}.items():write(D/key,value)
        write(D/'reclaim_mode',1);write(D/'reclaim_headroom_pages',16384);write(D/'sthd_cnt',4)
        for repeat in range(1,args.repeats+1):
            order=cases.copy();random.Random(20261006+repeat).shuffle(order)
            for label,mode,period in order:
                actual_thp=thp_settings()
                if actual_thp!=thp_saved:raise RuntimeError('External THP configuration change detected')
                print(f'START repeat={repeat} case={label}',flush=True)
                write(out/'progress',f'repeat={repeat} case={label}')
                run=out/f'r{repeat}-{label}';run.mkdir();cg=Path('/sys/fs/cgroup')/f'hermit-pebs-perf-{os.getpid()}';cg.mkdir()
                write(cg/'memory.max',max(2048,args.memory_mb)*1024**2);write(cg/'memory.swap.max','max')
                write(D/'remote_order_mask','0x1' if label=='off' else saved['remote_order_mask'] if label=='off-original-mask' else '0x3fd')
                write(D/'pebs_force_order',0)
                if mode is not None: write(D/'pebs_mode',mode)
                if fixed:
                    write(D/'pebs_adaptive',int(period is None));write(D/'pebs_load_period',period or 0);write(D/'pebs_store_period',1500003 if period else 0)
                command=['numactl','--membind=0','taskset','-c',server_cpus,str(home/'memcached/memcached'),'-l','127.0.0.1','-p',str(args.port),'-U','0','-m','1024','-c',str(connection_limits['server_max_connections']),'-t',str(len(server_ids))]
                with (run/'server.log').open('w') as log:
                    server=subprocess.Popen(command,preexec_fn=drop(cg),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                deadline=time.monotonic()+30
                while True:
                    if server.poll() is not None: raise RuntimeError(f'server exited; see {run}')
                    try:
                        text=stats(args.port)
                        if int(re.search(r'STAT pid (\d+)',text)[1])!=server.pid: raise RuntimeError('unexpected listener')
                        break
                    except OSError:
                        if time.monotonic()>deadline: raise
                        time.sleep(.2)
                # System-wide events cover all worker threads; the server pid
                # opts its mm into history without any cgroup dependency.
                sampler_affinity=None
                if mode is not None:
                    syscall(473,server.pid,mode);active=True
                    sampler_affinity=bind_sampler(sampler_ids)
                base=[str(home/'mutilate/mutilate'),'-s',f'127.0.0.1:{args.port}','-r',str(args.records),'--keysize=30','--valuesize=1024']
                client(base+['--loadonly'],run/'load.log',600)
                record_page_sizes(run/'pages-loaded',server.pid)
                write(cg/'memory.high',args.memory_mb*1024**2)
                requests=base+['--noload','-T',str(len(client_ids)),'-c',str(args.connections),'-q',str(args.qps),'--update=0.1','--iadist=fb_ia']
                client(requests+['-t',str(args.warmup)],run/'warmup.log',args.warmup+120)
                (run/'config.json').write_text(json.dumps({'case':label,'mode':mode,
                    'load_period':period,'store_period':1500003 if period else None,
                    'sampler_affinity':sampler_affinity,'pebs_event_scope':'all online CPUs',
                    'controls':{f:read(D/f) for f in controls},'thp':read(thp_control),
                    'thp_settings':actual_thp,'off_baseline':'natural-pages-v3',
                    'page_allocation':'preserve-system'},indent=2))
                pages_before=record_page_sizes(run/'pages-before',server.pid)
                capture(run/'before',cg,server.pid)
                client_usage=client(requests+['-t',str(args.duration)],run/'measure.log',args.duration+120)
                capture(run/'after',cg,server.pid)
                pages_after=record_page_sizes(run/'pages-after',server.pid)
                collect_final_stats(args.port,run,server)
                if sampler_affinity is not None and sorted(os.sched_getaffinity(sampler_affinity['pid']))!=sampler_ids:
                    raise RuntimeError('Sampler affinity changed during measurement')
                row={'repeat':repeat,'case':label,**latency(read(run/'measure.log'))}
                row['duration_seconds']=float(read(run/'after/time'))-float(read(run/'before/time'))
                dt=float(read(run/'after/time'))-float(read(run/'before/time'))
                for counter in ['sampled','lost','throttled','unknown']:
                    values=[]
                    for tag in ['before','after']:
                        m=re.search(r'^'+counter+r': (\d+)$',read(run/tag/'pebs_order_stats'),re.M)
                        values.append(int(m[1]) if m else 0)
                    row[counter+'_delta']=values[1]-values[0]
                row['samples_per_second']=row['sampled_delta']/dt
                row['backend_store_bytes']=0;row['backend_load_bytes']=0
                for tag,sign in [('before',-1),('after',1)]:
                    for line in read(run/tag/'order_stats').splitlines()[1:]:
                        fields=list(map(int,line.split()))
                        row['backend_store_bytes']+=sign*fields[1]*fields[2]
                        row['backend_load_bytes']+=sign*fields[1]*fields[3]
                cpu=[]
                for tag in ['before','after']:
                    fields=read(run/tag/'server-stat').rsplit(')',1)[1].split();cpu.append(int(fields[11])+int(fields[12]))
                row['server_cpu_seconds']=(cpu[1]-cpu[0])/os.sysconf('SC_CLK_TCK')
                row['server_cpu_capacity_pct']=100*row['server_cpu_seconds']/dt/len(server_ids)
                row['client_cpu_seconds']=client_usage['cpu_seconds']
                row['client_cpu_capacity_pct']=100*client_usage['cpu_seconds']/client_usage['wall_seconds']/len(client_ids)
                row['perf_controls_changed']=int(read(run/'before/perf-controls.json')!=read(run/'after/perf-controls.json'))

                if (run/'before/sampler-stat').exists():
                    ticks=[]
                    for tag in ['before','after']:
                        fields=read(run/tag/'sampler-stat').rsplit(')',1)[1].split()
                        ticks.append(int(fields[11])+int(fields[12]))
                    row['sampler_cpu_seconds']=(ticks[1]-ticks[0])/os.sysconf('SC_CLK_TCK')
                else: row['sampler_cpu_seconds']=0
                def table(path):
                    return {int(v[0]):list(map(int,v[1:])) for l in read(path).splitlines() if (v:=l.split()) and v[0].isdigit()}
                old,new=table(run/'before/order_stats'),table(run/'after/order_stats')
                row['order_errors_delta']=sum(v[-1]-old[k][-1] for k,v in new.items())
                row['folio_fallback_delta']=sum(v[-2]-old[k][-2] for k,v in new.items())
                row['large_load_pct']=100*sum(v[0]*(v[2]-old[k][2]) for k,v in new.items() if k>0)/max(row['backend_load_bytes'],1)
                b={}
                for event in ['oom','oom_kill']:
                    a,b=[dict(l.split() for l in read(run/tag/'memory.events').splitlines()) for tag in ['before','after']]
                    row[event+'_delta']=int(b[event])-int(a[event])
                row['oom_kill_total']=int(b['oom_kill'])
                a,b=table(run/'before/wr_stats'),table(run/'after/wr_stats')
                for index,name in enumerate(['write_wrs','read_wrs','write_bytes','read_bytes']):
                    row[name]=sum(v[index]-a[k][index] for k,v in b.items())
                row['large_wr_write_pct']=100*sum(v[2]-a[k][2] for k,v in b.items() if k>0)/max(row['write_bytes'],1)
                row['large_wr_read_pct']=100*sum(v[3]-a[k][3] for k,v in b.items() if k>0)/max(row['read_bytes'],1)
                row['page_allocation']='preserve-system'
                row['page_settings_unchanged']=int(pages_before['thp_settings']==thp_saved==pages_after['thp_settings'])
                for stage, snapshot in [('before',pages_before),('after',pages_after)]:
                    sizes=snapshot['folio_size_kib']
                    row[f'folio_sizes_kib_{stage}']=','.join(sorted(sizes,key=int)) if isinstance(sizes,dict) else ''
                    row[f'page_census_unknown_bytes_{stage}']=snapshot['unknown_resident_bytes']
                for order_idx in range(10):
                    vals=[]
                    for tag in ['before','after']:
                        match=re.search(r'^\s*'+str(order_idx)+r': (\d+)$',read(run/tag/'pebs_order_stats'),re.M)
                        vals.append(int(match[1]) if match else 0)
                    row[f'decisions_order{order_idx}']=vals[1]-vals[0]
                busy_before=0
                for tag in ['before','after']:
                    vals=list(map(int,read(run/tag/'system-stat').splitlines()[0].split()[1:]))
                    busy=sum(vals[:8])-vals[3]-vals[4]
                    if tag=='before':busy_before=busy
                    else:row['system_busy_cpu_seconds']=(busy-busy_before)/os.sysconf('SC_CLK_TCK')
                row['memcached_evictions']=int(re.search(r'STAT evictions (\d+)',read(run/'memcached.stats'))[1])
                row['workload_valid']=int(not any(row[k] for k in ['oom_kill_total','order_errors_delta','memcached_evictions','miss_pct','skipped_pct']))
                row['valid']=row['workload_valid']*row['page_settings_unchanged']*int(mode is None or row['sampled_delta']>0)
                row['sampling_effective']=int(mode is not None and row['sampled_delta']>0)
                row['policy_effective']=int(mode==1 and sum(row[f'decisions_order{i}'] for i in range(10))>0)
                rows.append(row)
                with (out/'summary.csv').open('w') as f:
                    w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
                print(json.dumps(row),flush=True)
                if mode is not None and not row['sampling_effective'] and not args.diagnostic:raise RuntimeError('PEBS produced zero usable samples; measurement is not a valid sampling benchmark')
                if not row['page_settings_unchanged']:raise RuntimeError(f'External THP configuration change: {run}')
                if not row['workload_valid']:raise RuntimeError(f'invalid measurement: {run}')
                if active: syscall(474,server.pid);active=False
                server.terminate();server.wait(timeout=30);server=None
                cg.rmdir();cg=None
        write(out/'status','completed')
    except BaseException as e:
        write(out/'status','failed: '+str(e));raise
    finally:
        cleanup_errors=[]
        def attempt(label, action):
            try:action()
            except Exception as error:cleanup_errors.append(f'{label}: {error}')
        if active:attempt('stop sampler',lambda:syscall(474,0))
        if server is not None and server.poll() is None:
            def stop_server():
                os.killpg(server.pid,signal.SIGTERM)
                try:server.wait(timeout=30)
                except subprocess.TimeoutExpired:os.killpg(server.pid,signal.SIGKILL);server.wait()
            attempt('stop server',stop_server)
        if cg is not None:attempt('remove cgroup',cg.rmdir)
        for f,value in saved.items():attempt('restore '+f,lambda f=f,value=value:write(D/f,value))
        (out/'dmesg-after.txt').write_text(subprocess.check_output(['dmesg'],text=True))
        restored={'controls':{f:read(D/f).strip() for f in saved},'thp':{p:re.search(r'\[([^]]+)\]',read(p))[1] for p in thp_saved},'pebs_enabled':read(D/'pebs_enabled').strip(),'cleanup_errors':cleanup_errors}
        if restored['controls']!=saved or restored['thp']!=thp_saved or restored['pebs_enabled']!='0':cleanup_errors.append('restored values differ')
        (out/'restore.json').write_text(json.dumps(restored,indent=2))
        if cleanup_errors:write(out/'status','cleanup failed: '+str(cleanup_errors))
        for p in [out,*out.rglob('*')]: os.chown(p,account.pw_uid,account.pw_gid)
        if cleanup_errors:raise RuntimeError(str(cleanup_errors))
if __name__=='__main__': main()
