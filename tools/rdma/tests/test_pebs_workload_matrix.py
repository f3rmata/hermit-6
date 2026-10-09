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
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class MatrixTests(unittest.TestCase):
    def test_default_plan_and_baseline_parameters(self):
        result = subprocess.check_output(['python3', str(RDMA/'pebs/run_workload_matrix.py'), '--dry-run'], text=True)
        data = json.loads(result)
        self.assertEqual(data['samples'], 1323)
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
            summary.write_text('page_kb,read_ops,read_ok,target_errors_delta\n64,8192,8192,0\n')
            header='order size_bytes stores loads fallback_4k errors\n'
            (root/'order-before.txt').write_text(header+'0 4096 0 0 0 0\n')
            (root/'order-after.txt').write_text(header+'0 4096 0 0 0 0\n')
            row={'suite':'ycsb','page_kb':64}
            self.assertEqual(M.validate_results(root,row),[])
            summary.write_text('page_kb,read_ops,read_ok,target_errors_delta\n64,8192,8191,0\n')
            self.assertTrue(any('read_ok' in s for s in M.validate_results(root,row)))
            (root/'order-after.txt').write_text(header+'0 4096 0 0 0 1\n')
            self.assertTrue(any('backend errors' in s for s in M.validate_results(root,row)))
            (root/'memory-events-after.txt').write_text('oom_kill 1\n')
            self.assertTrue(any('OOM' in s for s in M.validate_results(root,row)))

    def test_disabled_hooks_need_no_privilege(self):
        subprocess.run(['bash','-c', 'source "$1"; unset PEBS_BENCH_MODE; configure_benchmark_pebs; snapshot_benchmark_pebs /no/such/path', 'test',str(RDMA/'common.sh')],check=True)

    def test_subset_remains_paired(self):
        args=types.SimpleNamespace(periods=[1999],suites=['redis'],pages=[64],repeats=2,seed=7,store_period=1500003)
        rows=M.plan(args)
        self.assertEqual(len(rows),6)
        for repeat in (1,2):
            self.assertEqual({r['case'] for r in rows if r['repeat']==repeat},{'off','static-1999','policy-1999'})


if __name__ == '__main__': unittest.main()
