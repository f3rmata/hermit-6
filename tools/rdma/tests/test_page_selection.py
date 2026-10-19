#!/usr/bin/env python3
"""Offline correctness checks; no Hermit syscalls, THP writes or RDMA experiments."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import struct
from unittest.mock import patch
import sys
import tempfile
import unittest

PEBS = Path(__file__).resolve().parents[1]/'pebs'
sys.path.insert(0, str(PEBS))
import page_selection_metrics as metrics
import run_page_selection_microbench as runner
import plot_page_selection_microbench as plot


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

    def test_plot_refuses_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'manifest.json').write_text(json.dumps({'schema': 'page-selection-v1'}))
            with self.assertRaisesRegex(ValueError, 'No measured epochs'):
                plot.load_rows(root)

    @unittest.skipUnless(importlib.util.find_spec('matplotlib'), 'Matplotlib unavailable')
    def test_plot_pipeline_with_explicit_temporary_fixture(self):
        # Synthetic fixture validates file parsing/rendering only; not experimental evidence.
        with tempfile.TemporaryDirectory(prefix='page-selection-test-fixture-') as tmp:
            root = Path(tmp)
            spec = {'scenario': 'switch', 'case': 'fixed-4k', 'repeat': 1, 'phases': [1, 512, 1]}
            (root/'manifest.json').write_text(json.dumps({'schema': 'page-selection-v1', 'runs': [spec],
                'args': {'epochs': 1, 'cases': 'fixed-4k', 'scenarios': 'switch', 'workset_mb': 4}}))
            (root/'status').write_text('completed')
            run = root/'0001-fixture'; run.mkdir()
            for phase, density in enumerate(spec['phases'], 1):
                cycle = run/f'p{phase}-e1'; cycle.mkdir()
                a, b = cycle/'a', cycle/'b'
                self.counters(a, 10); self.counters(b, 20)
                delta = metrics.deltas(a, b)
                row = spec | {'phase': phase, 'epoch': 1, 'density_pages': density, 'valid': True,
                    'warnings': ['SYNTHETIC TEST FIXTURE'], 'access_delta': delta, 'pageout_delta': delta,
                    'access': {'seconds': 1., 'page_coverage_bytes': density * 8192,
                               'read_instructions': density * 128, 'checksum_errors': 0},
                    'pageout': {'seconds': 2., 'settle_seconds': .3}}
                (cycle/'result.json').write_text(json.dumps(row))
                for stage in ['before', 'evicted', 'after']:
                    d = cycle/f'pages-{stage}'; d.mkdir()
                    (d/'page-sizes.json').write_text(json.dumps({
                        'resident_mapped_bytes': 4 * 1024**2, 'swapped_mapped_bytes': 0,
                        'unknown_resident_bytes': 0, 'not_present_mapped_bytes': 0,
                        'folio_size_kib': {'4': {'mapped_bytes': 4 * 1024**2}}}))
            result = subprocess.run([sys.executable, str(PEBS/'plot_page_selection_microbench.py'), str(root)],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ['switch-metrics.png', 'switch-folios.png', 'switch-timeline.png']:
                self.assertTrue((root/'analysis'/name).read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertEqual(len(json.loads((root/'analysis'/'runs.json').read_text())), 1)


if __name__ == '__main__':
    unittest.main()
