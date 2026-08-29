#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Signal-driven Redis large-value harness for Hermit large-folio swap tests.

The harness implements a minimal RESP client using only the standard library,
so no redis Python package is required on the benchmark host.

Protocol
--------
  1. prints  WAITING pid=<pid>
  2. on SIGUSR1: connects to Redis, flushes DB, loads deterministic large
     string values, prints READY keys=<n> value_size=<n> populate_sec=<sec>
  3. on SIGUSR2: GETs every key sequentially, verifies length and (optionally)
     CRC32 checksums, prints BENCH sec=<sec> bytes=<n> checksum_errors=<n>
  4. on SIGTERM/SIGINT: exits

Environment
-----------
REDIS_HOST           default 127.0.0.1
REDIS_PORT           default 6391
REDIS_PORTS          comma-separated ports; overrides REDIS_PORT
REDIS_WORKSET_MB     total value bytes to load (default 16384)
REDIS_VALUE_SIZE     value size in bytes (default 1048576 = 1 MiB)
REDIS_SCAN_CHUNK     read only the first N bytes of each value during BENCH
                     via GETRANGE (default 65536 = 64 KiB; 0 = whole value)
REDIS_VALUE_SEED     seed for the deterministic value template (default 42)
REDIS_CHECKSUM       Y/N, verify CRC32 during scan (default Y)
REDIS_LOAD_PIPELINE  number of SET commands per pipeline batch (default 256)
REDIS_ACTIVE_RATIO   percent of keys read during BENCH (default 100)
REDIS_ACCESS_ORDER   sequential or random (default sequential)
REDIS_ACCESS_SEED    deterministic key-selection/order seed (default 1)
REDIS_CLIENTS        concurrent GET connections per Redis instance (default 1)
"""

import math
import os
import random
import signal
import socket
import sys
import threading
import time
import zlib


MASK64 = (1 << 64) - 1


def log(msg):
    print(msg, flush=True)


def env_int(name, default):
    val = os.environ.get(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        log("invalid integer %s=%s" % (name, val))
        sys.exit(2)


def env_float(name, default):
    val = os.environ.get(name, "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        log("invalid float %s=%s" % (name, val))
        sys.exit(2)


class Redis:
    def __init__(self, host, port, timeout=30):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.fh = self.sock.makefile("rb")

    def close(self):
        try:
            self.fh.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def send_command(self, *args):
        parts = [b"*%d\r\n" % len(args)]
        for arg in args:
            if isinstance(arg, str):
                arg = arg.encode()
            parts.append(b"$%d\r\n" % len(arg))
            parts.append(arg + b"\r\n")
        self.sock.sendall(b"".join(parts))

    def read_line(self):
        line = self.fh.readline()
        if not line:
            raise ConnectionError("redis connection closed")
        return line[:-2]

    def read_response(self):
        prefix = self.fh.read(1)
        if not prefix:
            raise ConnectionError("redis connection closed")
        if prefix == b"+":
            return self.read_line()
        if prefix == b"-":
            raise RuntimeError(self.read_line().decode(errors="replace"))
        if prefix == b":":
            return int(self.read_line())
        if prefix == b"$":
            length = int(self.read_line())
            if length == -1:
                return None
            data = self.fh.read(length)
            self.fh.read(2)
            return data
        if prefix == b"*":
            count = int(self.read_line())
            if count == -1:
                return None
            return [self.read_response() for _ in range(count)]
        raise RuntimeError("invalid redis response prefix: %r" % prefix)

    def execute(self, *args):
        self.send_command(*args)
        return self.read_response()


def load_dataset(redis, keys, template):
    pipeline = env_int("REDIS_LOAD_PIPELINE", 256)
    if pipeline < 1:
        pipeline = 1
    sent = 0
    while sent < len(keys):
        batch = keys[sent:sent + pipeline]
        for key in batch:
            redis.send_command("SET", key, template)
        for _ in batch:
            redis.read_response()
        sent += len(batch)


def scan_dataset(redis, keys, template, check, scan_chunk=0):
    expected = template[:scan_chunk] if scan_chunk > 0 else template
    expected_crc = zlib.crc32(expected) & 0xffffffff
    expected_sum = (len(keys) * expected_crc) & MASK64
    seen_sum = 0
    errors = 0
    total_bytes = 0
    for key in keys:
        if scan_chunk > 0:
            value = redis.execute("GETRANGE", key, "0", str(scan_chunk - 1))
        else:
            value = redis.execute("GET", key)
        if value is None:
            errors += 1
            continue
        if len(value) != len(expected):
            errors += 1
            continue
        total_bytes += len(value)
        if check:
            seen_sum = (seen_sum + (zlib.crc32(value) & 0xffffffff)) & MASK64
    if check and seen_sum != expected_sum:
        errors += 1
    return total_bytes, errors


def select_keys(keys, ratio, order, seed):
    count = max(1, int(math.ceil(len(keys) * ratio / 100.0)))
    count = min(count, len(keys))
    rng = random.Random(seed)
    if count == len(keys) and order == "sequential":
        return keys
    if count == len(keys):
        selected = list(keys)
    else:
        selected = rng.sample(keys, count)
    if order == "sequential":
        selected.sort()
    else:
        rng.shuffle(selected)
    return selected


def load_datasets_parallel(host, ports, key_groups, template):
    failures = [None] * len(ports)
    barrier = threading.Barrier(len(ports) + 1)

    def run(index):
        redis = None
        try:
            redis = Redis(host, ports[index])
            redis.execute("PING")
            redis.execute("FLUSHALL")
            barrier.wait()
            load_dataset(redis, key_groups[index], template)
        except BaseException as exc:
            failures[index] = exc
            try:
                barrier.abort()
            except threading.BrokenBarrierError:
                pass
        finally:
            if redis is not None:
                redis.close()

    threads = [threading.Thread(target=run, args=(index,))
               for index in range(len(ports))]
    for thread in threads:
        thread.start()
    start = time.monotonic()
    try:
        barrier.wait()
    except threading.BrokenBarrierError:
        pass
    for thread in threads:
        thread.join()
    seconds = time.monotonic() - start
    for failure in failures:
        if failure is not None:
            raise failure
    return seconds


def scan_datasets_parallel(host, ports, key_groups, template, check,
                           clients_per_instance, scan_chunk=0):
    jobs = []
    for port, keys in zip(ports, key_groups):
        worker_count = min(clients_per_instance, len(keys))
        for index in range(worker_count):
            chunk = keys[len(keys) * index // worker_count:
                         len(keys) * (index + 1) // worker_count]
            jobs.append((port, chunk))
    results = [None] * len(jobs)
    failures = [None] * len(jobs)
    barrier = threading.Barrier(len(jobs) + 1)

    def run(index):
        redis = None
        try:
            port, keys = jobs[index]
            redis = Redis(host, port)
            redis.execute("PING")
            barrier.wait()
            results[index] = scan_dataset(redis, keys, template, check,
                                          scan_chunk)
        except BaseException as exc:  # propagate worker failures to main
            failures[index] = exc
            try:
                barrier.abort()
            except threading.BrokenBarrierError:
                pass
        finally:
            if redis is not None:
                redis.close()

    threads = [threading.Thread(target=run, args=(index,))
               for index in range(len(jobs))]
    for thread in threads:
        thread.start()
    start = time.monotonic()
    try:
        barrier.wait()
    except threading.BrokenBarrierError:
        pass
    for thread in threads:
        thread.join()
    seconds = time.monotonic() - start
    for failure in failures:
        if failure is not None:
            raise failure
    total_bytes = sum(result[0] for result in results)
    checksum_errors = sum(result[1] for result in results)
    return total_bytes, checksum_errors, seconds, len(jobs)


def main():
    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    port = env_int("REDIS_PORT", 6391)
    ports_text = os.environ.get("REDIS_PORTS", "").strip()
    try:
        ports = ([int(item) for item in ports_text.split(",") if item]
                 if ports_text else [port])
    except ValueError:
        log("invalid REDIS_PORTS=%s" % ports_text)
        return 2
    workset_mb = env_int("REDIS_WORKSET_MB", 16384)
    value_size = env_int("REDIS_VALUE_SIZE", 1024 * 1024)
    scan_chunk = env_int("REDIS_SCAN_CHUNK", 64 * 1024)
    seed = env_int("REDIS_VALUE_SEED", 42)
    active_ratio = env_float("REDIS_ACTIVE_RATIO", 100.0)
    access_order = os.environ.get("REDIS_ACCESS_ORDER", "sequential").strip().lower()
    access_seed = env_int("REDIS_ACCESS_SEED", 1)
    clients = env_int("REDIS_CLIENTS", 1)
    check = os.environ.get("REDIS_CHECKSUM", "Y").strip().upper() not in ("0", "N", "NO", "OFF")

    if (workset_mb <= 0 or value_size <= 0 or clients <= 0 or not ports or
            any(item <= 0 or item > 65535 for item in ports) or
            len(set(ports)) != len(ports)):
        log("REDIS_WORKSET_MB, REDIS_VALUE_SIZE and REDIS_CLIENTS must be positive")
        return 2
    if scan_chunk < 0 or scan_chunk > value_size:
        log("REDIS_SCAN_CHUNK must be between 0 and REDIS_VALUE_SIZE")
        return 2
    if active_ratio <= 0.0 or active_ratio > 100.0:
        log("REDIS_ACTIVE_RATIO must be in (0, 100]")
        return 2
    if access_order not in ("sequential", "random"):
        log("REDIS_ACCESS_ORDER must be sequential or random")
        return 2

    n_keys = max(1, (workset_mb * 1024 * 1024) // value_size)
    if len(ports) > n_keys:
        log("number of Redis instances must not exceed number of keys")
        return 2
    keys = ["redis:%08d" % i for i in range(n_keys)]
    key_groups = [keys[n_keys * index // len(ports):
                       n_keys * (index + 1) // len(ports)]
                  for index in range(len(ports))]
    active_key_groups = [
        select_keys(group, active_ratio, access_order, access_seed + index)
        for index, group in enumerate(key_groups)
    ]
    active_key_count = sum(len(group) for group in active_key_groups)
    actual_access_pct = 100.0 * active_key_count / n_keys

    sigs = {signal.SIGUSR1, signal.SIGUSR2, signal.SIGTERM, signal.SIGINT}
    signal.pthread_sigmask(signal.SIG_BLOCK, sigs)

    log("WAITING pid=%d active_keys=%d actual_access_pct=%.6f "
        "access_order=%s instances=%d clients_per_instance=%d" %
        (os.getpid(), active_key_count, actual_access_pct, access_order,
         len(ports), clients))

    loaded = False
    while True:
        sig = signal.sigwait(sigs)
        if sig in (signal.SIGTERM, signal.SIGINT):
            break
        if sig == signal.SIGUSR1:
            rng = random.Random(seed)
            template = rng.randbytes(value_size)
            populate_sec = load_datasets_parallel(host, ports, key_groups,
                                                  template)
            loaded = True
            log("READY pid=%d keys=%d value_size=%d scan_chunk=%d "
                "workset_mb=%d populate_sec=%.6f instances=%d" %
                (os.getpid(), n_keys, value_size, scan_chunk, workset_mb,
                 populate_sec, len(ports)))
        elif sig == signal.SIGUSR2:
            if not loaded:
                log("BENCH skipped: no dataset loaded; send SIGUSR1 first")
                continue
            total_bytes, errors, bench_sec, actual_clients = \
                scan_datasets_parallel(host, ports, active_key_groups,
                                       template, check, clients, scan_chunk)
            get_qps = active_key_count / bench_sec if bench_sec > 0 else 0.0
            useful_gib_per_sec = (total_bytes / (1024.0 ** 3) / bench_sec
                                  if bench_sec > 0 else 0.0)
            log("BENCH sec=%.6f keys=%d active_keys=%d bytes=%d "
                "scan_chunk=%d actual_access_pct=%.6f access_order=%s "
                "clients=%d instances=%d clients_per_instance=%d "
                "get_qps=%.3f useful_gib_per_sec=%.6f "
                "checksum_errors=%d" %
                (bench_sec, n_keys, active_key_count, total_bytes, scan_chunk,
                 actual_access_pct, access_order, actual_clients, len(ports),
                 clients, get_qps, useful_gib_per_sec, errors))

    log("EXIT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
