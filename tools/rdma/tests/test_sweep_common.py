"""Shared sweep mechanics, using fixture counters and files instead of a live host."""
from pathlib import Path
import subprocess
import tempfile
import unittest

COMMON = Path(__file__).resolve().parents[1]/'sweep_common.sh'
HEADER = 'order size_bytes stores loads fallback_4k errors\n'


def shell(script, *args, check=True):
    return subprocess.run(['bash', '-c', 'source "$1"; shift; '+script, 'test', str(COMMON),
                           *map(str, args)], text=True, capture_output=True, check=check)


class SweepTests(unittest.TestCase):
    def test_metrics_keep_units_and_fallback_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            before, after = (Path(directory)/name for name in ('before', 'after'))
            before.write_text(HEADER+'0 4096 10 20 0 0\n4 65536 2 3 0 0\n')
            after.write_text(HEADER+'0 4096 14 25 0 0\n4 65536 5 7 1 0\n')
            result = shell('''
                summarize_stores "$1" "$2" 2000
                printf '%s,%s,%s,%s\n' "$total_store_bytes" "$large_store_bytes" \
                    "$large_store_pct" "$protocol_gib_per_sec"
                summarize_loads "$1" "$2" 2
                printf '%s,%s,%s,%s,%s\n' "$total_load_bytes" "$large_load_bytes" \
                    "$large_load_pct" "$load_protocol_gib_per_sec" "$backend_loads_delta"
            ''', before, after)
            self.assertEqual(result.stdout.splitlines(), [
                '212992,131072,61.538,0.000099', '282624,196608,69.565,0.000132,9'])
            for fallback, large_bytes in ((-1, 196608), (99, 0)):
                with self.subTest(fallback=fallback):
                    after.write_text(HEADER+f'4 65536 5 7 {fallback} 0\n')
                    fields = shell('order_metrics "$1" "$2" 3 0', before, after).stdout.strip().split(',')
                    self.assertEqual(fields[1], str(large_bytes))
                    self.assertEqual(fields[3], '0.000000')
            after.write_text(before.read_text())
            self.assertEqual(shell('order_metrics "$1" "$2" 4 1', before, after).stdout.strip(),
                             '0,0,0.000,0.000000,0')
            before.unlink()
            self.assertNotEqual(shell('summarize_loads "$1" "$2" 1', before, after, check=False).returncode, 0)

    def test_page_size_changes_restore_original_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policies = {'enabled': 'madvise', 'hugepages-64kB/enabled': 'always',
                        'hugepages-2048kB/enabled': 'inherit'}
            for path, policy in policies.items():
                file = root/path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(f'never [{policy}]\n')
            mask = root/'remote_order_mask'
            mask.write_text('0x3fd\n')
            result = shell('''
                THP_ROOT=$1; REMOTE_MASK_FILE=$1/remote_order_mask; RESTORE_THP=1
                sudo_cat() { cat "$1"; }
                sudo_write() { printf '%s\n' "$1" > "$2"; }
                save_thp_state
                configure_page_size 64 "$(page_order 64)"
                cat "$THP_ROOT/enabled" "$THP_ROOT/hugepages-64kB/enabled" \
                    "$THP_ROOT/hugepages-2048kB/enabled" "$REMOTE_MASK_FILE"
                configure_page_size 4 "$(page_order 4)"
                cat "$THP_ROOT/enabled" "$THP_ROOT/hugepages-64kB/enabled" \
                    "$THP_ROOT/hugepages-2048kB/enabled" "$REMOTE_MASK_FILE"
                restore_thp_state
            ''', root)
            self.assertEqual(result.stdout.splitlines(),
                             ['never', 'always', 'never', '0x11', 'never', 'never', 'never', '0x1'])
            for path, policy in policies.items():
                self.assertEqual((root/path).read_text().strip(), policy)
            self.assertEqual(mask.read_text().strip(), '0x3fd')

    def test_quiet_wait_tracks_last_change_and_times_out(self):
        with tempfile.TemporaryDirectory() as directory:
            counters = Path(directory)/'counters'
            for values, timeout, expected in [('0\n0\n0\n', 120, '123'),
                                               ('0\n0\n1\n1\n1\n', 120, '777'),
                                               ('0\n1\n', 0, None)]:
                with self.subTest(values=values, timeout=timeout):
                    counters.write_text(values)
                    result = shell('''
                        exec 3< "$1"
                        order_total_stores() { local value; read -r value <&3; printf '%s' "$value"; }
                        sleep() { :; }
                        date() { printf 777; }
                        QUIET_SAMPLES=2; QUIET_TIMEOUT_SEC=$2
                        wait_for_stores_quiet 123
                        printf '%s' "$last_change_ns"
                    ''', counters, timeout, check=False)
                    if expected is None:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('did not become quiet', result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, expected)


if __name__ == '__main__':
    unittest.main()
