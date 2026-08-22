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
"""

import os
import random
import signal
import socket
import sys
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


def main():
    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    port = env_int("REDIS_PORT", 6391)
    workset_mb = env_int("REDIS_WORKSET_MB", 16384)
    value_size = env_int("REDIS_VALUE_SIZE", 2 * 1024 * 1024)
    seed = env_int("REDIS_VALUE_SEED", 42)
    check = os.environ.get("REDIS_CHECKSUM", "Y").strip().upper() not in ("0", "N", "NO", "OFF")

    if workset_mb <= 0 or value_size <= 0:
        log("REDIS_WORKSET_MB and REDIS_VALUE_SIZE must be positive")
        return 2

    n_keys = max(1, (workset_mb * 1024 * 1024) // value_size)
    keys = ["redis:%08d" % i for i in range(n_keys)]

    sigs = {signal.SIGUSR1, signal.SIGUSR2, signal.SIGTERM, signal.SIGINT}
    signal.pthread_sigmask(signal.SIG_BLOCK, sigs)

    log("WAITING pid=%d" % os.getpid())

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
            start = time.monotonic()
            total_bytes, errors = scan_dataset(redis, keys, template, check)
            bench_sec = time.monotonic() - start
            log("BENCH sec=%.6f keys=%d bytes=%d checksum_errors=%d" %
                (bench_sec, n_keys, total_bytes, errors))

    if redis is not None:
        redis.close()
    log("EXIT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
