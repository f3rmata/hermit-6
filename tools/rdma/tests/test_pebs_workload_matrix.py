#!/usr/bin/env python3
"""Offline checks: baseline pairing, invalid measurements and inert hooks."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest

RDMA = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('matrix', RDMA/'pebs/run_workload_matrix.py')
assert SPEC is not None and SPEC.loader is not None
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class MatrixTests(unittest.TestCase):
    def test_default_plan_and_baseline_parameters(self):
        result = subprocess.check_output(['python3', str(RDMA/'pebs/run_workload_matrix.py'), '--dry-run'], text=True)
        data = json.loads(result)
        self.assertEqual(data['samples'], 1323)
        self.assertEqual(data['memory_policy'], 'fixed-max-v1')
        blocks = {}
        for row in data['plan']:
            blocks.setdefault((row['suite'],row['page_kb'],row['repeat']), []).append(row)
        self.assertEqual(len(blocks),189)
        for rows in blocks.values():
            self.assertEqual(len(rows),7)
            self.assertEqual(sum(r['case']=='off' for r in rows),1)
            self.assertEqual({r['store_period'] for r in rows},{1500003})
            self.assertEqual({r['load_period'] for r in rows},{0,199,1999,19997})
        env=data['environment']
        for key,value in dict(WORKSET_MB='16384',LOCAL_RATIO_PCT='70',STHD_CNT='16',
                              YCSB_RECORDCOUNT='8192',XGB_ROUNDS='30',REDIS_SCAN_CHUNK='65536').items():
            self.assertEqual(env[key],value)
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
            self.assertEqual(M.validate_results(root,row),[])
            summary.write_text(csv_header+'64,8192,8191,0,256,17\n')
            self.assertTrue(any('read_ok' in s for s in M.validate_results(root,row)))
            (root/'order-after.txt').write_text(header+'0 4096 0 0 0 1\n')
            self.assertTrue(any('backend errors' in s for s in M.validate_results(root,row)))
            (root/'memory-events-after.txt').write_text('oom_kill 1\n')
            self.assertTrue(any('OOM' in s for s in M.validate_results(root,row)))

    def test_reject_missing_released_or_changed_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'swapio-summary.csv').write_text('page_kb,limit_mb\n64,256\n')
            row={'suite':'anon-1t-full','page_kb':64}
            self.assertTrue(M.validate_results(root,row))
            (root/'memory-policy.txt').write_text('fixed-max-v1\n')
            (root/'memory-max-before-swapin.txt').write_text(str(256*1024**2)+'\n')
            for value in ('max', str(512*1024**2)):
                (root/'memory-max-after-swapin.txt').write_text(value+'\n')
                self.assertTrue(any('memory.max mismatch' in s for s in M.validate_results(root,row)))
            (root/'memory-max-after-swapin.txt').write_text(str(256*1024**2)+'\n')
            self.assertEqual(M.validate_results(root,row),[])

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
        rows=M.plan(args)
        self.assertEqual(len(rows),6)
        for repeat in (1,2):
            self.assertEqual({r['case'] for r in rows if r['repeat']==repeat},{'off','static-1999','policy-1999'})


if __name__ == '__main__': unittest.main()
