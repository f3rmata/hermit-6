#!/usr/bin/env python3
"""Plot the dnet-61 memcached page sweep and mTHP swap-in fix results."""

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt


PAGES = [4, 16, 32, 64, 128, 256, 512, 1024, 2048]
LABELS = ["4 KiB", "16 KiB", "32 KiB", "64 KiB", "128 KiB",
          "256 KiB", "512 KiB", "1 MiB", "2 MiB"]
COLORS = {"local": "#4c78a8", "cgroup-local": "#e45756",
          "cgroup-Hermit": "#59a14f"}


def rows(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def load_memcached(native, hermit):
    result = defaultdict(list)
    for row in rows(native):
        name = "local" if row["mode"] == "local" else "cgroup-local"
        result[(int(row["page_kb"]), name)].append(row)
    for row in rows(hermit):
        result[(int(row["page_kb"]), "cgroup-Hermit")].append(row)
    return result


def save(fig, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_throughput(data, output):
    fig, ax = plt.subplots(figsize=(11, 6.5))
    for mode in ("local", "cgroup-local", "cgroup-Hermit"):
        values = []
        for page in PAGES:
            peak = max(data[(page, mode)], key=lambda r: float(r["achieved_qps"]))
            values.append(float(peak["achieved_qps"]) / 1000)
        ax.plot(LABELS, values, "o-", linewidth=2.3, markersize=7,
                color=COLORS[mode], label=mode)
    ax.set_yscale("log")
    ax.set_ylabel("Peak achieved throughput (KQPS, log scale)")
    ax.set_xlabel("Requested page size")
    ax.set_title("dnet-61 memcached peak throughput after the swap-in fix")
    ax.grid(axis="y", which="both", alpha=0.3)
    ax.legend()
    save(fig, output)


def plot_latency(data, output):
    fig, ax = plt.subplots(figsize=(11, 6.5))
    for mode in ("local", "cgroup-local", "cgroup-Hermit"):
        values = []
        for page in PAGES:
            point = min(data[(page, mode)],
                        key=lambda r: abs(float(r["offered_qps"]) - 500000))
            values.append(float(point["read_p99_us"]))
        ax.plot(LABELS, values, "o-", linewidth=2.3, markersize=7,
                color=COLORS[mode], label=mode)
    ax.set_yscale("log")
    ax.set_ylabel("Read p99 latency (us, log scale)")
    ax.set_xlabel("Requested page size")
    ax.set_title("dnet-61 memcached read p99 at 500 KQPS offered load")
    ax.grid(axis="y", which="both", alpha=0.3)
    ax.legend()
    save(fig, output)


def plot_load_share(share_csv, output):
    by_page = {int(row["page_kb"]): float(row["high_order_byte_share_pct"])
               for row in rows(share_csv)}
    values = [by_page[page] for page in PAGES]
    fig, ax = plt.subplots(figsize=(11, 6.5))
    bars = ax.bar(LABELS, values, color="#59a14f")
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 1.5,
                f"{value:.1f}%", ha="center", va="bottom", fontsize=9)
    ax.set_ylim(0, 108)
    ax.set_ylabel("Target-order RDMA load byte share (%)")
    ax.set_xlabel("Requested page size")
    ax.set_title("Actual higher-order swap-in share during memcached")
    ax.grid(axis="y", alpha=0.3)
    save(fig, output)


def plot_micro(before_csv, after_csv, output):
    before = {int(row["page_kb"]): float(row["swapin_seconds"])
              for row in rows(before_csv)}
    after = {int(row["page_kb"]): float(row["swapin_seconds"])
             for row in rows(after_csv)}
    pages = sorted(set(before) & set(after))
    labels = [f"{page} KiB" if page < 1024 else "1 MiB" for page in pages]
    x = list(range(len(pages)))
    width = 0.38
    fig, ax = plt.subplots(figsize=(10.5, 6.5))
    ax.bar([value - width / 2 for value in x], [before[p] for p in pages],
           width, color="#e45756", label="Before fix (4 KiB loads)")
    ax.bar([value + width / 2 for value in x], [after[p] for p in pages],
           width, color="#59a14f", label="After fix (target-order loads)")
    for index, page in enumerate(pages):
        speedup = before[page] / after[page]
        ax.text(index, max(before[page], after[page]) + 0.025,
                f"{speedup:.2f}x", ha="center", fontsize=9)
    ax.set_xticks(x, labels)
    ax.set_ylim(0, max(before.values()) * 1.18)
    ax.set_ylabel("512 MiB sequential swap-in time (s)")
    ax.set_xlabel("mTHP size")
    ax.set_title("Higher-order swap-in fix: sequential anonymous-memory test")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()
    save(fig, output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--hermit", type=Path, required=True)
    parser.add_argument("--load-share", type=Path, required=True)
    parser.add_argument("--micro-before", type=Path, required=True)
    parser.add_argument("--micro-after", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    data = load_memcached(args.native, args.hermit)
    plot_throughput(data, args.output_dir / "memcached-peak-throughput.png")
    plot_latency(data, args.output_dir / "memcached-read-p99-500k.png")
    plot_load_share(args.load_share, args.output_dir / "memcached-high-order-load-share.png")
    plot_micro(args.micro_before, args.micro_after,
               args.output_dir / "mthp-swapin-before-after.png")


if __name__ == "__main__":
    main()
