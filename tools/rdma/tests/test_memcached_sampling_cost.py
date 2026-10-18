#!/usr/bin/env python3
import importlib.util
import json
import csv
import tempfile
from pathlib import Path
import subprocess
import unittest

PATH=Path(__file__).resolve().parents[1]/'pebs/run_memcached_sampling_cost.py'
spec=importlib.util.spec_from_file_location('cost',PATH)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


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
            summary,flags=module.summarize(folder)
            row=next(r for r in summary if r['case']=='static-high')
            self.assertAlmostEqual(row['paired_qps_loss_pct_median'],15)
            self.assertEqual(row['lost_cases'],2)
            self.assertTrue(flags)
            policy=next(r for r in summary if r['case']=='policy-high')
            self.assertAlmostEqual(policy['qps_gain_vs_static_pct_median'],20)
            self.assertAlmostEqual(row['qps_gain_vs_off_pct_median'],70)

    def test_dry_run_includes_both_baselines_static_and_policy(self):
        plan=json.loads(subprocess.check_output(['python3',str(PATH),'--user','xwz','--output','/tmp/not-created-memcached','--dry-run'],text=True))
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
        base=['python3',str(PATH),'--user','xwz','--output','/tmp/not-created-memcached','--dry-run']
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
            argv=[str(PATH),'--user','xwz','--output',str(output),'--connections','32']
            with patch.object(module.sys,'argv',argv), patch.object(module.os,'geteuid',return_value=0), \
                 patch.object(module.subprocess,'run') as run, patch.object(module,'summarize') as summary:
                module.main()
            self.assertEqual(run.call_count,2)
            self.assertEqual(summary.call_count,2)
            for call in run.call_args_list:
                command=call.args[0]
                self.assertTrue(command[command.index('--output')+1].endswith('/measurement'))
                self.assertEqual(command[command.index('--connections')+1],'32')
            self.assertEqual((output/'status').read_text().strip(),'completed')
            self.assertEqual(json.loads((output/'plan.json').read_text())['load_selection'],'fixed-concurrency-no-calibration')


if __name__=='__main__':unittest.main()
