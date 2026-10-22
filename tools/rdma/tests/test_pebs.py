#!/usr/bin/env python3
"""Offline checks for PEBS benchmark drivers, page selection, and mm scope."""
import csv
import hashlib
import io
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

RDMA = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[3]
KERNEL = ROOT / 'linux-stable'
TOOLS = RDMA

COST_PATH = RDMA / 'pebs/run_memcached_sampling_cost.py'
cost_spec = importlib.util.spec_from_file_location('cost', COST_PATH)
assert cost_spec is not None and cost_spec.loader is not None
cost = importlib.util.module_from_spec(cost_spec)
cost_spec.loader.exec_module(cost)

THP_PATH = RDMA / 'pebs/pebs_perf_matrix.py'
thp_spec = importlib.util.spec_from_file_location('thp', THP_PATH)
assert thp_spec is not None and thp_spec.loader is not None
thp = importlib.util.module_from_spec(thp_spec)
thp_spec.loader.exec_module(thp)
THP_ROOT = '/sys/kernel/mm/transparent_hugepage/'

PEBS = RDMA / 'pebs'
sys.path.insert(0, str(PEBS))
MATRIX_PATH = RDMA / 'pebs/run_workload_matrix.py'
matrix_spec = importlib.util.spec_from_file_location('matrix', MATRIX_PATH)
assert matrix_spec is not None and matrix_spec.loader is not None
matrix = importlib.util.module_from_spec(matrix_spec)
matrix_spec.loader.exec_module(matrix)



import page_selection_metrics as metrics
import run_page_selection_microbench as runner


