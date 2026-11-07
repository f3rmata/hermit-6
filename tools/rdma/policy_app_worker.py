#!/usr/bin/env python3
"""Deterministic SQLite query/update workload used by run_policy_app_matrix.py."""
import argparse
import concurrent.futures
import json
import random
import sqlite3
import threading
import time
from pathlib import Path


def value(key):
    return (key * 17) % 1000003


def create_database(path, rows, payload_bytes):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=DELETE')
    db.execute('CREATE TABLE kv (id INTEGER PRIMARY KEY, v INTEGER NOT NULL, payload BLOB NOT NULL)')
    payload = bytes(range(256)) * (payload_bytes // 256) + bytes(range(payload_bytes % 256))
    for start in range(0, rows, 4096):
        db.executemany('INSERT INTO kv VALUES (?, ?, ?)',
                       ((i, value(i), payload) for i in range(start, min(rows, start + 4096))))
        db.commit()
    assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    db.close()


def percentile(values, fraction):
    data = sorted(values)
    return data[min(len(data) - 1, int((len(data) - 1) * fraction))] if data else None


def sqlite_work(config, mode, threads):
    """Each worker owns a private in-memory DB, so updates are reproducible.

    Memory budgets are calibrated separately for 1 and 8 workers. Loading,
    warmup and correctness checks belong to process time, not operation time.
    """
    c = config['sqlite']
    rows, operations = c['rows'], c['operations']
    barrier = threading.Barrier(threads, timeout=config['timeout_seconds'])

    def work(worker):
        rng = random.Random(config['seed'] + worker)
        keys = [rng.randrange(rows) for _ in range(operations)]
        expected = sum(value(k) for k in keys) if mode == 'query' else sum(value(k) for k in range(rows)) + operations
        src = sqlite3.connect(Path(c['database']).as_uri() + '?mode=ro', uri=True)
        db = sqlite3.connect(':memory:')
        src.backup(db)
        src.close()
        db.execute('PRAGMA temp_store=MEMORY')
        db.execute('SELECT sum(v) FROM kv').fetchone()
        barrier.wait()
        samples = []
        checksum = 0
        start = time.perf_counter_ns()
        if mode == 'query':
            for key in keys:
                op_start = time.perf_counter_ns()
                checksum += db.execute('SELECT v FROM kv WHERE id=?', (key,)).fetchone()[0]
                samples.append(time.perf_counter_ns() - op_start)
            unit = 'query'
        else:
            batch = c['transaction_size']
            for pos in range(0, len(keys), batch):
                op_start = time.perf_counter_ns()
                with db:
                    db.executemany('UPDATE kv SET v=v+1 WHERE id=?',
                                   ((k,) for k in keys[pos:pos + batch]))
                samples.append(time.perf_counter_ns() - op_start)
            unit = 'transaction'
        end = time.perf_counter_ns()
        if mode == 'update':
            checksum = db.execute('SELECT sum(v) FROM kv').fetchone()[0]
        valid = checksum == expected and db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
        db.close()
        return dict(worker=worker, start_ns=start, end_ns=end, operations=operations,
                    checksum=checksum, expected=expected, valid=valid, latency_unit=unit,
                    latencies_ns=samples)

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        results = list(pool.map(work, range(threads)))
    elapsed = (max(r['end_ns'] for r in results) - min(r['start_ns'] for r in results)) / 1e9
    samples = [t for r in results for t in r.pop('latencies_ns')]
    return dict(valid=all(r['valid'] for r in results), workers=results,
                operation_seconds=elapsed, throughput_ops_s=operations * threads / elapsed,
                latency_unit=results[0]['latency_unit'],
                latency_p50_us=percentile(samples, .5) / 1000,
                latency_p95_us=percentile(samples, .95) / 1000,
                latency_p99_us=percentile(samples, .99) / 1000)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    p.add_argument('--mode', choices=['query', 'update'], required=True)
    p.add_argument('--threads', type=int, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = sqlite_work(json.loads(a.config.read_text()), a.mode, a.threads)
    a.output.write_text(json.dumps(result, indent=2) + '\n')
    if not result['valid']:
        raise SystemExit('SQLite correctness check failed')


if __name__ == '__main__':
    main()
