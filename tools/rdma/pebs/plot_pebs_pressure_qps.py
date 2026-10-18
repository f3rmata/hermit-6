#!/usr/bin/env python3
"""pressure（换页）满载吞吐柱状图：所有配置的绝对 QPS（中位数，误差线=min–max）。

数据源：tools/rdma/results/pebs/20261011-185459-pressure-fixed-load/pressure/measurement/summary.csv
"""
import csv
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
PRESSURE = os.path.join(ROOT, "..", "results", "pebs",
                       "20261011-185459-pressure-fixed-load",
                       "pressure", "measurement", "summary.csv")
PRESSURE = os.path.abspath(PRESSURE)

try:
    with open(PRESSURE) as fh:
        rows = list(csv.DictReader(fh))
except OSError as e:
    raise SystemExit(f"无法读取 {PRESSURE}: {e}") from e

by_case = defaultdict(list)
for r in rows:
    by_case[r["case"]].append(r)

C_OFF  = "#8e44ad"   # 紫 = off（纯 4 KiB 基线）
C_SAME = "#5b9bd5"   # 蓝 = off-same-pages（64 KiB 基线）
C_STATIC = "#edb32c" # 黄 = static（采样·静态）
C_POLICY = "#2e9e5b" # 绿 = policy（采样·策略）

def median_sps(case):
    if case not in by_case:
        return 0.0
    return st.median([float(r["samples_per_second"]) for r in by_case[case]])

configs = [
    ("off", C_OFF),
    ("off-same-pages", C_SAME),
    ("static-low", C_STATIC),
    ("static-medium", C_STATIC),
    ("static-high", C_STATIC),
    ("policy-low", C_POLICY),
    ("policy-medium", C_POLICY),
    ("policy-high", C_POLICY),
]

def xlabel(case):
    if case == "off":
        return "off\n(纯 4 KiB)"
    if case == "off-same-pages":
        return "off-same-pages\n(64 KiB)"
    mode, tier = case.split("-")
    sps = median_sps(case)
    return f"{mode}\n{tier} ~{sps/1000:.1f}k/s"

labels = [xlabel(c[0]) for c in configs]

meds, lows, highs, colors = [], [], [], []
for case, color in configs:
    qps = [float(r["qps"]) for r in by_case[case]]
    meds.append(st.median(qps))
    lows.append(st.median(qps) - min(qps))
    highs.append(max(qps) - st.median(qps))
    colors.append(color)

fig, ax = plt.subplots(figsize=(9.5, 5.0), dpi=110)
x = list(range(len(configs)))
bars = ax.bar(x, meds, 0.62, yerr=[lows, highs], capsize=4,
              color=colors, edgecolor="black", linewidth=0.6,
              error_kw=dict(elinewidth=1.0, alpha=0.7))
for b, v in zip(bars, meds):
    ax.text(b.get_x() + b.get_width() / 2, v + 6000, f"{v:,.0f}",
            ha="center", va="bottom", fontsize=9)

ax.set_xticks(x, labels, fontsize=8)
ax.set_ylabel("吞吐 QPS（中位数，误差线=min–max）", fontsize=10)
ax.set_title("pressure（换页）满载吞吐对比：所有配置", fontsize=12)
ax.set_ylim(0, 300000)
ax.grid(axis="y", alpha=0.25)

from matplotlib.patches import Patch
ax.legend(handles=[
    Patch(color=C_OFF, label="紫 = off（纯 4 KiB 基线）"),
    Patch(color=C_SAME, label="蓝 = off-same-pages（64 KiB 基线）"),
    Patch(color=C_STATIC, label="黄 = static（采样开 · 静态全页档）"),
    Patch(color=C_POLICY, label="绿 = policy（采样开 · 策略）"),
], fontsize=8, loc="upper left", framealpha=0.9)

fig.tight_layout()
out = os.path.join(os.path.dirname(PRESSURE), "fig-pressure-qps.png")
fig.savefig(out, bbox_inches="tight")
plt.close(fig)

print("chart written to", out, os.path.getsize(out), "bytes")
