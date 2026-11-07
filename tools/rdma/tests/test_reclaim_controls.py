"""Exercise real runner configuration with mocked debugfs, never a live host."""
from pathlib import Path
import subprocess
import unittest

COMMON = Path(__file__).resolve().parents[1] / 'common.sh'


class ReclaimControls(unittest.TestCase):
    def configure(self, stop, supported):
        script = '''source "$1"
            MODE=hermit; PAGE_SIZE_POLICY=; RSWAP_REQUIRED_BACKEND=rdma
            RECLAIM_STOP_HEADROOM_PAGES=$2; supported=$3
            mount_debugfs_if_needed() { :; }
            sudo_test() { [ "$1" = -d ] || [ "$supported" = 1 ]; }
            sudo_write() { printf '%s=%s\\n' "$2" "$1"; }
            detect_rswap_backend() { printf rdma; }
            configure_hermit_mode
        '''
        return subprocess.run(['bash', '-c', script, 'test', str(COMMON), str(stop), str(supported)],
                              capture_output=True, text=True)

    def test_requested_hysteresis_is_applied(self):
        r = self.configure(64, 1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('/sys/kernel/debug/hermit/reclaim_stop_headroom_pages=64', r.stdout)

    def test_default_resets_new_control_and_accepts_older_kernel(self):
        for supported in (0, 1):
            r = self.configure(0, supported)
            self.assertEqual(r.returncode, 0, r.stderr)
            if supported:
                self.assertIn('/sys/kernel/debug/hermit/reclaim_stop_headroom_pages=0', r.stdout)

    def test_unsupported_request_fails_before_writing_controls(self):
        r = self.configure(64, 0)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '')
        self.assertIn('running kernel', r.stderr)

    def test_invalid_setting_fails_before_writing_controls(self):
        for value in ('bad', '-1'):
            r = self.configure(value, 1)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(r.stdout, '')


if __name__ == '__main__':
    unittest.main()
