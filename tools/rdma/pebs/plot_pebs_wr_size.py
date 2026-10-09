#!/usr/bin/env python3
"""传输页大小对比图：不同策略下大 WR（≥64 KiB）vs 4 KiB 基页的读字节占比。

数据源：memcached pressure 阶段（固定 30000 QPS，含换页）summary.csv 的
large_wr_read_pct。大 WR = folio order>0（64 KiB 及以上），其余为 4 KiB 基页 WR。

static 四档（low/medium/high/adaptive）对页大小不敏感，合并为一条柱。
"""
import csv
import os
import statistics as st
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams
from matplotlib.patches import Patch

rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "WenQuanYi Micro Hei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.join(ROOT, "..", "results", "pebs", "20261006", "pebs-campaign-20261006")
BASE = os.path.abspath(BASE)

try:
    with open(os.path.join(BASE, "pressure", "summary.csv")) as fh:
        rows = list(csv.DictReader(fh))
except OSError as e:
    raise SystemExit(f"无法读取 pressure/summary.csv: {e}") from e

by_case = defaultdict(list)
for r in rows:
    by_case[r["case"]].append(r)

def med_large(case):
    return st.median([float(r["large_wr_read_pct"]) for r in by_case[case]])

# static 四档合并为一条柱（对页大小不敏感，取全体中位数）
static_vals = [float(r["large_wr_read_pct"])
               for c in by_case if c.startswith("static-")
               for r in by_case[c]]
static_med = st.median(static_vals)

C_GRAY = "#d9d9d9"   # 4 KiB 基页 WR（底部）
C_OFF  = "#5b9bd5"   # 蓝 = off
C_STATIC = "#edb32c" # 黄 = static
C_POLICY = "#2e9e5b" # 绿 = policy

# (case, x 标签, 大 WR 色, 大 WR 占比)
configs = [
    ("off-original-mask", "off-original\n(只允许 4 KiB)", "#8a8a8a", med_large("off-original-mask")),
    ("off", "off\n(全页档)", C_OFF, med_large("off")),
    ("static", "static\n(静态全页档)", C_STATIC, static_med),
    ("policy-low", "policy\nlow", C_POLICY, med_large("policy-low")),
    ("policy-medium", "policy\nmedium", C_POLICY, med_large("policy-medium")),
    ("policy-high", "policy\nhigh", C_POLICY, med_large("policy-high")),
    ("policy-adaptive", "policy\nadaptive", C_POLICY, med_large("policy-adaptive")),
]

fig, ax = plt.subplots(figsize=(9.2, 4.8), dpi=110)
x = list(range(len(configs)))
large = [c[3] for c in configs]
colors = [c[2] for c in configs]

# 底部浅灰 = 4 KiB 基页；上部彩色 = 大 WR（≥64 KiB folio）
ax.bar(x, [100 - v for v in large], 0.62, color=C_GRAY,
       edgecolor="black", linewidth=0.5)
ax.bar(x, large, 0.62, bottom=[100 - v for v in large],
       color=colors, edgecolor="black", linewidth=0.5)

for i, v in enumerate(large):
    if v > 6:
        ax.text(i, v / 2, f"{v:.0f}%", ha="center", va="center", fontsize=8, color="white")
    ax.text(i, (100 + max(v, 0)) / 2 + 2, f"{100 - v:.0f}%", ha="center", va="bottom",
            fontsize=7, color="#333333")

ax.set_xticks(x, [c[1] for c in configs], fontsize=8)
ax.set_ylabel("读字节占比（%）", fontsize=10)
ax.set_title("不同策略下的传输页大小对比（pressure，固定 30000 QPS，读字节）", fontsize=12)
ax.set_ylim(0, 112)

handles = [
    Patch(color=C_GRAY, label="浅灰 = 4 KiB 基页 WR（柱子底部）"),
    Patch(color=C_OFF, label="蓝 = 大页（≥64 KiB）· off（采样关 · 全页档）"),
    Patch(color=C_STATIC, label="黄 = 大页（≥64 KiB）· static（采样开 · 静态全页档）"),
    Patch(color=C_POLICY, label="绿 = 大页（≥64 KiB）· policy（采样开 · 策略）"),
]
ax.legend(handles=handles, fontsize=8, loc="upper left", framealpha=0.9)
ax.grid(axis="y", alpha=0.2)
fig.tight_layout()
out = os.path.join(BASE, "fig-wr-size.png")
fig.savefig(out, bbox_inches="tight")
plt.close(fig)

print("chart written to", out, os.path.getsize(out), "bytes")