class FixedConcurrencyTests(unittest.TestCase):
    def test_summary_uses_paired_loss_and_flags_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            (folder/'status').write_text('completed')
            rows=[]
            for repeat,off,on in [(1,1000,900),(2,2000,1600)]:
                for case,qps in [('off',off/2),('off-same-pages',off),('static-high',on),('policy-high',on*1.2)]:
                    rows.append(dict(repeat=repeat,case=case,qps=qps,valid=1,read_p99_us=20,
                        server_cpu_capacity_pct=90,client_cpu_capacity_pct=40,system_busy_cpu_seconds=1,
                        total_requests=1000,samples_per_second=10,lost_delta=int(case=='static-high'),
                        throttled_delta=0,perf_controls_changed=0,read_bytes=0,write_bytes=0))
            with (folder/'summary.csv').open('w') as f:
                w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
            summary,flags=cost.summarize(folder)
            row=next(r for r in summary if r['case']=='static-high')
            self.assertAlmostEqual(row['paired_qps_loss_pct_median'],15)
            self.assertEqual(row['lost_cases'],2)
            self.assertTrue(flags)
            policy=next(r for r in summary if r['case']=='policy-high')
            self.assertAlmostEqual(policy['qps_gain_vs_static_pct_median'],20)
            self.assertAlmostEqual(row['qps_gain_vs_off_pct_median'],70)

    def test_dry_run_includes_both_baselines_static_and_policy(self):
        plan=json.loads(subprocess.check_output(['python3',str(COST_PATH),'--user','xwz','--output','/tmp/not-created-memcached','--dry-run'],text=True))
        for phase,section in plan.items():
            self.assertNotIn('calibration',section)
            self.assertEqual(section['selection'],'fixed-concurrency-no-calibration')
            cmd=section['measurement']
            self.assertEqual(cmd[cmd.index('--connections')+1],'32')
            self.assertEqual(cmd[cmd.index('--cases')+1],'off,off-same-pages,static-low,static-medium,static-high,policy-low,policy-medium,policy-high')
            self.assertEqual(cmd[cmd.index('--thp')+1],'preserve')
            self.assertNotIn('--page-kb',cmd)
            self.assertEqual(cmd[cmd.index('--qps')+1],'0')
            self.assertEqual(cmd[cmd.index('--sampler-cpus')+1],cmd[cmd.index('--server-cpus')+1])
            self.assertEqual(cmd[cmd.index('--memory-mb')+1], '2048' if phase=='resident' else '640')

    def test_explicit_connections_and_invalid_input(self):
        base=['python3',str(COST_PATH),'--user','xwz','--output','/tmp/not-created-memcached','--dry-run']
        plan=json.loads(subprocess.check_output(base+['--connections','64','--phases','pressure'],text=True))
        self.assertEqual(list(plan),['pressure'])
        cmd=plan['pressure']['measurement']
        self.assertEqual(cmd[cmd.index('--connections')+1],'64')
        for extra in (['--connections','0'],['--connections','16','32','64'],['--pressure-selection','observed-peak']):
            self.assertNotEqual(subprocess.run(base+extra,capture_output=True).returncode,0)

    def test_runs_measurements_without_calibration(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'campaign'
            argv=[str(COST_PATH),'--user','xwz','--output',str(output),'--connections','32']
            with patch.object(cost.sys,'argv',argv), patch.object(cost.os,'geteuid',return_value=0), \
                 patch.object(cost.subprocess,'run') as run, patch.object(cost,'summarize') as summary:
                cost.main()
            self.assertEqual(run.call_count,2)
            self.assertEqual(summary.call_count,2)
            for call in run.call_args_list:
                command=call.args[0]
                self.assertTrue(command[command.index('--output')+1].endswith('/measurement'))
                self.assertEqual(command[command.index('--connections')+1],'32')
            self.assertEqual((output/'status').read_text().strip(),'completed')
            self.assertEqual(json.loads((output/'plan.json').read_text())['load_selection'],'fixed-concurrency-no-calibration')


class ThpCases(unittest.TestCase):
    def setUp(self):
        self.original = {THP_ROOT+'enabled':'madvise',THP_ROOT+'hugepages-64kB/enabled':'always',
                         THP_ROOT+'hugepages-2048kB/enabled':'inherit'}

    def test_compound_extent_for_head_tail_and_base(self):
        flags = {16:1<<15, **{i:1<<16 for i in range(17,32)}}
        get = lambda pfn: flags.get(pfn,0)
        self.assertEqual(thp.folio_extent(16,get),(16,16))
        self.assertEqual(thp.folio_extent(23,get),(16,16))
        self.assertEqual(thp.folio_extent(32,get),(32,1))
        self.assertIsNone(thp.folio_extent(2,lambda p: (1<<16) if p==2 else 0))

    def test_no_page_size_forcing_remains(self):
        source = THP_PATH.read_text()
        self.assertNotIn("write(path,value)",source)
        self.assertNotIn("base_page_baseline_valid",source)
        self.assertNotIn("--page-kb",source)

    def test_sampler_affinity_is_applied_and_verified(self):
        with patch.object(thp.Path,'glob',return_value=[Path('/proc/123')]), \
             patch.object(thp,'read',return_value='hermit_pebsd\n'), \
             patch.object(thp.os,'sched_setaffinity') as setter, \
             patch.object(thp.os,'sched_getaffinity',return_value={0,1}):
            self.assertEqual(thp.bind_sampler([0,1]),{'pid':123,'cpus':[0,1]})
            setter.assert_called_once_with(123,{0,1})
        with patch.object(thp.Path,'glob',return_value=[Path('/proc/123')]), \
             patch.object(thp,'read',return_value='hermit_pebsd\n'), \
             patch.object(thp.os,'sched_setaffinity'), \
             patch.object(thp.os,'sched_getaffinity',return_value={2}):
            with self.assertRaises(RuntimeError):thp.bind_sampler([0,1])

    def test_connection_limits_cover_largest_default_calibration(self):
        with patch.object(thp.resource,'getrlimit',side_effect=[(1024,1048576),(65536,1048576)]), \
             patch.object(thp.resource,'setrlimit') as setter:
            settings=thp.configure_connection_limits(16,256)
            setter.assert_called_once_with(thp.resource.RLIMIT_NOFILE,(65536,1048576))
            self.assertEqual(settings['estimated_client_connections'],4096)
            self.assertEqual(settings['server_max_connections'],32768)

    def test_cpu_lists(self):
        self.assertEqual(thp.parse_cpu_list('0-3,6,2'),[0,1,2,3,6])
        with self.assertRaises(ValueError): thp.parse_cpu_list('3-1')


    def test_final_stats_retries_and_preserves_diagnostics(self):
        import tempfile,json
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);server=Mock();server.poll.return_value=None
            with patch.object(thp,'stats',side_effect=[TimeoutError('slow'), 'STAT evictions 0\r\nEND\r\n']) as fetch:
                thp.collect_final_stats(11329,run,server)
            self.assertEqual(fetch.call_count,2)
            records=json.loads((run/'stats-collection.json').read_text())
            self.assertFalse(records[0]['success']);self.assertTrue(records[1]['success'])
            self.assertIn('STAT evictions 0',(run/'memcached.stats').read_text())

    def test_final_stats_exhaustion_is_not_validated_as_success(self):
        import tempfile,json
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);server=Mock();server.poll.return_value=None
            with patch.object(thp,'stats',side_effect=TimeoutError('slow')) as fetch:
                with self.assertRaisesRegex(RuntimeError,'measurement is incomplete'):
                    thp.collect_final_stats(11329,run,server)
            self.assertEqual(fetch.call_count,3)
            self.assertFalse((run/'memcached.stats').exists())
            self.assertEqual(len(json.loads((run/'stats-collection.json').read_text())),3)


