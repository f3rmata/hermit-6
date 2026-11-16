"""Offline fixtures for the two-table WR ABI and the base-page matrix."""
# The drivers are imported from their own directory, not from an installed
# package, so static resolvers cannot see them.
# pyright: reportMissingImports=false
from pathlib import Path
import csv
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pebs'))
import matrix_metrics as metrics
import pebs_perf_matrix as memcached
import run_workload_matrix as workloads


def wr(order_reads=0, batch_reads=0, three_reads=0):
    return ('order write_wrs read_wrs write_bytes read_bytes\n'
            f'0 0 {order_reads} 0 {order_reads * 4096}\n'
            '2 0 0 0 0\n'
            'pages batch_write_wrs batch_read_wrs batch_write_bytes batch_read_bytes\n'
            f'1 0 {batch_reads} 0 {batch_reads * 4096}\n'
            f'3 0 {three_reads} 0 {three_reads * 12288}\n')


def policy(**values):
    return '\n'.join(f'{k} {values.get(k, 0)}' for k in metrics.POLICY_FIELDS) + '\n'


class MatrixMetricsTests(unittest.TestCase):
    def test_order_and_batch_namespaces_are_additive(self):
        result = metrics.wr_delta(wr(10, 3, 2), wr(12, 8, 6))
        self.assertEqual(result[4]['read_wrs'], 7)
        self.assertEqual(result[4]['batch_read_wrs'], 5)
        self.assertEqual(result[12]['read_wrs'], 4)
        self.assertEqual(sum(v['read_bytes'] for v in result.values()), 7 * 4096 + 4 * 12288)
        old = 'order write_wrs read_wrs write_bytes read_bytes\n0 0 1 0 4096\n'
        self.assertEqual(metrics.wr_delta(old, old)[4]['read_wrs'], 0)

    def test_reset_cannot_hide_behind_another_table(self):
        with self.assertRaisesRegex(ValueError, 'reset'):
            metrics.wr_delta(wr(10, 0), wr(9, 100))
        with self.assertRaisesRegex(ValueError, 'schema'):
            metrics.wr_delta(wr(), wr().split('pages')[0])
        with self.assertRaisesRegex(ValueError, 'missing'):
            metrics.policy_delta(policy(), None)

    def test_single_page_fallback_is_policy_activity(self):
        counters = metrics.policy_delta(policy(), policy())
        wrs = metrics.wr_delta(wr(), wr(batch_reads=1))
        activity, issues = metrics.policy_activity(1, 4, 0, counters, wrs)
        self.assertTrue(activity['required'])
        self.assertTrue(activity['path_exercised'])
        self.assertEqual(issues, [])
        activity, issues = metrics.policy_activity(1, 2048, 0, counters, metrics.wr_delta(wr(), wr(1)))
        self.assertFalse(activity['path_exercised'])
        self.assertTrue(issues)
        activity, issues = metrics.policy_activity(1, 2048, 0, counters, metrics.wr_delta(wr(), wr()))
        self.assertFalse(activity['required'])
        self.assertEqual(issues, [])

    def test_mglru_selection_is_policy_activity_without_pte_observation(self):
        counters = metrics.policy_delta(policy() + 'mglru_candidates 0\n',
                                        policy() + 'mglru_candidates 3\n')
        wrs = metrics.wr_delta(wr(), wr(1))
        activity, issues = metrics.policy_activity(1, 4, 0, counters, wrs)
        self.assertTrue(activity['path_exercised'])
        self.assertEqual(issues, [])
        activity, _ = metrics.policy_activity(None, 4, 0, counters, wrs)
        self.assertFalse(activity['path_exercised'])

    def test_application_measurement_keeps_non_power_of_two_wrs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'redis-summary.csv').write_text('page_kb,get_qps,bench_sec\n64,100,2\n')
            for phase in ('', '-swapin'):
                for tag, n in [('before', 0), ('after', 1)]:
                    path = root/f'order-{tag}{phase}.txt'
                    path.write_text(f'order size_bytes stores loads fallback_4k errors\n0 4096 0 {n} 0 0\n')
                    Path(str(path)+'.wr').write_text(wr(batch_reads=n, three_reads=n))
                    Path(str(path)+'.pebs').write_text('0: 0\nactual_order_0: 0\n')
                    Path(str(path)+'.policy').write_text(policy())
                    Path(str(path)+'.vmstat').write_text(f'pswpin {4*n}\npswpout {n}\n')
                    Path(str(path)+'.thp').write_text('/thp/enabled=never\n/thp/hugepages-64kB/enabled=always\n')
            row = dict(suite='redis', page_kb=64, mode=1)
            result = workloads.collect_measurement(root, row)
            batch = next(r for r in result['granularity'] if r['phase']=='application' and r['size_kib']==12)
            self.assertIsNone(batch['order'])
            self.assertEqual(batch['read_bytes'], 12288)
            self.assertEqual(result['swap']['application'], dict(pswpin=4, pswpout=1))
            activity, issues = workloads.check_policy_activity(row, result)
            self.assertTrue(activity['path_exercised'])
            self.assertEqual(issues, [])

    def test_memcached_plan_pairs_off_for_each_ceiling(self):
        plan = memcached.matrix_plan(metrics.PAGE_SIZES, 3, memcached.matrix_cases(''))
        self.assertEqual(len(plan), 189)
        for page in metrics.PAGE_SIZES:
            for repeat in range(1, 4):
                rows = [r for r in plan if r['max_page_kb']==page and r['repeat']==repeat]
                self.assertEqual(len(rows), 7)
                self.assertEqual(sum(r['case']=='off' and r['mode'] is None for r in rows), 1)
        saved = {'/thp/enabled': 'madvise', **{f'/thp/hugepages-{k}kB/enabled':'never' for k in metrics.PAGE_SIZES[1:]}}
        settings = metrics.page_ceiling_settings(64, saved)
        self.assertEqual({p for p,v in settings.items() if v=='always'},
                         {f'/thp/hugepages-{k}kB/enabled' for k in (16,32,64)})
        self.assertEqual(set(metrics.page_ceiling_settings(4, saved).values()), {'never'})
        sparse = memcached.matrix_plan([64], 1, memcached.matrix_cases(''))
        self.assertEqual(len(sparse), 8)
        self.assertEqual([r['case'] for r in sparse if r['max_page_kb']==4], ['off'])
        self.assertEqual(sparse[0]['baseline_role'], '4KiB-only')

    def test_four_kib_comparison_matches_repeat_and_preserves_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [dict(suite='redis', repeat=r, max_page_kb=p, case=c, usable=True, throughput=t)
                    for r,p,c,t in [(1,4,'off',100), (1,64,'off',150), (1,64,'policy-1999',200),
                                    (2,64,'policy-1999',500)]]
            with (root/'summary.csv').open('w') as f:
                w=csv.DictWriter(f,fieldnames=rows[0]);w.writeheader();w.writerows(rows)
            metrics.write_comparisons(root)
            with (root/'paired.csv').open() as f:
                paired=list(csv.DictReader(f))
            row=next(r for r in paired if r['repeat']=='1' and r['case']=='policy-1999' and r['baseline']=='4KiB-only')
            self.assertEqual(float(row['throughput_gain_pct']), 100)
            row=next(r for r in paired if r['repeat']=='2' and r['baseline']=='4KiB-only')
            self.assertEqual(row['baseline_status'], 'missing')
            self.assertEqual(row['valid_pair'], 'False')


