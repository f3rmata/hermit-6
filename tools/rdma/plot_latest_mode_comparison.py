#!/usr/bin/env python3
"""Plot measured page-size peaks for local, cgroup-local, and Hermit."""

import argparse
import csv
import math
from pathlib import Path

import matplotlib.pyplot as plt


PAGES = ["4k", "16k", "32k", "64k", "128k", "256k", "512k", "1024k", "2048k"]
MODES = [
    ("local", "Local"),
    ("cgroup-linux", "Cgroup-local"),
    ("cgroup-hermit", "Cgroup-Hermit"),
]


def read_csv(path):
    with Path(path).open(newline="") as handle:
        return list(csv.DictReader(handle))


def status_peaks(rows, mode, status):
    values = []
    for page in PAGES:
        candidates = [
            float(row["achieved_qps"]) / 1000
            for row in rows
            if row.get("mode") == mode
            and row.get("page_label") == page
            and row.get("wait_status") == status
            and float(row["achieved_qps"]) > 0
        ]
        values.append(max(candidates) if candidates else math.nan)
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("native_summary", type=Path)
    parser.add_argument("hermit_summary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = read_csv(args.native_summary) + read_csv(args.hermit_summary)
    fig, axes = plt.subplots(3, 1, figsize=(10, 12), sharex=True,
                             constrained_layout=True)
    for ax, (mode, title) in zip(axes, MODES):
        stable = status_peaks(rows, mode, "stable")
        mixed = status_peaks(rows, mode, "mixed")
        ax.plot(PAGES, stable, "o-", linewidth=2, label="Measured (stable)")
        if any(math.isfinite(value) for value in mixed):
            ax.plot(PAGES, mixed, "s--", linewidth=2, color="tab:orange",
                    label="Measured (mixed)")
        ax.set_ylabel("Peak throughput (KQPS)")
        ax.set_title(title)
        ax.grid(axis="y", alpha=0.3)
        ax.margins(y=0.12)
        ax.legend()

    axes[-1].set_xlabel("Page size")
    fig.suptitle("dnet-61 latest complete page sweep — measured peak throughput",
                 fontsize=16)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180)
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()