class MatrixTests(unittest.TestCase):
    def test_archived_drivers_include_pebs_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'source'
            hashes = matrix.snapshot_sources(source)
            self.assertIn('sweep_common.sh', hashes)
            self.assertIn('pebs/enable_pebs_pid.py', hashes)
            self.assertIn('pebs/run_workload_matrix.py', hashes)
            self.assertIn('pebs/page_selection_metrics.py', hashes)
            for name, digest in hashes.items():
                self.assertEqual(hashlib.sha256((source/name).read_bytes()).hexdigest(), digest)
            for script in {entry[0] for entry in matrix.SUITES.values()}:
                archived = subprocess.check_output(['bash', str(source/script), '--help'], text=True)
                original = subprocess.check_output(['bash', str(RDMA/script), '--help'], text=True)
                self.assertEqual(archived, original)
            subprocess.run(['bash', '-c', 'source "$1"; PEBS_BENCH_MODE=policy configure_benchmark_pebs',
                            'test', str(source/'sweep_common.sh')], check=True, capture_output=True)
            archived_plan = subprocess.check_output(['python3', str(source/'pebs/run_workload_matrix.py'),
                                                     '--dry-run'], text=True)
            self.assertEqual(json.loads(archived_plan)['samples'], 1323)

    def test_default_plan_and_baseline_parameters(self):
        result = subprocess.check_output(['python3', str(RDMA/'pebs/run_workload_matrix.py'), '--dry-run'], text=True)
        data = json.loads(result)
        self.assertEqual(data['samples'], 1323)
        self.assertEqual(data['memory_policy'], 'fixed-max-v1')
        self.assertEqual(data['page_policy'], 'max-allocation-v1')
        blocks = {}
        for row in data['plan']:
            self.assertEqual(row['max_page_kb'], row['page_kb'])
            blocks.setdefault((row['suite'],row['page_kb'],row['repeat']), []).append(row)
        self.assertEqual(len(blocks),189)
        for rows in blocks.values():
            self.assertEqual(len(rows),7)
            self.assertEqual(sum(r['case']=='off' for r in rows),1)
            self.assertEqual({r['store_period'] for r in rows},{1500003})
            self.assertEqual({r['load_period'] for r in rows},{0,199,1999,19997})
        env=data['environment']
        for key,value in dict(WORKSET_MB='16384',LOCAL_RATIO_PCT='70',PAGE_SIZE_POLICY='max',ACCESS_REGION_KB='2048',
                              YCSB_RECORDCOUNT='8192',XGB_ROUNDS='30',REDIS_SCAN_CHUNK='65536').items():
            self.assertEqual(env[key],value)
        for key in ('STHD_CNT', 'RECLAIM_MODE', 'RECLAIM_HEADROOM_PAGES', 'BYPASS_SWAPCACHE', 'LAZY_POLL'):
            self.assertNotIn(key, env)
        again=json.loads(subprocess.check_output(['python3',str(RDMA/'pebs/run_workload_matrix.py'),'--dry-run'],text=True))
        self.assertEqual(data['plan'],again['plan'])

    def test_reject_partial_ycsb_and_non_target_order_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            summary=root/'ycsb-swapio-summary.csv'
            csv_header='page_kb,read_ops,read_ok,target_errors_delta,limit_mb,bench_pswpout_delta\n'
            summary.write_text(csv_header+'64,8192,8192,0,256,17\n')
            (root/'memory-policy.txt').write_text('fixed-max-v1\n')
            for phase in ('before-swapin', 'after-swapin'):
                (root/f'memory-max-{phase}.txt').write_text(str(256*1024**2)+'\n')
            header='order size_bytes stores loads fallback_4k errors\n'
            (root/'order-before.txt').write_text(header+'0 4096 0 0 0 0\n')
            (root/'order-after.txt').write_text(header+'0 4096 0 0 0 0\n')
            row={'suite':'ycsb','page_kb':64}
            self.assertEqual(matrix.validate_results(root,row),[])
            summary.write_text(csv_header+'64,8192,8191,0,256,17\n')
            self.assertTrue(any('read_ok' in s for s in matrix.validate_results(root,row)))
            (root/'order-after.txt').write_text(header+'0 4096 0 0 0 1\n')
            self.assertTrue(any('backend errors' in s for s in matrix.validate_results(root,row)))
            (root/'memory-events-after.txt').write_text('oom_kill 1\n')
            self.assertTrue(any('OOM' in s for s in matrix.validate_results(root,row)))

    def test_reject_missing_released_or_changed_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'swapio-summary.csv').write_text('page_kb,limit_mb\n64,256\n')
            row={'suite':'anon-1t-full','page_kb':64}
            self.assertTrue(matrix.validate_results(root,row))
            (root/'memory-policy.txt').write_text('fixed-max-v1\n')
            (root/'memory-max-before-swapin.txt').write_text(str(256*1024**2)+'\n')
            for value in ('max', str(512*1024**2)):
                (root/'memory-max-after-swapin.txt').write_text(value+'\n')
                self.assertTrue(any('memory.max mismatch' in s for s in matrix.validate_results(root,row)))
            (root/'memory-max-after-swapin.txt').write_text(str(256*1024**2)+'\n')
            self.assertEqual(matrix.validate_results(root,row),[])

    def test_budget_snapshot_checks_real_value_without_changing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cg=root/'cgroup'; cg.mkdir()
            out=root/'results'; out.mkdir()
            values={'memory.max':str(256*1024**2), 'memory.high':'max',
                    'memory.current':'123456', 'memory.swap.current':'7890'}
            for key,value in values.items(): (cg/key).write_text(value+'\n')
            script='source "$1"; sudo_cat() { cat "$1"; }; snapshot_fixed_memory_budget "$2" "$3" before-swapin "$4"'
            args=['bash','-c',script,'test',str(RDMA/'common.sh'),str(cg),str(out),values['memory.max']]
            subprocess.run(args,check=True,capture_output=True,text=True)
            self.assertEqual((out/'memory-policy.txt').read_text().strip(),'fixed-max-v1')
            for key,value in values.items():
                self.assertEqual((out/f'{key.replace(".","-")}-before-swapin.txt').read_text().strip(),value)
                self.assertEqual((cg/key).read_text().strip(),value)
            (cg/'memory.max').write_text('max\n')
            result=subprocess.run(args,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('memory.max changed',result.stderr)
            self.assertEqual((cg/'memory.max').read_text(),'max\n')

    def test_disabled_hooks_need_no_privilege(self):
        subprocess.run(['bash','-c', 'source "$1"; unset PEBS_BENCH_MODE; configure_benchmark_pebs; snapshot_benchmark_pebs /no/such/path', 'test',str(RDMA/'common.sh')],check=True)

    def test_subset_remains_paired(self):
        args=types.SimpleNamespace(periods=[1999],suites=['redis'],pages=[64],repeats=2,seed=7,store_period=1500003)
        rows=matrix.plan(args)
        self.assertEqual(len(rows),6)
        for repeat in (1,2):
            self.assertEqual({r['case'] for r in rows if r['repeat']==repeat},{'off','static-1999','policy-1999'})

    def test_reject_hidden_backend_candidate_restrictions(self):
        for ceiling, mask in [(4, 1), (64, 0x1d), (2048, 0x3fd), (64, 0x3fd)]:
            matrix.validate_backend_orders(ceiling, mask)
        for ceiling, mask in [(64, 1), (64, 0x11), (2048, 0x201)]:
            with self.assertRaisesRegex(ValueError, 'excludes sizes'):
                matrix.validate_backend_orders(ceiling, mask)

    def test_policy_changes_ignore_only_sampling_controls(self):
        baseline = {'controls': {'remote_order_mask':'0x3fd', 'pebs_force_order':'0',
                                'pebs_mode':'0', 'pebs_load_period':'1999', 'pebs_min_samples':'32'},
                    'thp': {'enabled':'[madvise]'}}
        current = json.loads(json.dumps(baseline))
        current['controls'].update(pebs_mode='1', pebs_load_period='199', pebs_order_stats='sampled: 5')
        self.assertEqual(matrix.policy_changes(baseline, current), [])
        for key in ('remote_order_mask', 'pebs_force_order', 'pebs_min_samples'):
            changed = json.loads(json.dumps(current))
            changed['controls'][key] = '7'
            self.assertEqual(len(matrix.policy_changes(baseline, changed)), 1)
        current['thp']['enabled'] = '[never]'
        self.assertTrue(matrix.policy_changes(baseline, current))

    def test_measurement_pairs_performance_with_all_orders_and_actual_wrs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'redis-swapio-summary.csv').write_text('page_kb,get_qps,bench_sec\n64,1234,2\n')
            for suffix in ('', '-swapin'):
                for phase, extra in [('before', 0), ('after', 1)]:
                    p = root/f'order-{phase}{suffix}.txt'
                    p.write_text('order size_bytes stores loads fallback_4k errors\n'
                                 f'0 4096 10 10 0 0\n4 65536 {10+extra} {10+2*extra} {extra} 0\n')
                    Path(str(p)+'.wr').write_text('order write_wrs read_wrs write_bytes read_bytes\n'
                        f'0 0 {10+16*extra} 0 {40960+65536*extra}\n'
                        f'4 {10+extra} {10+extra} {655360+65536*extra} {655360+65536*extra}\n')
                    Path(str(p)+'.pebs').write_text('order: decisions\n 0: 10\n'
                        f' 4: {10+3*extra}\nactual_order_0: 10\nactual_order_4: {10+2*extra}\n')
                    Path(str(p)+'.thp').write_text('/thp/enabled=never\n/thp/hugepages-64kB/enabled=always\n'
                                                  '/thp/hugepages-2048kB/enabled=never\n')
            row = {'suite':'redis', 'page_kb':64}
            result = matrix.collect_measurement(root, row)
            self.assertEqual(result['max_page_kb'], 64)
            self.assertEqual(result['performance'], dict(metric='get_qps', unit='GET/s', throughput=1234, duration_sec=2))
            small, large = [r for r in result['granularity'] if r['phase'] == 'application']
            self.assertEqual((small['folio_loads'], small['read_wrs'], small['read_bytes']), (0, 16, 65536))
            self.assertEqual((large['folio_loads'], large['read_wrs'], large['read_bytes']), (2, 1, 65536))
            self.assertEqual((large['folio_fallback_4k'], large['pebs_decisions'], large['pebs_actual_swapins']), (1, 3, 2))
            self.assertEqual(large['read_byte_share'], 0.5)
            # Older kernels lack actual_order_N: missing data must not become zero.
            for p in root.glob('*.pebs'):
                p.write_text('\n'.join(s for s in p.read_text().splitlines() if not s.startswith('actual_'))+'\n')
            result = matrix.collect_measurement(root, row)
            self.assertTrue(all(r['pebs_actual_swapins'] is None for r in result['granularity']))
            wr = root/'order-after-swapin.txt.wr'
            original = wr.read_text()
            wr.write_text(original.replace('0 0 26', '0 0 9'))
            with self.assertRaisesRegex(ValueError, 'counter reset'):
                matrix.collect_measurement(root, row)
            wr.write_text(original)
            thp = root/'order-after-swapin.txt.thp'
            thp.write_text(thp.read_text().replace('2048kB/enabled=never', '2048kB/enabled=always'))
            with self.assertRaisesRegex(ValueError, 'THP settings'):
                matrix.collect_measurement(root, row)
            wr.unlink()
            with self.assertRaises(FileNotFoundError):
                matrix.collect_measurement(root, row)


