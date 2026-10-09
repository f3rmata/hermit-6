#!/usr/bin/env python3
"""吞吐对比图 for the 2026-10-07 full-workload PEBS sweep.

Reads every per-case raw summary CSV under
results/pebs/20261007-233523-workloads/<case>/results/ and produces two charts:
  fig4  normalized throughput (off=1.0) per workload x config  (grouped bars)
  fig5  absolute throughput vs page size per workload          (grid, off/static-199/policy-199)
"""
import csv
import glob
import os
import statistics as st
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams

rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "WenQuanYi Micro Hei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "..", "results", "pebs", "20261007-233523-workloads")
BASE = os.path.abspath(BASE)

SUITES = [
    "anon-1t-full", "anon-1t-chunk64k", "anon-8t-full", "anon-8t-chunk64k",
    "redis", "ycsb", "xgboost",
]
CASES = ["off", "static-19997", "static-1999", "static-199",
         "policy-19997", "policy-1999", "policy-199"]

# samples.csv maps (suite,page_kb,case,repeat) -> source dir
try:
    with open(os.path.join(BASE, "analysis", "samples.csv")) as fh:
        samples = list(csv.DictReader(fh))
except OSError as e:
    raise SystemExit(f"无法读取 samples.csv: {e}") from e

# raw csv name per suite
def raw_csv_name(suite):
    if suite.startswith("anon"):
        return "swapio-summary.csv"
    return f"{suite}-swapio-summary.csv"

def throughput(suite, row):
    if suite.startswith("anon"):
        return float(row["accessed_scan_gib_per_sec"])
    if suite == "redis":
        return float(row["get_qps"])
    if suite == "ycsb":
        return float(row["throughput_ops"])
    if suite == "xgboost":
        return 1.0 / float(row["train_sec"])
    raise ValueError(suite)

# (suite, page_kb, case) -> [throughput over 3 repeats]
per_case = defaultdict(list)
for s in samples:
    suite, page, case, repeat = s["suite"], s["page_kb"], s["case"], s["repeat"]
    src = s["source"]
    raw = os.path.join(BASE, src, "results", raw_csv_name(suite))
    if not os.path.exists(raw):
        continue
    try:
        with open(raw) as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        continue
    if not rows:
        continue
    per_case[(suite, int(page), case)].append(throughput(suite, rows[0]))

# median over repeats
tp = {k: st.median(v) for k, v in per_case.items()}

PAGES = sorted({k[1] for k in tp})

# ---- fig4: normalized throughput (off=1.0) per workload x config ----
C_OFF = "#8a8a8a"
C_STATIC = ["#f6c98a", "#ed9a4e", "#d96a1f"]   # low/med/high (19997/1999/199)
C_POLICY = ["#9fd6a8", "#4fb06a", "#1c7a3e"]   # low/med/high

def workload_norm(suite):
    off = tp[(suite, 0, "off")] if (suite, 0, "off") in tp else None
    # off median across pages
    offs = [tp[(suite, p, "off")] for p in PAGES if (suite, p, "off") in tp]
    return st.median(offs)

fig, ax = plt.subplots(figsize=(11, 4.6), dpi=110)
xw = list(range(len(SUITES)))
labels = ["off"] + [f"static-{p}" for p in ["19997", "1999", "199"]] + \
         [f"policy-{p}" for p in ["19997", "1999", "199"]]
colors = [C_OFF] + C_STATIC + C_POLICY
n = len(labels)
width = 0.8 / n
for i, (label, color) in enumerate(zip(labels, colors)):
    vals, xs = [], []
    for j, suite in enumerate(SUITES):
        off = workload_norm(suite)
        if off is None:
            continue
        # median across pages of this config's throughput
        cvals = [tp[(suite, p, label)] for p in PAGES if (suite, p, label) in tp]
        if not cvals:
            continue
        vals.append(st.median(cvals) / off)
        xs.append(xw[j] + (i - (n - 1) / 2) * width)
    ax.bar(xs, vals, width, color=color, edgecolor="black", linewidth=0.3, label=label)

ax.axhline(1.0, color="black", linewidth=0.8)
ax.set_xticks(xw, SUITES, fontsize=9, rotation=20, ha="right")
ax.set_ylabel("吞吐（相对 off，off=1.0）", fontsize=10)
ax.set_title("逐负载吞吐对比：off / static / policy（各页档中位数归一化）", fontsize=12)
ax.set_ylim(0.90, 1.10)
ax.legend(ncol=7, fontsize=7, loc="lower center", bbox_to_anchor=(0.5, -0.28))
ax.grid(axis="y", alpha=0.25)
fig.tight_layout()
fig.savefig(os.path.join(BASE, "analysis", "fig4-throughput-normalized.png"), bbox_inches="tight")
plt.close(fig)

# ---- fig5: absolute throughput vs page size (grid, off/static-199/policy-199) ----
fig, axes = plt.subplots(2, 4, figsize=(13, 6.4), dpi=110)
axes = axes.ravel()
unit = {
    "anon-1t-full": "GiB/s", "anon-1t-chunk64k": "GiB/s",
    "anon-8t-full": "GiB/s", "anon-8t-chunk64k": "GiB/s",
    "redis": "GET/s", "ycsb": "ops/s", "xgboost": "1/s（训练吞吐）",
}
curves = ["off", "static-199", "policy-199"]
ccolors = [C_OFF, "#d96a1f", "#1c7a3e"]
for idx, suite in enumerate(SUITES):
    ax = axes[idx]
    for case, color in zip(curves, ccolors):
        xs, ys = [], []
        for p in PAGES:
            if (suite, p, case) in tp:
                xs.append(p)
                ys.append(tp[(suite, p, case)])
        ax.plot(xs, ys, "-o", color=color, linewidth=1.5, markersize=3.5, label=case)
    ax.set_title(suite, fontsize=9)
    ax.set_xscale("log", base=2)
    ax.set_xticks(PAGES, [str(p) for p in PAGES], fontsize=6, rotation=45)
    ax.set_ylabel(unit[suite], fontsize=7)
    ax.grid(which="both", alpha=0.2)
    ax.tick_params(axis="y", labelsize=6)
# legend panel
axes[7].axis("off")
axes[7].legend(*axes[0].get_legend_handles_labels(), loc="center", fontsize=9, title="配置")
fig.suptitle("逐负载吞吐 vs 页档（off / static-199 / policy-199，三次重复中位数）", fontsize=12)
fig.tight_layout(rect=(0, 0, 1, 0.95))
fig.savefig(os.path.join(BASE, "analysis", "fig5-throughput-pagesize.png"), bbox_inches="tight")
plt.close(fig)

print("charts written to", os.path.join(BASE, "analysis"))
for f in ["fig4-throughput-normalized.png", "fig5-throughput-pagesize.png"]:
    p = os.path.join(BASE, "analysis", f)
    print(" ", p, os.path.getsize(p), "bytes")
