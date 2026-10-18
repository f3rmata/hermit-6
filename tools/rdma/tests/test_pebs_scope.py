#!/usr/bin/env python3
"""Regression: PEBS history and Hermit order policy must not require a cgroup.

The benchmark harness may still use cgroups for memory limits and isolation,
but the kernel path is per-mm: no memory.hermit_pebs file, no memcg lookup in
the policy, and CONFIG_HERMIT does not depend on MEMCG.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
KERNEL = ROOT / 'linux-stable'
TOOLS = ROOT / 'tools' / 'rdma'


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
        self.assertIn('hermit_order_policy(struct mm_struct *mm', text)
        self.assertNotIn('#include <linux/memcontrol.h>', text)
        self.assertNotIn('get_mem_cgroup_from_mm', text)
        self.assertNotIn('hermit_pebs_memcg', text)

    def test_policy_call_sites_use_mm(self):
        page_io = (KERNEL / 'mm/page_io.c').read_text()
        self.assertNotIn('get_mem_cgroup_from_folio', page_io)
        memory = (KERNEL / 'mm/memory.c').read_text()
        block = memory.split('PEBS policy:', 1)[1].split('#endif', 1)[0]
        self.assertIn('hermit_order_policy(vma->vm_mm', block)
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
            if 'results' in path.parts or path.name == 'test_pebs_scope.py':
                continue
            self.assertNotIn('memory.hermit_pebs',
                             path.read_text(errors='ignore'), str(path))


if __name__ == '__main__':
    unittest.main()