class ScopeTests(unittest.TestCase):
    def test_kconfig_does_not_require_memcg(self):
        text = (KERNEL / 'mm/Kconfig').read_text()
        block = text.split('config HERMIT\n', 1)[1]
        block = block.split('\nconfig ', 1)[0].split('\nsource ', 1)[0]
        depends = [line for line in block.splitlines()
                   if line.strip().startswith('depends on')]
        self.assertTrue(depends)
        self.assertNotIn('MEMCG', '\n'.join(depends))

    def test_pebs_owned_by_mm(self):
        text = (KERNEL / 'mm/hermit_pebs.c').read_text()
        self.assertIn('hermit_pebs_mm_exit', text)
        self.assertIn('hermit_pebs_get_mm', text)
        self.assertIn('hermit_swapin_order(struct mm_struct *mm', text)
        self.assertNotIn('#include <linux/memcontrol.h>', text)
        self.assertNotIn('get_mem_cgroup_from_mm', text)
        self.assertNotIn('hermit_pebs_memcg', text)

    def test_policy_call_sites_use_mm(self):
        page_io = (KERNEL / 'mm/page_io.c').read_text()
        self.assertNotIn('get_mem_cgroup_from_folio', page_io)
        memory = (KERNEL / 'mm/memory.c').read_text()
        block = memory.split('PEBS policy:', 1)[1].split('#endif', 1)[0]
        self.assertIn('hermit_swapin_order(', block)
        self.assertIn('vma->vm_mm', block)
        self.assertNotIn('get_mem_cgroup_from_mm', block)

    def test_pid_enable_helper_uses_syscall(self):
        helper = TOOLS / 'pebs' / 'enable_pebs_pid.py'
        self.assertTrue(helper.exists())
        text = helper.read_text()
        self.assertIn('473', text)
        self.assertIn('hermit_pebs_start', text)

    def test_mm_lifetime_hooks(self):
        fork = (KERNEL / 'kernel/fork.c').read_text()
        self.assertIn('hermit_pebs_mm_exit(mm)', fork)
        self.assertIn('mm->hermit_pebs = NULL', fork)
        types = (KERNEL / 'include/linux/mm_types.h').read_text()
        self.assertIn('struct hermit_pebs_state *hermit_pebs', types)

    def test_active_tools_do_not_write_memcg_file(self):
        for path in TOOLS.rglob('*'):
            if not path.is_file() or path.suffix not in {'.py', '.sh', '.c'}:
                continue
            if 'results' in path.parts or 'tests' in path.parts:
                continue
            self.assertNotIn('memory.hermit_pebs',
                             path.read_text(errors='ignore'), str(path))


