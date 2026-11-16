"""Shared sweep mechanics, using fixture counters and files instead of a live host."""
from pathlib import Path
import json
import select
import signal
import subprocess
import tempfile
import unittest

COMMON = Path(__file__).resolve().parents[1]/'sweep_common.sh'
HEADER = 'order size_bytes stores loads fallback_4k errors\n'


def shell(script, *args, check=True):
    return subprocess.run(['bash', '-c', 'source "$1"; shift; '+script, 'test', str(COMMON),
                           *map(str, args)], text=True, capture_output=True, check=check)


class SweepTests(unittest.TestCase):
    def test_absolute_memory_budget(self):
        self.assertEqual(shell('MEMORY_LIMIT_MB=512; benchmark_memory_limit_mb 2000 70').stdout.strip(),'512')
        self.assertEqual(shell('unset MEMORY_LIMIT_MB; benchmark_memory_limit_mb 2000 70').stdout.strip(),'1400')
        self.assertNotEqual(shell('MEMORY_LIMIT_MB=bad; benchmark_memory_limit_mb 2000 70',check=False).returncode,0)

    def test_standard_ycsb_mix_is_preserved(self):
        source=(COMMON.parent/'run_ycsb_page_sweep.sh').read_text()
        start=source.index('generate_workload_file() {')
        definition=source[start:source.index('\n}\n',start)+3]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);profile=root/'workloada';output=root/'generated'
            profile.write_text('workload=site.ycsb.workloads.CoreWorkload\nreadproportion=0.5\nupdateproportion=0.5\n')
            shell(definition+'''\nYCSB_WORKLOAD_FILE=$1; YCSB_RECORDCOUNT=100
                YCSB_OPERATIONCOUNT=1000; YCSB_FIELDCOUNT=10; YCSB_FIELDLENGTH=100
                YCSB_READALLFIELDS=true; BINDING=redis
                generate_workload_file "$2" 6379
                ''',profile,output)
            text=output.read_text()
            self.assertIn('updateproportion=0.5',text)
            self.assertEqual(text.count('readproportion='),1)
            self.assertIn('fieldlength=100',text)

    def test_process_thp_disable_is_rejected_and_status_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status, output = root/'status', root/'captured-status'
            for value in ('1', '0', '3', None):
                with self.subTest(value=value):
                    text = 'Name:\tredis-server\n'
                    if value is not None:
                        text += f'THP_enabled:\t{value}\n'
                    status.write_text(text)
                    result = shell('''
                        fixture=$1
                        sudo_cat() { [ "$1" = /proc/123/status ] || exit 9; cat "$fixture"; }
                        require_process_thp_enabled 123 "$2"
                    ''', status, output, check=False)
                    self.assertEqual(output.read_text(), text)
                    if value == '1':
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('process-level THP disable', result.stderr)

    def test_redis_and_ycsb_launchers_disable_redis_thp_optout(self):
        for script_name, function, instances in [
                ('run_redis_page_sweep.sh', 'start_redis_servers', 2),
                ('run_ycsb_page_sweep.sh', 'start_server', 1)]:
            with self.subTest(script=script_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                binary = root/'redis-server'
                binary.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
port = sys.argv[sys.argv.index('--port') + 1]
(Path(os.environ['REDIS_ARG_DIR']) / ('argv-' + port + '.json')).write_text(json.dumps(sys.argv[1:]))
''')
                binary.chmod(0o755)
                source = (COMMON.parent/script_name).read_text()
                start = source.index(function+'() {')
                definition = source[start:source.index('\n}\n', start)+3]
                shell(definition + '''
                    REDIS_SERVER_BIN=$1
                    export REDIS_ARG_DIR=$2
                    REDIS_INSTANCES=2; REDIS_PORT=6391; BINDING=redis
                    REDIS_SERVER_CPUS=0; SERVER_CPUS=0; CGROUP=/unused
                    taskset() { shift 2; "$@"; }
                    sudo_write() { :; }
                    enable_benchmark_pebs_pid() { :; }
                    ''' + function + ' "$2"\nwait\n', binary, root)
                files = sorted(root.glob('argv-*.json'))
                self.assertEqual(len(files), instances)
                for file in files:
                    args = json.loads(file.read_text())
                    self.assertEqual(args.count('--disable-thp'), 1)
                    self.assertEqual(args[args.index('--disable-thp')+1], 'no')

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

    def test_allocation_ceiling_keeps_smaller_orders_and_never_writes_mask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policies = {'enabled': 'madvise', 'hugepages-16kB/enabled': 'never',
                        'hugepages-32kB/enabled': 'never', 'hugepages-64kB/enabled': 'always',
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
                sudo_write() {
                    [ "$2" != "$REMOTE_MASK_FILE" ] || exit 31
                    printf '[%s]\n' "$1" > "$2"
                }
                save_thp_state
                for cap in 4 16 32 64 2048; do
                    configure_page_size "$cap"
                    for file in enabled hugepages-16kB/enabled hugepages-32kB/enabled \
                                hugepages-64kB/enabled hugepages-2048kB/enabled; do
                        selected_policy "$THP_ROOT/$file"
                    done
                done
                # Hermit configuration must also be read-only in ceiling sweeps.
                mount_debugfs_if_needed() { exit 32; }
                set_hermit_flag() { exit 33; }
                set_debugfs_file() { exit 34; }
                configure_hermit_mode
                restore_thp_state
            ''', root)
            self.assertEqual(result.stdout.splitlines(),
                             ['never']*5 + ['never', 'always', 'never', 'never', 'never'] +
                             ['never', 'always', 'always', 'never', 'never'] +
                             ['never', 'always', 'always', 'always', 'never'] + ['always']*5)
            for path, policy in policies.items():
                self.assertEqual((root/path).read_text().strip(), f'[{policy}]')
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

    def test_ceiling_write_failure_is_not_reported_as_success(self):
        result = shell('''
            THP_ROOT=/fixture; thp_files=(/fixture/enabled)
            sudo_write() { return 1; }
            if configure_page_size 4; then exit 9; fi
        ''')
        self.assertEqual(result.returncode, 0)

    def test_chunk_access_geometry_is_independent_of_page_advice(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory)/'anon-workset'
            subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-std=c11', '-pthread',
                            '-o', str(binary), str(COMMON.parent/'anon_seq_workset.c')], check=True)
            for advice in ('base', 'huge'):
                with self.subTest(advice=advice):
                    p = subprocess.Popen([str(binary), '4', advice, '2048', 'chunk64k',
                                          'sequential', 'high', '1', '1'], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True)
                    assert p.stdout is not None and p.stderr is not None
                    stdout, stderr = p.stdout, p.stderr
                    def response(prefix):
                        self.assertTrue(select.select([stdout], [], [], 10)[0], 'worker timed out')
                        line = stdout.readline()
                        self.assertTrue(line.startswith(prefix), line)
                        return dict(word.split('=', 1) for word in line.split()[1:])
                    try:
                        response('WAITING')
                        p.send_signal(signal.SIGUSR1)
                        ready = response('READY')
                        self.assertEqual(int(ready['accessed_pages']), 32)
                        self.assertEqual(int(ready['accessed_bytes']), 128*1024)
                        p.send_signal(signal.SIGUSR2)
                        scan = response('SCAN')
                        self.assertEqual(scan['accessed_bytes'], ready['accessed_bytes'])
                        self.assertEqual(scan['checksum_errors'], '0')
                        p.terminate()
                        self.assertEqual(p.wait(timeout=10), 0)
                    finally:
                        if p.poll() is None:
                            p.kill(); p.wait()
                        p.stdout.close(); p.stderr.close()

    def test_bandwidth_uses_the_interval_its_bytes_were_sampled_in(self):
        # Protocol counters are snapshotted around a phase, so the pairing
        # interval is wall time between the snapshots. GAPBS verification
        # runs inside that window but outside the trial timer.
        self.assertEqual(shell('elapsed_seconds 1000000000 3500000000').stdout.strip(),
                         '2.500000')
        self.assertEqual(shell('elapsed_seconds 500 500').stdout.strip(), '0.000000')
        for values in ('4000 3000', '4000 x'):
            with self.subTest(values=values):
                self.assertNotEqual(shell('elapsed_seconds '+values, check=False).returncode, 0)
        with tempfile.TemporaryDirectory() as directory:
            before, after = (Path(directory)/name for name in ('before', 'after'))
            # 2 MiB loaded, counted over a 4 s phase including verification.
            before.write_text(HEADER+'0 4096 0 0 0 0\n')
            after.write_text(HEADER+'0 4096 0 512 0 0\n')
            seconds = shell('elapsed_seconds 2000000000 6000000000').stdout.strip()
            result = shell('summarize_loads "$1" "$2" "$3"; printf "%s|%s\n" \
                "$total_load_bytes" "$load_protocol_gib_per_sec"', before, after, seconds)
            # 512 pages * 4096 B / 4 s = 0.000488 GiB/s, not /trial-time.
            self.assertEqual(result.stdout.strip(), '2097152|0.000488')

    def test_gapbs_reports_phase_bandwidth_not_trial_bandwidth(self):
        source = (COMMON.parent/'run_gapbs_page_sweep.sh').read_text()
        self.assertIn('load_start_ns=$(date +%s%N)', source)
        self.assertIn('load_end_ns=$(date +%s%N)', source)
        self.assertIn('load_phase_sec=$(elapsed_seconds "$load_start_ns" "$load_end_ns")', source)
        self.assertIn('summarize_loads "$load_before" "$load_after" "$load_phase_sec"', source)
        self.assertIn('store_phase_sec=$(elapsed_seconds "$store_start_ns" "$last_change_ns")', source)
        self.assertNotIn('summarize_loads "$load_before" "$load_after" "$trial_sec"', source)
        header = next(line for line in source.splitlines() if line.startswith("printf 'page_kb,"))
        self.assertIn('store_phase_sec,store_phase_gib_per_sec', header)
        self.assertIn('load_phase_sec,load_phase_gib_per_sec', header)
        self.assertNotIn('load_protocol_gib_per_sec', header)


    def test_cost_table_helpers_diff_stable_columns(self):
        keys = ' '.join(shell('cost_keys').stdout.split())
        self.assertEqual(keys.split(),
                         ['order0_faults', 'order0_ns', 'order1_faults', 'order1_ns',
                          'order2_faults', 'order2_ns', 'order3_faults', 'order3_ns',
                          'order4_faults', 'order4_ns', 'order5_faults', 'order5_ns',
                          'order6_faults', 'order6_ns', 'order7_faults', 'order7_ns',
                          'order8_faults', 'order8_ns', 'order9_faults', 'order9_ns',
                          'prefetched_4k', 'prefetch_hit_faults', 'prefetch_hit_ns'])
        with tempfile.TemporaryDirectory() as directory:
            before, after = (Path(directory)/name for name in ('before', 'after'))
            before.write_text('enabled 1\norder0_faults 10\norder0_ns 1000\nprefetch_hit_faults 2\n')
            after.write_text('enabled 1\norder0_faults 14\norder0_ns 5000\nprefetch_hit_faults 5\n')
            self.assertEqual(shell('cost_delta "$1" "$2" order0_faults; echo; \
                cost_delta "$1" "$2" order0_ns; echo; \
                cost_delta "$1" "$2" prefetch_hit_faults; echo; \
                cost_delta "$1" "$2" order9_ns', before, after).stdout.split(),
                ['4', '4000', '3', '0'])
            # A reset counter must not be reported as a negative delta.
            before.write_text('order0_faults 20\n')
            after.write_text('order0_faults 3\n')
            self.assertNotEqual(shell('cost_delta "$1" "$2" order0_faults', before, after,
                                      check=False).returncode, 0)

    def test_policy_mask_names_match_the_python_presets(self):
        expected = {'default': '0x1bf', 'no-pebs': '0x1bd', 'no-batch': '0x19f'}
        for name, mask in expected.items():
            with self.subTest(preset=name):
                self.assertEqual(shell('policy_mask_of '+name).stdout.strip(), mask)
                self.assertEqual(shell('valid_policy_mask '+mask).returncode, 0)
        self.assertEqual(shell('policy_mask_of 0x1ae').stdout.strip(), '0x1ae')
        for bad in ('0x200', '1024', 'zz', ''):
            with self.subTest(mask=bad):
                self.assertNotEqual(shell('valid_policy_mask "$1"', bad, check=False).returncode, 0)


    def test_granularity_runner_parses_the_trial_protocol(self):
        runner = COMMON.parent/'bench'/'run_swapin_granularity.sh'
        source = runner.read_text()

        def definition(name):
            start = source.index(name+'() {')
            return source[start:source.index('\n}\n', start)+3]

        log = ('POPULATE pages=4 size_bytes=16384 sec=0.1\n'
               'READY pid=9 pages=4 size_bytes=16384\nRUN\n'
               'TRIAL seg=1 trial=1 start_page=512 bytes=4096 sec=0.001 gib_per_sec=3.8 '
               'minflt=2 majflt=1 checksum_errors=0\n'
               'TRIAL seg=1 trial=2 start_page=768 bytes=4096 sec=0.003 gib_per_sec=1.3 '
               'minflt=1 majflt=1 checksum_errors=0\n'
               'TRIAL seg=4 trial=1 start_page=1024 bytes=16384 sec=0.004 gib_per_sec=3.8 '
               'minflt=0 majflt=4 checksum_errors=0\n'
               'DONE sec=0.008 trials=3 pages=6 checksum_errors=0\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'bench.log'
            path.write_text(log)
            result = shell(definition('emit_trials') + definition('trial_summary') + '''
                emit_trials "$1" 64 policy 0x1bf
                trial_summary 1 "$1"
                trial_summary 4 "$1"
                trial_summary 16 "$1"
            ''', path)
            lines = result.stdout.splitlines()
        self.assertEqual(lines[0], '64,policy,0x1bf,1,1,512,4096,0.001,3.8,2,1,0')
        self.assertEqual(lines[1], '64,policy,0x1bf,1,2,768,4096,0.003,1.3,1,1,0')
        self.assertEqual(lines[2], '64,policy,0x1bf,4,1,1024,16384,0.004,3.8,0,4,0')
        # trial_summary prints faults,checksum_errors,seconds,bytes per segment.
        self.assertEqual(lines[3], '5,0,0.004000,8192')
        self.assertEqual(lines[4], '4,0,0.004000,16384')
        self.assertEqual(lines[5], '0,0,0.000000,0')


if __name__ == '__main__':
    unittest.main()
