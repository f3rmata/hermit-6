#!/usr/bin/env python3
"""Plot sparse/concurrent Redis Hermit page-size sweep results."""

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt


REQUIRED_FIELDS = {
    "page_kb",
    "active_ratio_requested",
    "get_qps",
    "useful_gib_per_sec",
    "normalized_read_amplification",
    "backend_loads_per_get",
}


def page_label(page_kb):
    return f"{page_kb}k" if page_kb < 1024 else f"{page_kb // 1024}M"


def median(rows, field):
    return statistics.median(float(row[field]) for row in rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with args.csv.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("no Redis benchmark rows")
    missing = REQUIRED_FIELDS - rows[0].keys()
    if missing:
        raise SystemExit("CSV lacks fields: " + ", ".join(sorted(missing)))

    pages = sorted({int(row["page_kb"]) for row in rows})
    labels = [page_label(page) for page in pages]
    ratios = list(dict.fromkeys(row["active_ratio_requested"] for row in rows))
    groups = defaultdict(list)
    for row in rows:
        groups[(row["active_ratio_requested"], int(row["page_kb"]))].append(row)

    metrics = [
        ("useful_gib_per_sec", "Useful Redis throughput", "GiB/s"),
        ("get_qps", "GET throughput", "GET/s"),
        ("normalized_read_amplification", "Normalized read amplification",
         "Remote bytes / useful bytes"),
        ("backend_loads_per_get", "Backend loads per GET", "Loads / GET"),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), constrained_layout=True)
    for axis, (field, title, ylabel) in zip(axes.flat, metrics):
        for ratio in ratios:
            values = []
            valid_labels = []
            for page, label in zip(pages, labels):
                group = groups[(ratio, page)]
                if group:
                    valid_labels.append(label)
                    values.append(median(group, field))
            axis.plot(valid_labels, values, marker="o", linewidth=2,
                      label=f"{ratio}%")
        axis.set_title(title)
        axis.set_xlabel("Requested folio size")
        axis.set_ylabel(ylabel)
        axis.grid(axis="y", alpha=0.3)
        axis.legend(title="Active keys")
    axes[1][0].set_yscale("log", base=2)

    clients = sorted({row.get("clients", "") for row in rows} - {""})
    instances = sorted({row.get("instances", "") for row in rows} - {""})
    order = sorted({row.get("access_order", "") for row in rows} - {""})
    context = []
    if len(clients) == 1:
        context.append(f"{clients[0]} clients")
    if len(instances) == 1:
        context.append(f"{instances[0]} instances")
    if len(order) == 1:
        context.append(f"{order[0]} access")
    title = "Redis sparse GET under Hermit"
    if context:
        title += " (" + ", ".join(context) + ")"
    fig.suptitle(title, fontsize=16)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180)
    print(args.output)


if __name__ == "__main__":
    main()
