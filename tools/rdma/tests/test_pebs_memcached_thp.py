#!/usr/bin/env python3
"""Offline verification of per-case THP isolation; does not access sysfs."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1]/'pebs/pebs_perf_matrix.py'
spec = importlib.util.spec_from_file_location('matrix', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ROOT = '/sys/kernel/mm/transparent_hugepage/'


class ThpCases(unittest.TestCase):
    def setUp(self):
        self.original = {ROOT+'enabled':'madvise',ROOT+'hugepages-64kB/enabled':'always',
                         ROOT+'hugepages-2048kB/enabled':'inherit'}

    def test_compound_extent_for_head_tail_and_base(self):
        flags = {16:1<<15, **{i:1<<16 for i in range(17,32)}}
        get = lambda pfn: flags.get(pfn,0)
        self.assertEqual(module.folio_extent(16,get),(16,16))
        self.assertEqual(module.folio_extent(23,get),(16,16))
        self.assertEqual(module.folio_extent(32,get),(32,1))
        self.assertIsNone(module.folio_extent(2,lambda p: (1<<16) if p==2 else 0))

    def test_no_page_size_forcing_remains(self):
        source = path.read_text()
        self.assertNotIn("write(path,value)",source)
        self.assertNotIn("base_page_baseline_valid",source)
        self.assertNotIn("--page-kb",source)

    def test_sampler_affinity_is_applied_and_verified(self):
        with patch.object(module.Path,'glob',return_value=[Path('/proc/123')]), \
             patch.object(module,'read',return_value='hermit_pebsd\n'), \
             patch.object(module.os,'sched_setaffinity') as setter, \
             patch.object(module.os,'sched_getaffinity',return_value={0,1}):
            self.assertEqual(module.bind_sampler([0,1]),{'pid':123,'cpus':[0,1]})
            setter.assert_called_once_with(123,{0,1})
        with patch.object(module.Path,'glob',return_value=[Path('/proc/123')]), \
             patch.object(module,'read',return_value='hermit_pebsd\n'), \
             patch.object(module.os,'sched_setaffinity'), \
             patch.object(module.os,'sched_getaffinity',return_value={2}):
            with self.assertRaises(RuntimeError):module.bind_sampler([0,1])

    def test_connection_limits_cover_largest_default_calibration(self):
        with patch.object(module.resource,'getrlimit',side_effect=[(1024,1048576),(65536,1048576)]), \
             patch.object(module.resource,'setrlimit') as setter:
            settings=module.configure_connection_limits(16,256)
            setter.assert_called_once_with(module.resource.RLIMIT_NOFILE,(65536,1048576))
            self.assertEqual(settings['estimated_client_connections'],4096)
            self.assertEqual(settings['server_max_connections'],32768)

    def test_cpu_lists(self):
        self.assertEqual(module.parse_cpu_list('0-3,6,2'),[0,1,2,3,6])
        with self.assertRaises(ValueError): module.parse_cpu_list('3-1')


    def test_final_stats_retries_and_preserves_diagnostics(self):
        import tempfile,json
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);server=Mock();server.poll.return_value=None
            with patch.object(module,'stats',side_effect=[TimeoutError('slow'), 'STAT evictions 0\r\nEND\r\n']) as fetch:
                module.collect_final_stats(11329,run,server)
            self.assertEqual(fetch.call_count,2)
            records=json.loads((run/'stats-collection.json').read_text())
            self.assertFalse(records[0]['success']);self.assertTrue(records[1]['success'])
            self.assertIn('STAT evictions 0',(run/'memcached.stats').read_text())

    def test_final_stats_exhaustion_is_not_validated_as_success(self):
        import tempfile,json
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            run=Path(tmp);server=Mock();server.poll.return_value=None
            with patch.object(module,'stats',side_effect=TimeoutError('slow')) as fetch:
                with self.assertRaisesRegex(RuntimeError,'measurement is incomplete'):
                    module.collect_final_stats(11329,run,server)
            self.assertEqual(fetch.call_count,3)
            self.assertFalse((run/'memcached.stats').exists())
            self.assertEqual(len(json.loads((run/'stats-collection.json').read_text())),3)


if __name__ == '__main__': unittest.main()
