#!/usr/bin/env python3
"""Overlay a new single-page rerun on the previous complete page sweep."""

import argparse
import csv
from pathlib import Path

import matplotlib.pyplot as plt


PAGES = ["4k", "16k", "32k", "64k", "128k", "256k", "512k", "1024k", "2048k"]


def rows(path):
    with Path(path).open(newline="") as handle:
        return list(csv.DictReader(handle))


def peak(rows_for_page):
    return max(float(row["achieved_qps"]) for row in rows_for_page) / 1000


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("complete_summary", type=Path)
    parser.add_argument("rerun_summary", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    complete = rows(args.complete_summary)
    rerun = rows(args.rerun_summary)
    baseline = [peak([row for row in complete if row["page_label"] == page])
                for page in PAGES]
    rerun_pages = sorted(set(row["page_label"] for row in rerun))
    if rerun_pages != ["2048k"]:
        raise SystemExit(f"expected only a 2048k rerun, got {rerun_pages}")
    rerun_peak = peak(rerun)

    fig, ax = plt.subplots(figsize=(10, 5), constrained_layout=True)
    ax.plot(PAGES, baseline, "o-", linewidth=2, label="Measured (complete sweep)")
    ax.plot(["2048k"], [rerun_peak], "*", markersize=15, color="tab:red",
            label="Measured (latest 2 MiB rerun)")
    ax.set_xlabel("Page size")
    ax.set_ylabel("Peak achieved throughput (KQPS)")
    ax.set_title("Cgroup-Hermit: measured peak throughput by page size")
    ax.grid(axis="y", alpha=0.3)
    ax.margins(y=0.12)
    ax.legend()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180)
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()
