#!/usr/bin/env python3
"""Offline verification of per-case THP isolation; does not access sysfs."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1]/'pebs/pebs_perf_matrix.py'
spec = importlib.util.spec_from_file_location('matrix', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ROOT = '/sys/kernel/mm/transparent_hugepage/'


class ThpCases(unittest.TestCase):
    def setUp(self):
        self.original = {ROOT+'enabled':'madvise',ROOT+'hugepages-64kB/enabled':'always',
                         ROOT+'hugepages-2048kB/enabled':'inherit'}

    def test_off_disables_every_size_in_both_modes(self):
        for mode in ('always','preserve'):
            self.assertEqual(set(module.case_thp_settings('off',mode,64,self.original).values()),{'never'})

    def test_policy_after_off_gets_64k(self):
        module.case_thp_settings('off','always',64,self.original)
        settings = module.case_thp_settings('policy-medium','always',64,self.original)
        self.assertEqual(settings[ROOT+'enabled'],'never')
        self.assertEqual(settings[ROOT+'hugepages-64kB/enabled'],'always')
        self.assertEqual(settings[ROOT+'hugepages-2048kB/enabled'],'never')

    def test_preserve_restores_original_after_off(self):
        module.case_thp_settings('off','preserve',64,self.original)
        self.assertEqual(module.case_thp_settings('static-medium','preserve',64,self.original),self.original)

    def test_2m_and_explicit_4k(self):
        settings=module.case_thp_settings('policy-medium','always',2048,self.original)
        self.assertEqual(settings[ROOT+'enabled'],'always')
        self.assertEqual(settings[ROOT+'hugepages-2048kB/enabled'],'always')
        self.assertEqual(set(module.case_thp_settings('static-medium','always',4,self.original).values()),{'never'})


if __name__ == '__main__': unittest.main()
