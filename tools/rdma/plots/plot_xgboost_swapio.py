#!/usr/bin/env python3
"""Plot XGBoost Hermit large-folio swap-out/swap-in results."""

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt


def median_float(group, key):
    return statistics.median(float(r[key]) for r in group)


def median_int(group, key):
    return statistics.median(int(r[key]) for r in group)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--title-prefix", default="XGBoost (HIGGS 11M, 30 rounds)")
    args = parser.parse_args()

    with args.csv.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("no benchmark rows")

    groups = defaultdict(list)
    for row in rows:
        groups[int(row["page_kb"])].append(row)

    pages = sorted(groups)
    labels = [f"{p}k" if p < 1024 else f"{p // 1024}M" for p in pages]
    store_bw = [median_float(groups[p], "protocol_gib_per_sec") for p in pages]
    load_bw = [median_float(groups[p], "load_protocol_gib_per_sec") for p in pages]
    store_pct = [median_float(groups[p], "large_store_pct") for p in pages]
    load_pct = [median_float(groups[p], "large_load_pct") for p in pages]
    store_count = [median_int(groups[p], "target_stores_delta") for p in pages]
    load_count = [median_int(groups[p], "target_loads_delta") for p in pages]
    train_sec = [median_float(groups[p], "train_sec") for p in pages]
    auc = sorted({float(r["train_metric_value"]) for r in rows})

    fig, axes = plt.subplots(2, 2, figsize=(12, 9), constrained_layout=True)

    # Top-left: store protocol throughput.
    ax = axes[0][0]
    bars = ax.bar(labels, store_bw, color="tab:blue", alpha=0.8)
    ax.bar_label(bars, fmt="%.3g", padding=2, fontsize=8)
    ax.set_ylabel("Store protocol throughput (GiB/s)")
    ax.set_title(f"{args.title_prefix}\nSwap-out protocol throughput")
    ax.grid(axis="y", alpha=0.3)

    # Top-right: large-transfer share.
    ax = axes[0][1]
    ax.plot(labels, store_pct, "o-", linewidth=2, color="tab:green",
            label="Store")
    ax.plot(labels, load_pct, "s--", linewidth=2, color="tab:orange",
            label="Load")
    ax.set_ylim(-3, 103)
    ax.set_ylabel("Large-transfer byte share (%)")
    ax.set_xlabel("Requested folio size")
    ax.set_title("Large RDMA transfer share")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()

    # Bottom-left: folio request counts (log scale).
    ax = axes[1][0]
    ax.plot(labels, store_count, "o-", linewidth=2, color="tab:red",
            label="Store requests")
    ax.plot(labels, load_count, "s--", linewidth=2, color="tab:purple",
            label="Load requests")
    ax.set_yscale("log")
    ax.set_ylabel("Request count (log)")
    ax.set_xlabel("Requested folio size")
    ax.set_title("RDMA store/load request counts")
    ax.grid(axis="y", alpha=0.3, which="both")
    ax.legend()

    # Bottom-right: training time.
    ax = axes[1][1]
    ax.plot(labels, train_sec, "o-", linewidth=2, color="tab:brown")
    ax.set_ylabel("Training time (s)")
    ax.set_xlabel("Requested folio size")
    ax.set_title("XGBoost training time")
    ax.grid(axis="y", alpha=0.3)
    for p, sec in zip(pages, train_sec):
        ax.annotate(f"{sec:.1f}s", (labels[pages.index(p)], sec),
                    textcoords="offset points", xytext=(0, 7), ha="center",
                    fontsize=8)

    # Add load throughput as text annotation because it is orders of
    # magnitude smaller than the store throughput and is mostly compute-bound.
    load_text = ", ".join(
        f"{p}k={bw:.4f}" if p < 1024 else f"{p // 1024}M={bw:.4f}"
        for p, bw in zip(pages, load_bw))
    fig.text(0.5, 0.015,
             f"Median AUC={auc[0]:.6f}  |  Load protocol throughput (GiB/s): "
             f"{load_text}",
             ha="center", fontsize=8, color="dimgray")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180)
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()
