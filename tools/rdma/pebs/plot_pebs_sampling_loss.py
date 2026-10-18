#!/usr/bin/env python3
"""PEBS 采样性能损失图（static vs off-same-pages，同页档）。

数据源：20261011 满载复测
- resident：tools/rdma/results/pebs/20261011-150846-memcached-cost-8cores/resident/measurement/summary.csv
- pressure：tools/rdma/results/pebs/20261011-185459-pressure-fixed-load/pressure/measurement/summary.csv

吞吐损失 = 100 × (1 − static QPS / 同重复 off-same-pages QPS)，正值 = 变慢。
"""
import csv
import os
import statistics as st
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams
import numpy as np

rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "WenQuanYi Micro Hei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

ROOT = os.path.dirname(os.path.abspath(__file__))
RESIDENT = os.path.join(ROOT, "..", "results", "pebs",
                        "20261011-150846-memcached-cost-8cores",
                        "resident", "measurement", "summary.csv")
PRESSURE = os.path.join(ROOT, "..", "results", "pebs",
                        "20261011-185459-pressure-fixed-load",
                        "pressure", "measurement", "summary.csv")

def paired_loss(path, case):
    """static case 相对 off-same-pages 的配对吞吐损失 %（列表）"""
    try:
        with open(path) as fh:
            rows = list(csv.DictReader(fh))
    except OSError as e:
        raise SystemExit(f"无法读取 {path}: {e}") from e
    offsp = {r["repeat"]: float(r["qps"]) for r in rows if r["case"] == "off-same-pages"}
    st_ = {r["repeat"]: float(r["qps"]) for r in rows if r["case"] == case}
    return [(1 - st_[k] / offsp[k]) * 100 for k in offsp if k in st_]

def stat(losses):
    return st.median(losses), min(losses), max(losses)

freq = ["low", "medium", "high"]
res = [stat(paired_loss(RESIDENT, f"static-{f}")) for f in freq]
prs = [stat(paired_loss(PRESSURE, f"static-{f}")) for f in freq]

x = np.arange(len(freq))
w = 0.36
fig, ax = plt.subplots(figsize=(7.6, 4.6), dpi=110)

def bars(vals, offset, color, label):
    meds = [v[0] for v in vals]
    lo = [v[0] - v[1] for v in vals]
    hi = [v[2] - v[0] for v in vals]
    ax.bar(x + offset, meds, w, yerr=[lo, hi], capsize=4,
           color=color, edgecolor="black", linewidth=0.6, label=label,
           error_kw=dict(elinewidth=1.0, alpha=0.6))

bars(res, -w / 2, "#4f81bd", "resident（无换页，饱和 ~84 万 QPS）")
bars(prs, +w / 2, "#ed7d31", "pressure（换页，饱和 ~9.8 万 QPS）")

for i, v in enumerate(res):
    ax.text(i - w / 2, v[0] + 0.12, f"{v[0]:+.2f}%", ha="center", va="bottom", fontsize=8)
for i, v in enumerate(prs):
    ax.text(i + w / 2, v[0] + 0.12, f"{v[0]:+.2f}%", ha="center", va="bottom", fontsize=8)

ax.axhline(0, color="black", linewidth=0.8)
ax.set_xticks(x, [f"low\n~2.5k/s", f"medium\n~2.8k/s", f"high\n~4.7k/s"], fontsize=9)
ax.set_ylabel("吞吐损失 %（static vs off-same-pages，正值=变慢）", fontsize=10)
ax.set_title("PEBS 采样性能损失：不同采样率（满载饱和）", fontsize=12)
ax.set_ylim(-10, 16)
ax.legend(fontsize=8, loc="upper left")
ax.grid(axis="y", alpha=0.25)
ax.annotate("满载采样开销 ~0.4–0.7%，\n基本不随采样率变化；\npressure 散布大（含换页/发压波动）",
            xy=(0.02, 0.62), xycoords="axes fraction", fontsize=8,
            bbox=dict(boxstyle="round,pad=0.4", fc="#fffbe6", ec="#cccccc"))
fig.tight_layout()
out = os.path.join(os.path.dirname(RESIDENT), "fig-sampling-loss.png")
fig.savefig(out, bbox_inches="tight")
plt.close(fig)

print("chart written to", out, os.path.getsize(out), "bytes")