class MicrobenchTests(unittest.TestCase):
    def test_worker_density_switch_and_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp)/'workset'
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-std=c11',
                            str(PEBS/'page_selection_workset.c'), '-o', str(binary)], check=True)
            p = subprocess.Popen([str(binary), '4', '17', 'base'], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                log = io.StringIO()
                runner.response(p, 'boot', log, 10)
                ready = runner.command(p, 'init', 'ready', log, 10)
                self.assertEqual(ready['address'] % (2 * 1024**2), 0)
                self.assertEqual(ready['bytes'], 4 * 1024**2)
                for density, mixed, selected in [(1, 0, 2), (512, 0, 1024), (1, 0, 2),
                                                  (1, 1, 513), (16, 0, 32), (64, 0, 128),
                                                  (256, 0, 512)]:
                    r = runner.command(p, f'access {density} 3 {mixed}', 'access', log, 10)
                    self.assertEqual(r['selected_pages'], selected)
                    self.assertEqual(r['page_coverage_bytes'], selected * 4096)
                    self.assertEqual(r['read_instructions'], selected * 64 * 3)
                    self.assertEqual(r['checksum_errors'], 0)
                    self.assertGreater(r['seconds'], 0)
                # Exercise byte-version wrap without falsifying checksums.
                r = runner.command(p, 'access 1 260 0', 'access', log, 10)
                self.assertEqual(r['checksum_errors'], 0)
                p.stdin.write(b'quit\n'); p.stdin.flush()
                self.assertEqual(p.wait(timeout=10), 0)
            finally:
                if p.poll() is None:
                    p.kill(); p.wait()
                p.stdin.close(); p.stdout.close(); p.stderr.close()

    def test_compound_folios(self):
        flags = {100: 1 << 15, 101: 1 << 16, 102: 1 << 16, 103: 1 << 16}
        get = lambda pfn: flags.get(pfn, 0)
        self.assertEqual(metrics.folio_extent(102, get), (100, 4))
        self.assertEqual(metrics.folio_extent(99, get), (99, 1))
        flags.pop(103)
        self.assertIsNone(metrics.folio_extent(100, get))

    def test_census_only_target_mapping_and_unknowns(self):
        entries = [0, (1 << 63) | 777, (1 << 63) | 102, 1 << 62, 0, 1 << 63]
        pagemap = b''.join(struct.pack('=Q', v) for v in entries)
        flags = bytearray(4096 * 8)
        for pfn in range(100, 104):
            struct.pack_into('=Q', flags, pfn * 8, 1 << (15 if pfn == 100 else 16))
        def fake_open(path, *_args, **_kwargs):
            return io.BytesIO(pagemap if str(path).endswith('pagemap') else flags)
        def fake_read(path):
            return '00001000-00006000 rw-p 0 00:00 0\n' if str(path).endswith('maps') else ''
        with tempfile.TemporaryDirectory() as tmp, patch.object(metrics, 'read', fake_read), \
             patch.object(metrics, 'thp_settings', return_value={}), patch('builtins.open', fake_open):
            result = metrics.record_page_sizes(Path(tmp)/'pages', 123, 0x2000, 0x4000)
        self.assertEqual(result['folio_size_kib'], {'16': {'mapped_bytes': 4096, 'observed_folios': 1}})
        self.assertEqual(result['resident_mapped_bytes'], 8192)
        self.assertEqual(result['unknown_resident_bytes'], 4096)
        self.assertEqual(result['swapped_mapped_bytes'], 4096)
        self.assertEqual(result['not_present_mapped_bytes'], 4096)

    def test_plan_is_paired_and_contains_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'not-created'
            result = subprocess.run([sys.executable, str(PEBS/'run_page_selection_microbench.py'),
                                     '--user', 'unused', '--output', str(output), '--dry-run'],
                                    check=True, text=True, capture_output=True)
            plan = json.loads(result.stdout)
            self.assertFalse(output.exists())
            self.assertEqual(len(plan['runs']), 147)
            self.assertEqual(plan['measurement_epochs'], 945)
            for repeat in range(1, 4):
                runs = [r for r in plan['runs'] if r['repeat'] == repeat]
                self.assertEqual(len({r['seed'] for r in runs}), 1)
                switch = [r for r in runs if r['scenario'] == 'switch']
                self.assertEqual(len(switch), 7)
                self.assertTrue(all(r['phases'] == [1, 512, 1] for r in switch))

    def counters(self, path, count):
        path.mkdir()
        (path/'wr_stats').write_text('order write_wrs read_wrs write_bytes read_bytes\n'
                                    f'0 {count} {count} {4096 * count} {4096 * count}\n')
        (path/'order_stats').write_text('order size_bytes stores loads fallback_4k errors\n'
                                      f'0 4096 {count} {count} 0 0\n')
        (path/'pebs_order_stats').write_text('order: decisions\n'
                                           f'0: {count}\nsampled: {count}\nlost: 0\nthrottled: 0\nunknown: 0\n')
        (path/'perf.json').write_text('{}')
        (path/'time.json').write_text(json.dumps({'end': count}))

    def test_actual_wr_delta_and_counter_reset_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp)/'a', Path(tmp)/'b'
            self.counters(a, 10); self.counters(b, 20)
            delta = metrics.deltas(a, b)
            self.assertEqual(delta['wr']['0']['read_bytes'], 40960)
            self.assertEqual(delta['pebs']['sampled'], 10)
            self.assertNotIn('size_bytes', delta['order']['0'])
            with self.assertRaisesRegex(ValueError, 'Counter reset'):
                metrics.deltas(b, a)
            (b/'pebs_order_stats').write_text('sampled: 20\n')
            with self.assertRaisesRegex(ValueError, 'Missing PEBS'):
                metrics.deltas(a, b)

if __name__ == '__main__':
    unittest.main()
