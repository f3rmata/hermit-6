#!/usr/bin/env python3
"""Clean, focused re-analysis charts for the 2026-10-06 PEBS campaign.

Answers four questions with one figure each:
  fig1  PEBS on vs off, and policy gain  -> saturation throughput (QPS)
  fig2  pure sampling overhead           -> resident phase CPU & tail latency
  fig3  sampling frequency effect        -> RDMA read traffic vs samples/s (pressure)
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import rcParams
import os
import argparse

rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "WenQuanYi Micro Hei", "DejaVu Sans"]
rcParams["axes.unicode_minus"] = False

OUT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(OUT, "..", "results", "pebs", "20261006", "pebs-campaign-20261006")
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output-dir", default=os.path.abspath(OUT))
OUT = os.path.abspath(parser.parse_args().output_dir)
try:
    os.makedirs(OUT, exist_ok=True)
except OSError as e:
    raise SystemExit(f"无法创建输出目录 {OUT}: {e}") from e

# ---- shared colors ----
C_OFF0 = "#8a8a8a"   # off-original-mask（只允许 4 KiB）
C_OFF  = "#5b9bd5"   # off（允许全部页档）
C_STATIC = "#ed7d31" # PEBS on, 静态页档
C_POLICY = "#2e9e5b" # PEBS on, policy

# =====================================================================
# fig1 — saturation throughput (unlimited QPS)
# =====================================================================
cases = ["off-original\n(只允许 4 KiB)", "off\n(允许全部页档)",
         "static-medium\n(PEBS 开·静态)", "policy-medium\n(PEBS 开·策略)"]
qps   = [44492, 63424, 59489, 145338]
lo    = [42009, 60228, 52372, 137874]
hi    = [47739, 65582, 64388, 152345]
colors = [C_OFF0, C_OFF, C_STATIC, C_POLICY]

fig, ax = plt.subplots(figsize=(7.2, 4.6), dpi=110)
yerr = [[m - l for m, l in zip(qps, lo)], [h - m for m, h in zip(qps, hi)]]
bars = ax.bar(range(len(cases)), qps, yerr=yerr, capsize=5,
              color=colors, edgecolor="black", linewidth=0.6,
              error_kw=dict(elinewidth=1.2))
for i, (b, v) in enumerate(zip(bars, qps)):
    ax.text(b.get_x() + b.get_width()/2, v + 3500, f"{v:,}", ha="center", va="bottom", fontsize=10)
ax.set_xticks(range(len(cases)), cases, fontsize=9)
ax.set_ylabel("吞吐 QPS（中位数，误差线=min–max）", fontsize=10)
ax.set_title("不限速吞吐：PEBS 开/关 与 策略收益", fontsize=12)
ax.grid(axis="y", alpha=0.25)
ax.annotate("纯采样开销\nstatic vs off ≈ −6.2%",
            xy=(2, qps[2]), xytext=(2.32, 80000), fontsize=9, color="#7a3b12",
            arrowprops=dict(arrowstyle="->", color="#7a3b12", lw=1))
ax.annotate("策略收益\npolicy vs static ≈ +136%\npolicy vs off ≈ +124%",
            xy=(3, qps[3]), xytext=(1.9, 128000), fontsize=9, color="#1c5e36",
            arrowprops=dict(arrowstyle="->", color="#1c5e36", lw=1))
ax.set_ylim(0, 168000)
fig.tight_layout()
fig.savefig(os.path.join(OUT, "fig1-throughput.png"), bbox_inches="tight")
plt.close(fig)

# =====================================================================
# fig2 — pure sampling overhead (resident, fixed 30k QPS, no swap)
# =====================================================================
freq_labels = ["off", "low\n821/s", "medium\n868/s", "high\n1324/s"]
cpu  = [142.03, 143.07, 142.79, 143.18]
p99  = [23.30, 24.60, 24.80, 24.90]
cpu_delta = ["基线", "+0.73%", "+0.52%", "+0.79%"]

fig, (a1, a2) = plt.subplots(1, 2, figsize=(9.0, 4.0), dpi=110)
b1 = a1.bar(range(4), cpu, color=[C_OFF, C_STATIC, C_STATIC, C_STATIC],
            edgecolor="black", linewidth=0.6)
for i, (b, v, d) in enumerate(zip(b1, cpu, cpu_delta)):
    a1.text(b.get_x() + b.get_width()/2, v + 0.3, f"{v:.2f}\n({d})",
            ha="center", va="bottom", fontsize=8)
a1.set_xticks(range(4), freq_labels, fontsize=9)
a1.set_ylabel("整机忙碌 CPU（µs/请求）", fontsize=9)
a1.set_title("采样开销：CPU/请求", fontsize=10)
a1.set_ylim(140, 146)
a1.grid(axis="y", alpha=0.25)

b2 = a2.bar(range(4), p99, color=[C_OFF, C_STATIC, C_STATIC, C_STATIC],
            edgecolor="black", linewidth=0.6)
for b, v in zip(b2, p99):
    a2.text(b.get_x() + b.get_width()/2, v + 0.15, f"{v:.1f}", ha="center", va="bottom", fontsize=8)
a2.set_xticks(range(4), freq_labels, fontsize=9)
a2.set_ylabel("read p99（µs）", fontsize=9)
a2.set_title("采样开销：尾部延迟（绝对增量 1.3–1.6µs）", fontsize=10)
a2.set_ylim(22.5, 26.5)
a2.grid(axis="y", alpha=0.25)
fig.suptitle("纯采样开销（resident 阶段，固定 30000 QPS、无换页）", fontsize=12)
fig.tight_layout(rect=(0, 0, 1, 0.94))
fig.savefig(os.path.join(OUT, "fig2-overhead.png"), bbox_inches="tight")
plt.close(fig)

# =====================================================================
# fig3 — RDMA read traffic vs sampling rate (pressure, fixed 30k QPS)
# =====================================================================
# medians from pressure/summary.csv: (samples/s, rdma_read_KiB/op)
static = [(825.9, 48.27), (953.9, 55.94), (2166.4, 54.35), (13135.2, 50.89)]
policy = [(815.3, 42.01), (892.1, 7.93), (1485.2, 4.64), (12795.3, 4.66)]
tiers  = ["low", "medium", "high", "adaptive"]

fig, ax = plt.subplots(figsize=(7.8, 4.8), dpi=110)

# 不采样基线（samples/s=0 画在 log 轴上无意义，用水平虚线表示）
ax.axhline(56.26, color=C_OFF0, linestyle=":", linewidth=1.4, alpha=0.9)
ax.axhline(50.90, color=C_OFF, linestyle="--", linewidth=1.4, alpha=0.9)

sx = [p[0] for p in static]; sy = [p[1] for p in static]
px = [p[0] for p in policy]; py = [p[1] for p in policy]

ax.plot(sx, sy, "-o", color=C_STATIC, linewidth=1.6, markersize=6,
        label="static（PEBS 开 · 静态页档）", zorder=3)
ax.plot(px, py, "-o", color=C_POLICY, linewidth=2.2, markersize=7,
        label="policy（PEBS 开 · 策略）", zorder=4)

for x, y, t in zip(px, py, tiers):
    ax.annotate(t, (x, y), textcoords="offset points", xytext=(11, 9),
                fontsize=9, color="#1c5e36", fontweight="bold")

ax.text(1500, 51.6, "off（不采样 · 允许全部页档）", color="#2a5ca8", fontsize=8, va="bottom")
ax.text(1500, 57.0, "off-original-mask（只允许 4 KiB）", color="#555555", fontsize=8, va="bottom")

ax.set_xscale("log")
ax.set_xlabel("实际采样率 samples/s（对数刻度）", fontsize=10)
ax.set_ylabel("RDMA 读流量（KiB/请求）", fontsize=10)
ax.set_title("采样率与 RDMA 读流量（pressure，固定 30000 QPS）", fontsize=12)
ax.set_xlim(600, 30000)
ax.set_ylim(0, 62)
ax.grid(which="both", alpha=0.22)
ax.legend(fontsize=9, loc="upper right")
fig.tight_layout()
fig.savefig(os.path.join(OUT, "fig3-frequency.png"), bbox_inches="tight")
plt.close(fig)

print("charts written to", OUT)
for f in ["fig1-throughput.png", "fig2-overhead.png", "fig3-frequency.png"]:
    p = os.path.join(OUT, f)
    print(" ", p, os.path.getsize(p), "bytes")
