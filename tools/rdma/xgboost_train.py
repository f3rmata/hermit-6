#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Signal-driven XGBoost training harness for Hermit large-folio swap tests.

Protocol
--------
The process blocks SIGUSR1/SIGUSR2/SIGTERM/SIGINT on startup and then:

  1. prints  WAITING pid=<pid>
  2. on SIGUSR1: builds the training matrix (synthetic or from a file),
     prints READY rows=<n> features=<m> populate_sec=<sec>
  3. on SIGUSR2: trains with deterministic parameters,
     prints TRAIN sec=<sec> metric=<name> value=<value>
  4. on SIGTERM/SIGINT: exits

Environment
-----------
XGB_DATA_FILE        optional CSV or libsvm training file; when empty a
                     synthetic binary-classification matrix is generated.
XGB_DATA_FORMAT      csv or libsvm; by default inferred from the file
                     extension (defaults to libsvm for unknown suffixes).
XGB_WORKSET_MB       synthetic data size in MiB (default 16384). Used
                     only to derive the row count when XGB_DATA_FILE is
                     empty; the actual resident set is read by the sweep
                     script from the cgroup.
XGB_FEATURES         feature count for the synthetic matrix (default 28).
XGB_ROUNDS           number of boosting rounds (default 30).
XGB_MAX_DEPTH        tree depth (default 8).
XGB_NTHREAD          number of XGBoost worker threads (default 4).
XGB_TREE_METHOD      XGBoost tree method (default hist).
XGB_OBJECTIVE        training objective (default binary:logistic).
XGB_EVAL_METRIC      metric reported in TRAIN (default auc).
XGB_SEED             deterministic seed (default 42).
"""

import ctypes
import os
import signal
import sys
import time


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


def madvise_hugepage(arr):
    """Best-effort MADV_HUGEPAGE on a numpy array."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        MADV_HUGEPAGE = 14  # x86-64 Linux
        ptr = arr.ctypes.data
        size = arr.nbytes
        if libc.madvise(ctypes.c_void_p(ptr), ctypes.c_size_t(size),
                        ctypes.c_int(MADV_HUGEPAGE)) != 0:
            errno = ctypes.get_errno()
            print("madvise(MADV_HUGEPAGE) failed: errno=%d size=%d" %
                  (errno, size), file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - best-effort hint only
        print("madvise(MADV_HUGEPAGE) exception: %s" % exc, file=sys.stderr)


def drop_file_cache(path):
    """Best-effort eviction of clean page cache for a data file.

    After DMatrix parses a CSV/libsvm file the file's clean page cache is no
    longer needed.  Dropping it here keeps the cgroup memory.current close to
    the anonymous DMatrix footprint so the sweep script's memory.max
    calculation targets swap-out instead of clean page-cache reclaim.
    """
    try:
        posix_fadvise = getattr(os, "posix_fadvise", None)
        dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
        if posix_fadvise is None or dontneed is None:
            return
        fd = os.open(path, os.O_RDONLY)
        try:
            posix_fadvise(fd, 0, 0, dontneed)
        finally:
            os.close(fd)
    except OSError:
        pass


def build_synthetic(rows, features):
    """Create a deterministic, contiguous, single-array training set."""
    import numpy as np

    rng = np.random.RandomState(42)
    x = np.empty((rows, features), dtype=np.float32)
    # Column-wise fill keeps the temporary memory low while x stays one
    # contiguous allocation.  Every 4 KiB page of x is written at least once.
    for col in range(features):
        x[:, col] = rng.randn(rows).astype(np.float32)
    y = ((x[:, 0] + x[:, 1] + x[:, 2]) > 0).astype(np.int32)
    madvise_hugepage(x)
    madvise_hugepage(y)
    return x, y


def build_dmatrix():
    import xgboost as xgb

    data_file = os.environ.get("XGB_DATA_FILE", "").strip()
    start = time.monotonic()
    if data_file:
        if not os.path.exists(data_file):
            log("XGB_DATA_FILE does not exist: %s" % data_file)
            sys.exit(2)
        fmt = os.environ.get("XGB_DATA_FORMAT", "").strip().lower()
        if not fmt:
            lower = data_file.lower()
            if lower.endswith(".csv"):
                fmt = "csv"
            elif lower.endswith(".libsvm") or lower.endswith(".txt"):
                fmt = "libsvm"
            else:
                fmt = "libsvm"
        uri = data_file.split("?")[0]
        if fmt == "csv":
            uri += "?format=csv&label_column=0"
        else:
            uri += "?format=libsvm"
        dtrain = xgb.DMatrix(uri)
        drop_file_cache(data_file)
        rows = dtrain.num_row()
        features = dtrain.num_col()
    else:
        workset_mb = env_int("XGB_WORKSET_MB", 16384)
        features = env_int("XGB_FEATURES", 28)
        bytes_per_row = features * 4
        rows = max(1, (workset_mb * 1024 * 1024) // bytes_per_row)
        x, y = build_synthetic(rows, features)
        dtrain = xgb.DMatrix(x, label=y)
        rows = dtrain.num_row()
        features = dtrain.num_col()
    populate_sec = time.monotonic() - start
    log("READY pid=%d rows=%d features=%d populate_sec=%.6f" %
        (os.getpid(), rows, features, populate_sec))
    return dtrain


def train_dmatrix(dtrain):
    import xgboost as xgb

    params = {
        "objective": os.environ.get("XGB_OBJECTIVE", "binary:logistic"),
        "eval_metric": os.environ.get("XGB_EVAL_METRIC", "auc"),
        "tree_method": os.environ.get("XGB_TREE_METHOD", "hist"),
        "max_depth": env_int("XGB_MAX_DEPTH", 8),
        "eta": env_float("XGB_ETA", 0.3),
        "subsample": env_float("XGB_SUBSAMPLE", 1.0),
        "colsample_bytree": env_float("XGB_COLSAMPLE_BYTREE", 1.0),
        "min_child_weight": env_float("XGB_MIN_CHILD_WEIGHT", 1.0),
        "seed": env_int("XGB_SEED", 42),
        "nthread": env_int("XGB_NTHREAD", 4),
    }
    rounds = env_int("XGB_ROUNDS", 30)
    evals_result = {}
    start = time.monotonic()
    xgb.train(params, dtrain, num_boost_round=rounds,
              evals=[(dtrain, "train")], evals_result=evals_result)
    train_sec = time.monotonic() - start

    metric_name = "unknown"
    metric_value = -1.0
    try:
        per_iter = evals_result.get("train", {})
        if per_iter:
            metric_name = list(per_iter.keys())[0]
            values = per_iter[metric_name]
            if values:
                metric_value = float(values[-1])
    except Exception:  # noqa: BLE001 - metric is diagnostic only
        pass

    log("TRAIN sec=%.6f metric=%s value=%.6f" %
        (train_sec, metric_name, metric_value))
    return train_sec, metric_name, metric_value


def main():
    data_file = os.environ.get("XGB_DATA_FILE", "").strip()
    if not data_file:
        # Validate before blocking so a bad configuration fails fast.
        env_int("XGB_WORKSET_MB", 16384)
        env_int("XGB_FEATURES", 28)

    sigs = {signal.SIGUSR1, signal.SIGUSR2, signal.SIGTERM, signal.SIGINT}
    signal.pthread_sigmask(signal.SIG_BLOCK, sigs)

    log("WAITING pid=%d" % os.getpid())

    dtrain = None
    while True:
        sig = signal.sigwait(sigs)
        if sig in (signal.SIGTERM, signal.SIGINT):
            break
        if sig == signal.SIGUSR1:
            dtrain = build_dmatrix()
        elif sig == signal.SIGUSR2:
            if dtrain is None:
                log("TRAIN skipped: no DMatrix built; send SIGUSR1 first")
                continue
            train_dmatrix(dtrain)
            dtrain = None

    log("EXIT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
