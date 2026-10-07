#!/usr/bin/env python3
"""Compare 1-thread and 8-thread anon sparse swap results."""

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt


def page_label(page):
    return f"{page}k" if page < 1024 else f"{page // 1024}M"


def median(groups, ratio, page, field):
    group = groups.get((ratio, page), [])
    return statistics.median(float(row[field]) for row in group) if group else float("nan")


def load_csv(path):
    with open(path, newline="") as handle:
        rows = list(csv.DictReader(handle))
    groups = defaultdict(list)
    for row in rows:
        groups[(row["access_ratio"], int(row["page_kb"]))].append(row)
    pages = sorted({int(row["page_kb"]) for row in rows})
    return groups, pages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("single_csv", type=Path)
    parser.add_argument("multi_csv", type=Path)
    parser.add_argument("--single-label", default="1 thread")
    parser.add_argument("--multi-label", default="8 threads")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    single, pages_s = load_csv(args.single_csv)
    multi, pages_m = load_csv(args.multi_csv)
    pages = sorted(set(pages_s) | set(pages_m))
    labels = [page_label(page) for page in pages]
    positions = list(range(len(pages)))

    fig, axes = plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)

    # Top-left: chunk64k effective bandwidth.
    ax = axes[0][0]
    s = [median(single, "chunk64k", page, "accessed_scan_gib_per_sec")
         for page in pages]
    m = [median(multi, "chunk64k", page, "accessed_scan_gib_per_sec")
         for page in pages]
    ax.plot(positions, s, "o-", linewidth=2.2, markersize=7,
            label=args.single_label)
    ax.plot(positions, m, "s-", linewidth=2.2, markersize=7,
            label=args.multi_label)
    ax.set_xticks(positions, labels)
    ax.set_ylabel("GiB/s")
    ax.set_title("chunk64k effective bandwidth")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()

    # Top-right: dense (100%) effective bandwidth.
    ax = axes[0][1]
    s = [median(single, "100", page, "accessed_scan_gib_per_sec")
         for page in pages]
    m = [median(multi, "100", page, "accessed_scan_gib_per_sec")
         for page in pages]
    ax.plot(positions, s, "o-", linewidth=2.2, markersize=7,
            label=args.single_label)
    ax.plot(positions, m, "s-", linewidth=2.2, markersize=7,
            label=args.multi_label)
    ax.set_xticks(positions, labels)
    ax.set_ylabel("GiB/s")
    ax.set_title("100% full-scan effective bandwidth")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()

    # Bottom-left: chunk64k load protocol bandwidth.
    ax = axes[1][0]
    s = [median(single, "chunk64k", page, "load_protocol_gib_per_sec")
         for page in pages]
    m = [median(multi, "chunk64k", page, "load_protocol_gib_per_sec")
         for page in pages]
    ax.plot(positions, s, "o-", linewidth=2.2, markersize=7,
            label=args.single_label)
    ax.plot(positions, m, "s-", linewidth=2.2, markersize=7,
            label=args.multi_label)
    ax.set_xticks(positions, labels)
    ax.set_ylabel("GiB/s")
    ax.set_title("chunk64k remote-load protocol bandwidth")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()

    # Bottom-right: store protocol bandwidth.
    ax = axes[1][1]
    s = [median(single, "chunk64k", page, "protocol_gib_per_sec")
         for page in pages]
    m = [median(multi, "chunk64k", page, "protocol_gib_per_sec")
         for page in pages]
    ax.plot(positions, s, "o-", linewidth=2.2, markersize=7,
            label=args.single_label)
    ax.plot(positions, m, "s-", linewidth=2.2, markersize=7,
            label=args.multi_label)
    ax.set_xticks(positions, labels)
    ax.set_ylabel("GiB/s")
    ax.set_title("Swap-out protocol bandwidth")
    ax.grid(axis="y", alpha=0.3)
    ax.legend()

    fig.suptitle("Anon sparse swap: 1 thread vs 8 threads", fontsize=16)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180, bbox_inches="tight")
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()