class PolicyPresetTests(unittest.TestCase):
    def test_preset_names_map_to_the_documented_masks(self):
        self.assertEqual(metrics.policy_masks(['default']), [('default', 0x1bf)])
        self.assertEqual(metrics.policy_masks(['no-pebs']), [('no-pebs', 0x1bd)])
        self.assertEqual(metrics.policy_masks(['no-batch']), [('no-batch', 0x19f)])
        # Phase B adds a bit instead of clearing one, so it is a separate
        # preset: 0x1bf | MEDIUM. Its label cannot collide with a period name.
        self.assertEqual(metrics.policy_masks(['medium-on']), [('medium-on', 0x3bf)])
        self.assertEqual(0x3bf, metrics.DEFAULT_POLICY_MASK | 0x200)
        # Ablations only clear bits of the default mask.
        for _, mask in metrics.policy_masks(['default', 'no-pebs', 'no-batch']):
            self.assertEqual(mask & ~metrics.DEFAULT_POLICY_MASK, 0)
        self.assertEqual(0x1bf & ~0x2, 0x1bd)
        self.assertEqual(0x1bf & ~0x20, 0x19f)

    def test_raw_and_comma_separated_masks_are_accepted(self):
        self.assertEqual(metrics.policy_masks(['0x1bd']), [('f1bd', 0x1bd)])
        self.assertEqual(metrics.policy_masks(['445']), [('f1bd', 0x1bd)])
        self.assertEqual(metrics.policy_masks(['default,no-pebs']),
                         [('default', 0x1bf), ('no-pebs', 0x1bd)])
        self.assertEqual(metrics.policy_masks(['default', 'default']), [('default', 0x1bf)])
        # Bits 0..9 exist: 0x200 is MEDIUM alone and 0x3bf is default|MEDIUM.
        self.assertEqual(metrics.policy_masks(['0x3bf']), [('f3bf', 0x3bf)])
        self.assertEqual(metrics.policy_masks(['512']), [('f200', 0x200)])
        for bad in (['bogus'], ['0x400'], ['1024'], ['-1'], []):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                metrics.policy_masks(bad)

    def test_default_suffix_keeps_historical_case_names(self):
        self.assertEqual(metrics.policy_suffix('default'), '')
        self.assertEqual(metrics.policy_suffix(None), '')
        self.assertEqual(metrics.policy_suffix('no-pebs'), '-no-pebs')

    def test_each_preset_is_its_own_labelled_cell_sharing_off_and_static(self):
        presets = metrics.policy_masks(['default', 'no-pebs', 'no-batch'])
        cases = memcached.matrix_cases('', presets=presets)
        plan = memcached.matrix_plan([64], 1, cases)
        labels = {row['case']: row['policy_features'] for row in plan}
        self.assertEqual(labels['policy-medium'], 0x1bf)
        self.assertEqual(labels['policy-medium-no-pebs'], 0x1bd)
        self.assertEqual(labels['policy-medium-no-batch'], 0x19f)
        self.assertIsNone(labels['off'])
        self.assertIsNone(labels['static-medium'])
        # One shared off/static set per block, plus the separate 4 KiB-only off.
        self.assertEqual(sum(1 for r in plan if r['case'] == 'off'), 2)
        self.assertEqual(sum(1 for r in plan if r['case'].startswith('static-')), 3)
        self.assertEqual(sum(1 for r in plan if r['case'].startswith('policy-')), 9)
        self.assertEqual({r['max_page_kb'] for r in plan if r['case'] == 'off'}, {4, 64})

    def test_workload_matrix_labels_policy_cells_per_preset(self):
        base = dict(periods=[1999], pages=[64], repeats=1, seed=1,
                    suites=['redis'], store_period=1)
        args = SimpleNamespace(**base, policy_features=['default', 'no-pebs', 'no-batch'])
        rows = workloads.plan(args)
        labels = {row['case']: row['policy_features'] for row in rows}
        self.assertEqual(labels['policy-1999'], 0x1bf)
        self.assertEqual(labels['policy-1999-no-pebs'], 0x1bd)
        self.assertEqual(labels['policy-1999-no-batch'], 0x19f)
        self.assertEqual(labels['off'], None)
        self.assertEqual(labels['static-1999'], None)
        # Historical behaviour: without --policy-features no mask is written.
        rows = workloads.plan(SimpleNamespace(**base, policy_features=None))
        self.assertEqual({r['case'] for r in rows}, {'off', 'static-1999', 'policy-1999'})
        self.assertTrue(all(r['policy_features'] is None for r in rows))


if __name__ == '__main__':
    unittest.main()
