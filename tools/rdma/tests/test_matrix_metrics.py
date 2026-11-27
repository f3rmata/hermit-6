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
from unittest.mock import patch

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
    def test_adc_client_window_and_restore(self):
        header = 'stage count total_ns tsc_count total_tsc_ticks\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            control, stats = root/'adc_enable', root/'adc_stats'
            control.write_text('0\n')
            # The general before/after captures simulate extra system activity.
            def capture(folder, cg, pid):
                self.assertEqual(control.read_text().strip(), '1')
                ns = 2**55 if folder.name == 'before' else 2**56
                stats.write_text(header + f'swap_major 6 {ns} 0 0\nslotrun 0 0 0 0\n')
            with patch.object(memcached, 'D', root), patch.object(memcached, 'capture', capture):
                with memcached.measurement_window(root, None, 0, True):
                    stats.write_text(header + f'swap_major 8 {2**55+25} 0 0\nslotrun 0 0 0 0\n')
                self.assertEqual(control.read_text().strip(), '0')
                self.assertEqual((root/'adc.csv').read_text().splitlines(), [
                    'stage,count,ns,avg_ns', 'swap_major,2,25,12.50', 'slotrun,0,0,'])
                for saved in ('0', '1'):
                    control.write_text(saved)
                    with self.assertRaises(InterruptedError):
                        with memcached.measurement_window(root, None, 0, True):
                            raise InterruptedError
                    self.assertEqual(control.read_text().strip(), saved)
            # Older kernels can run uninstrumented cells with no ADC interface.
            with metrics.cell_adc(False, root/'absent', Path.read_text, Path.write_text):
                pass

    def test_adc_rejects_resets_and_missing_stages(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            before, after = root/'before', root/'after'
            header = 'stage count total_ns\n'
            before.write_text(header + 'swap_major 7 100\n')
            for row in ('swap_major 8 99', 'swap_minor 7 100', 'swap_major x 101'):
                with self.subTest(row=row), self.assertRaises(ValueError):
                    after.write_text(header + row + '\n')
                    metrics.write_adc_csv(root/'adc.csv', before, after)
            self.assertFalse((root/'adc.csv').exists())

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
        with self.assertRaisesRegex(ValueError, 'missing'):
            metrics.policy_counters('predicted_4k 0\nrefault_4k 0\n')

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
        # Single-page direct bypass skips batch_read_wrs; predict_calls or
        # order-0 actual swapins at a >4 KiB ceiling prove the policy ran.
        activity, issues = metrics.policy_activity(1, 16, 0, counters, metrics.wr_delta(wr(), wr(1)),
                                                   predict_calls=1)
        self.assertTrue(activity['path_exercised'])
        self.assertEqual(issues, [])
        direct_wr = metrics.wr_delta(wr(), wr(1))
        direct_wr[4]['pebs_actual_swapins'] = 1
        activity, issues = metrics.policy_activity(1, 16, 0, counters, direct_wr)
        self.assertTrue(activity['path_exercised'])
        self.assertEqual(issues, [])
        activity, issues = metrics.policy_activity(1, 2048, 0, counters, metrics.wr_delta(wr(), wr()))
        self.assertFalse(activity['required'])
        self.assertEqual(issues, [])

    def test_mglru_selection_is_policy_activity_without_pte_observation(self):
        counters = metrics.policy_delta(policy(), policy(mglru_candidates=3))
        wrs = metrics.wr_delta(wr(), wr(1))
        activity, issues = metrics.policy_activity(1, 4, 0, counters, wrs)
        self.assertTrue(activity['path_exercised'])
        self.assertEqual(issues, [])
        activity, _ = metrics.policy_activity(None, 4, 0, counters, wrs)
        self.assertFalse(activity['path_exercised'])

    def test_historical_policy_counters_remain_parseable(self):
        classic = ('observed_4k 2\ncold_observations 0\ninvalid_epochs 0\n'
                   'late_samples 0\npredicted_4k 1\nrefault_4k 0\nranked_candidates 0\n')
        delta = metrics.policy_delta(classic, classic.replace('observed_4k 2', 'observed_4k 3'))
        self.assertEqual(delta['observed_4k'], 1)
        activity, _ = metrics.policy_activity(1, 4, 0, delta, metrics.wr_delta(wr(), wr(1)))
        self.assertTrue(activity['path_exercised'])

    def test_mglru_check_requires_core_bit(self):
        for value in ('0x0001\n', '0x0007', '0x0003'):
            self.assertTrue(metrics.mglru_core_enabled(value))
        for value in (None, '', '0x0000', '0x0006'):
            self.assertFalse(metrics.mglru_core_enabled(value))

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
                    count = n if phase == '-swapin' else 100 * n
                    Path(str(path)+'.adc').write_text('stage count total_ns\n'
                                                     f'swap_major {count} {9*count}\n')
                    Path(str(path)+'.thp').write_text('/thp/enabled=never\n/thp/hugepages-64kB/enabled=always\n')
            row = dict(suite='redis', page_kb=64, mode=1)
            result = workloads.collect_measurement(root, row, root/'adc.csv')
            self.assertEqual((root/'adc.csv').read_text().splitlines(),
                             ['stage,count,ns,avg_ns', 'swap_major,1,9,9.00'])
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
        self.assertEqual(metrics.policy_masks(['default']), [('default', 0xa3)])
        self.assertEqual(metrics.policy_masks(['no-pebs']), [('no-pebs', 0xa1)])
        self.assertEqual(metrics.policy_masks(['no-batch']), [('no-batch', 0x83)])
        # Phase B adds a bit instead of clearing one, so it is a separate
        # preset: 0xa3 | MEDIUM. Its label cannot collide with a period name.
        self.assertEqual(metrics.policy_masks(['medium-on']), [('medium-on', 0x2a3)])
        self.assertEqual(0x2a3, metrics.DEFAULT_POLICY_MASK | 0x200)
        # Phase C adds two more bits, again as separate presets.
        self.assertEqual(metrics.policy_masks(['fitstart-on']), [('fitstart-on', 0x4a3)])
        self.assertEqual(metrics.policy_masks(['ptebatch-on']), [('ptebatch-on', 0x8a3)])
        self.assertEqual(0x4a3, metrics.DEFAULT_POLICY_MASK | 0x400)
        self.assertEqual(0x8a3, metrics.DEFAULT_POLICY_MASK | 0x800)
        self.assertEqual(metrics.policy_masks(['fitpte', 'fitpte-medium']),
                         [('fitpte', metrics.DEFAULT_POLICY_MASK | 0xc00),
                          ('fitpte-medium', metrics.DEFAULT_POLICY_MASK | 0xe00)])
        # Phase D adds SPLIT, a reclaim-side bit that is again a new preset.
        self.assertEqual(metrics.policy_masks(['split-on']), [('split-on', 0x10a3)])
        self.assertEqual(0x10a3, metrics.DEFAULT_POLICY_MASK | 0x1000)
        self.assertEqual(metrics.policy_masks(['split-obs']), [('split-obs', 0x40a3)])
        self.assertEqual(0x40a3, metrics.DEFAULT_POLICY_MASK | 0x4000)
        # Ablations only clear bits of the default mask.
        for _, mask in metrics.policy_masks(['default', 'no-pebs', 'no-batch']):
            self.assertEqual(mask & ~metrics.DEFAULT_POLICY_MASK, 0)
        self.assertEqual(0xa3 & ~0x2, 0xa1)
        self.assertEqual(0xa3 & ~0x20, 0x83)

    def test_raw_and_comma_separated_masks_are_accepted(self):
        self.assertEqual(metrics.policy_masks(['0xa1']), [('fa1', 0xa1)])
        self.assertEqual(metrics.policy_masks(['161']), [('fa1', 0xa1)])
        self.assertEqual(metrics.policy_masks(['default,no-pebs']),
                         [('default', 0xa3), ('no-pebs', 0xa1)])
        self.assertEqual(metrics.policy_masks(['default', 'default']), [('default', 0xa3)])
        # Surviving bits retain their positions: 0x200 is MEDIUM alone, 0x4a3 is default|FITSTART,
        # 0x8a3 is default|PTEBATCH and 0x10a3 is default|SPLIT.
        self.assertEqual(metrics.policy_masks(['0x2a3']), [('f2a3', 0x2a3)])
        self.assertEqual(metrics.policy_masks(['512']), [('f200', 0x200)])
        self.assertEqual(metrics.policy_masks(['0xee3']), [('fee3', 0xee3)])
        self.assertEqual(metrics.policy_masks(['0x10a3']), [('f10a3', 0x10a3)])
        self.assertEqual(metrics.policy_masks(['0x2ea3']), [('f2ea3', 0x2ea3)])
        self.assertEqual(metrics.policy_masks(['0x4ea3']), [('f4ea3', 0x4ea3)])
        for bad in (['bogus'], ['0x1bf'], ['0x4'], ['0x8'], ['0x10'], ['0x100'], ['0x8000'], ['32768'], ['-1'], []):
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
        self.assertEqual(labels['policy-medium'], 0xa3)
        self.assertEqual(labels['policy-medium-no-pebs'], 0xa1)
        self.assertEqual(labels['policy-medium-no-batch'], 0x83)
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
        self.assertEqual(labels['policy-1999'], 0xa3)
        self.assertEqual(labels['policy-1999-no-pebs'], 0xa1)
        self.assertEqual(labels['policy-1999-no-batch'], 0x83)
        self.assertEqual(labels['off'], None)
        self.assertEqual(labels['static-1999'], None)
        # With no presets every cell requests the saved baseline.
        rows = workloads.plan(SimpleNamespace(**base, policy_features=None))
        self.assertEqual({r['case'] for r in rows}, {'off', 'static-1999', 'policy-1999'})
        self.assertTrue(all(r['policy_features'] is None for r in rows))

    def test_base_policy_case_names_expand_across_presets(self):
        # Selecting a base policy name expands across every requested preset.
        presets = metrics.policy_masks(['default', 'fitpte'])
        cases = memcached.matrix_cases('off,policy-medium', presets=presets)
        self.assertEqual([(c[0], c[3]) for c in cases], [
            ('off', None), ('policy-medium', 0xa3), ('policy-medium-fitpte', 0xca3)])
        # Presets without 'default' also expand cleanly.
        obs = metrics.policy_masks(['split-obs'])
        cases = memcached.matrix_cases('policy-high,policy-medium', presets=obs)
        self.assertEqual([(c[0], c[3]) for c in cases], [
            ('policy-medium-split-obs', 0x40a3), ('policy-high-split-obs', 0x40a3)])
        # Raw hex masks expand cleanly too.
        raw = metrics.policy_masks(['0xca3', '0xca1'])
        cases = memcached.matrix_cases('off,static-medium,policy-medium', presets=raw)
        self.assertEqual([(c[0], c[3]) for c in cases], [
            ('off', None), ('static-medium', None),
            ('policy-medium-fca3', 0xca3), ('policy-medium-fca1', 0xca1)])
        # Explicit suffixed selection stays exact.
        cases = memcached.matrix_cases('off,policy-medium-fitpte', presets=presets)
        self.assertEqual([(c[0], c[3]) for c in cases], [
            ('off', None), ('policy-medium-fitpte', 0xca3)])

    def test_split_obs_delta_and_swapin_disabled_waiver(self):
        before = 'aged_total 10\naged_skew_lt12 2\n'
        after = 'aged_total 15\naged_skew_lt12 5\n'
        self.assertEqual(metrics.split_obs_delta(before, after),
                         {'split_obs_aged_total': 5, 'split_obs_aged_skew_lt12': 3})
        self.assertEqual(metrics.split_obs_delta(None, after), {})
        measurement = {
            'granularity': [dict(phase='application', order=0, size_kib=4,
                                 read_wrs=10, write_wrs=0, batch_read_wrs=0,
                                 pebs_decisions=0, pebs_actual_swapins=0)],
            'policy': {'application': dict.fromkeys(metrics.POLICY_FIELDS, 0)},
            'pebs_predict': {'application': 0},
        }
        # Mask 0x80 clears SWAPIN (bit 0): zero base-page swap-in activity is expected.
        activity, issues = workloads.check_policy_activity(
            dict(mode=1, page_kb=64, policy_features=0x80), measurement)
        self.assertTrue(activity['swapin_disabled'])
        self.assertEqual(issues, [])
        # Mask 0xa3 sets SWAPIN: zero activity is still rejected.
        _, issues = workloads.check_policy_activity(
            dict(mode=1, page_kb=64, policy_features=0xa3), measurement)
        self.assertTrue(issues)

    def test_policy_mask_is_written_and_verified_even_for_baselines(self):
        baseline = '0x000000a1'
        for requested in (0x10a3, None, 0, None):
            writes = []
            actual = metrics.apply_policy_mask('control', requested, baseline,
                                               lambda p, v: writes.append(v),
                                               lambda p: str(int(writes[-1], 0)))
            expected = 0xa1 if requested is None else requested
            self.assertEqual(writes, [hex(expected)])
            self.assertEqual(int(actual), expected)
        with self.assertRaisesRegex(RuntimeError, 'expected 0xa1'):
            metrics.apply_policy_mask('control', None, baseline, lambda p, v: None,
                                      lambda p: '0x10a3')


if __name__ == '__main__':
    unittest.main()
