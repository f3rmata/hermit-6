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
REDIS_WORKSET_MB     total value bytes to load (default 16384)
REDIS_VALUE_SIZE     value size in bytes (default 2097152 = 2 MiB)
REDIS_VALUE_SEED     seed for the deterministic value template (default 42)
REDIS_CHECKSUM       Y/N, verify CRC32 during scan (default Y)
REDIS_LOAD_PIPELINE  number of SET commands per pipeline batch (default 256)
REDIS_ACTIVE_RATIO   percent of keys read during BENCH (default 100)
REDIS_ACCESS_ORDER   sequential or random (default sequential)
REDIS_ACCESS_SEED    deterministic key-selection/order seed (default 1)
REDIS_CLIENTS        concurrent GET connections (default 1)
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


def scan_dataset(redis, keys, template, check):
    expected_crc = zlib.crc32(template) & 0xffffffff
    expected_sum = (len(keys) * expected_crc) & MASK64
    seen_sum = 0
    errors = 0
    total_bytes = 0
    for key in keys:
        value = redis.execute("GET", key)
        if value is None:
            errors += 1
            continue
        if len(value) != len(template):
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


def scan_dataset_parallel(host, port, keys, template, check, clients):
    worker_count = min(clients, len(keys))
    chunks = [keys[len(keys) * index // worker_count:
                   len(keys) * (index + 1) // worker_count]
              for index in range(worker_count)]
    results = [None] * worker_count
    failures = [None] * worker_count
    barrier = threading.Barrier(worker_count + 1)

    def run(index):
        redis = None
        try:
            redis = Redis(host, port)
            redis.execute("PING")
            barrier.wait()
            results[index] = scan_dataset(redis, chunks[index], template,
                                          check)
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
               for index in range(worker_count)]
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
    return total_bytes, checksum_errors, seconds, worker_count


def main():
    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    port = env_int("REDIS_PORT", 6391)
    workset_mb = env_int("REDIS_WORKSET_MB", 16384)
    value_size = env_int("REDIS_VALUE_SIZE", 2 * 1024 * 1024)
    seed = env_int("REDIS_VALUE_SEED", 42)
    active_ratio = env_float("REDIS_ACTIVE_RATIO", 100.0)
    access_order = os.environ.get("REDIS_ACCESS_ORDER", "sequential").strip().lower()
    access_seed = env_int("REDIS_ACCESS_SEED", 1)
    clients = env_int("REDIS_CLIENTS", 1)
    check = os.environ.get("REDIS_CHECKSUM", "Y").strip().upper() not in ("0", "N", "NO", "OFF")

    if workset_mb <= 0 or value_size <= 0 or clients <= 0:
        log("REDIS_WORKSET_MB, REDIS_VALUE_SIZE and REDIS_CLIENTS must be positive")
        return 2
    if active_ratio <= 0.0 or active_ratio > 100.0:
        log("REDIS_ACTIVE_RATIO must be in (0, 100]")
        return 2
    if access_order not in ("sequential", "random"):
        log("REDIS_ACCESS_ORDER must be sequential or random")
        return 2

    n_keys = max(1, (workset_mb * 1024 * 1024) // value_size)
    keys = ["redis:%08d" % i for i in range(n_keys)]
    active_keys = select_keys(keys, active_ratio, access_order, access_seed)
    actual_access_pct = 100.0 * len(active_keys) / n_keys

    sigs = {signal.SIGUSR1, signal.SIGUSR2, signal.SIGTERM, signal.SIGINT}
    signal.pthread_sigmask(signal.SIG_BLOCK, sigs)

    log("WAITING pid=%d active_keys=%d actual_access_pct=%.6f "
        "access_order=%s clients=%d" %
        (os.getpid(), len(active_keys), actual_access_pct, access_order,
         clients))

    redis = None
    while True:
        sig = signal.sigwait(sigs)
        if sig in (signal.SIGTERM, signal.SIGINT):
            break
        if sig == signal.SIGUSR1:
            start = time.monotonic()
            redis = Redis(host, port)
            redis.execute("PING")
            redis.execute("FLUSHALL")
            rng = random.Random(seed)
            template = rng.randbytes(value_size)
            load_dataset(redis, keys, template)
            populate_sec = time.monotonic() - start
            log("READY pid=%d keys=%d value_size=%d workset_mb=%d "
                "populate_sec=%.6f" %
                (os.getpid(), n_keys, value_size, workset_mb, populate_sec))
        elif sig == signal.SIGUSR2:
            if redis is None:
                log("BENCH skipped: no dataset loaded; send SIGUSR1 first")
                continue
            total_bytes, errors, bench_sec, actual_clients = \
                scan_dataset_parallel(host, port, active_keys, template,
                                      check, clients)
            get_qps = len(active_keys) / bench_sec if bench_sec > 0 else 0.0
            useful_gib_per_sec = (total_bytes / (1024.0 ** 3) / bench_sec
                                  if bench_sec > 0 else 0.0)
            log("BENCH sec=%.6f keys=%d active_keys=%d bytes=%d "
                "actual_access_pct=%.6f access_order=%s clients=%d "
                "get_qps=%.3f useful_gib_per_sec=%.6f "
                "checksum_errors=%d" %
                (bench_sec, n_keys, len(active_keys), total_bytes,
                 actual_access_pct, access_order, actual_clients, get_qps,
                 useful_gib_per_sec, errors))

    if redis is not None:
        redis.close()
    log("EXIT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
