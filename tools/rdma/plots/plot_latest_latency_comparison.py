#!/usr/bin/env python3
"""Compare measured read p99 latency at a fixed offered load."""

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt


PAGES = ["4k", "16k", "32k", "64k", "128k", "256k", "512k", "1024k", "2048k"]
MODES = [
    ("local", "Local", "o-"),
    ("cgroup-linux", "Cgroup-local", "s-"),
    ("cgroup-hermit", "Cgroup-Hermit", "^-"),
]


def read_csv(path):
    with Path(path).open(newline="") as handle:
        return list(csv.DictReader(handle))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("native_summary", type=Path)
    parser.add_argument("hermit_summary", type=Path)
    parser.add_argument("--offered-qps", type=float, default=500000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = read_csv(args.native_summary) + read_csv(args.hermit_summary)
    fig, ax = plt.subplots(figsize=(10, 6), constrained_layout=True)
    for mode, label, style in MODES:
        values = []
        for page in PAGES:
            candidates = [
                row for row in rows
                if row.get("mode") == mode
                and row.get("page_label") == page
                and float(row["offered_qps"]) == args.offered_qps
            ]
            if len(candidates) != 1:
                raise SystemExit(
                    f"expected one {mode}/{page} row at {args.offered_qps:g} QPS, "
                    f"got {len(candidates)}"
                )
            values.append(float(candidates[0]["read_p99_us"]))
        ax.plot(PAGES, values, style, linewidth=2, markersize=7, label=label)

    ax.set_xlabel("Page size")
    ax.set_ylabel("Read p99 latency (µs, log scale)")
    ax.set_yscale("log")
    ax.set_title(
        f"dnet-61 measured read p99 at offered {args.offered_qps / 1000:g} KQPS"
    )
    ax.grid(which="both", axis="y", alpha=0.3)
    ax.legend()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180)
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()
