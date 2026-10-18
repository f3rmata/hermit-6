#!/usr/bin/env python3
"""Opt one process's mm into Hermit PEBS history via syscall 473.

This is the cgroup-free control path used by the benchmark scripts: the
memory cgroup still supplies the limit and isolation, but PEBS ownership is
the address space (mm). Run as root or with CAP_SYS_ADMIN.

usage: enable_pebs_pid.py <pid> [mode]
       mode: -1 keep current, 0 static, 1 policy
"""
import ctypes
import os
import sys


def main():
    if len(sys.argv) not in (2, 3):
        raise SystemExit(f'usage: {sys.argv[0]} <pid> [mode]')
    try:
        pid = int(sys.argv[1])
        mode = int(sys.argv[2]) if len(sys.argv) == 3 else -1
    except ValueError as exc:
        raise SystemExit(f'invalid pid/mode: {exc}') from exc
    if pid <= 0:
        raise SystemExit('pid must be positive')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    if libc.syscall(ctypes.c_long(473), ctypes.c_long(pid),
                    ctypes.c_long(mode)) < 0:
        err = ctypes.get_errno()
        raise SystemExit(f'hermit_pebs_start({pid}, {mode}) failed: '
                         f'{os.strerror(err)}')


if __name__ == '__main__':
    main()
