#!/usr/bin/env python3
"""Plot anonymous swap I/O results, including sparse access matrices."""

import argparse
import csv
import statistics
from collections import defaultdict
from pathlib import Path

import matplotlib.pyplot as plt


def page_label(page):
    return f"{page}k" if page < 1024 else f"{page // 1024}M"


def median(group, field):
    return statistics.median(float(row[field]) for row in group)


def benchmark_context(rows):
    parts = []
    threads = sorted({row.get("bench_threads", "") for row in rows} - {""})
    triggers = sorted({row.get("swapout_trigger", "") for row in rows} - {""})
    if len(threads) == 1:
        parts.append(f"{threads[0]} threads")
    if len(triggers) == 1:
        parts.append(triggers[0])
    return ", ".join(parts)


def plot_full_scan(rows, output):
    groups = defaultdict(list)
    for row in rows:
        groups[int(row["page_kb"])].append(row)
    pages = sorted(groups)
    labels = [page_label(page) for page in pages]
    store_bw = [median(groups[page], "protocol_gib_per_sec") for page in pages]
    store_pct = [median(groups[page], "large_store_pct") for page in pages]
    has_swapin = "load_protocol_gib_per_sec" in rows[0]
    fig, (ax_bw, ax_share) = plt.subplots(
        2, 1, figsize=(10, 8), sharex=True, constrained_layout=True
    )
    ax_bw.plot(labels, store_bw, "o-", linewidth=2, label="Swap-out")
    if has_swapin:
        load_bw = [
            median(groups[page], "load_protocol_gib_per_sec") for page in pages
        ]
        ax_bw.plot(labels, load_bw, "s-", linewidth=2, label="Swap-in")
    ax_bw.set_ylabel("Protocol throughput (GiB/s)")
    ax_bw.set_title("Sequential anonymous-memory swap I/O")
    ax_bw.grid(axis="y", alpha=0.3)
    ax_bw.legend()
    ax_share.plot(
        labels, store_pct, "o-", linewidth=2, color="tab:green", label="Store"
    )
    if has_swapin:
        load_pct = [median(groups[page], "large_load_pct") for page in pages]
        ax_share.plot(
            labels,
            load_pct,
            "s--",
            linewidth=2,
            color="tab:orange",
            label="Load",
        )
    ax_share.set_ylabel("Large-transfer byte share (%)")
    ax_share.set_xlabel("Requested folio size")
    ax_share.set_ylim(-3, 103)
    ax_share.grid(axis="y", alpha=0.3)
    ax_share.legend()
    fig.savefig(output, dpi=180)
    plt.close(fig)


def plot_sparse(rows, output):
    pages = sorted({int(row["page_kb"]) for row in rows})
    labels = [page_label(page) for page in pages]
    ratios = list(dict.fromkeys(row["access_ratio"] for row in rows))
    patterns = list(
        dict.fromkeys(
            (row["access_order"], row["access_locality"]) for row in rows
        )
    )
    groups = defaultdict(list)
    for row in rows:
        key = (
            row["access_order"],
            row["access_locality"],
            row["access_ratio"],
            int(row["page_kb"]),
        )
        groups[key].append(row)

    amplification_field = (
        "measured_read_amplification"
        if "measured_read_amplification" in rows[0]
        else "load_to_accessed_ratio"
    )
    metrics = [
        ("load_protocol_gib_per_sec", "Remote-load throughput\n(GiB/s)"),
        ("accessed_scan_gib_per_sec", "Useful-access throughput\n(GiB/s)"),
        (amplification_field, "Normalized read amplification"),
    ]
    fig, axes = plt.subplots(
        len(patterns),
        len(metrics),
        figsize=(16, max(3.2 * len(patterns), 6)),
        sharex=True,
        constrained_layout=True,
        squeeze=False,
    )
    for row_index, (access_order, locality) in enumerate(patterns):
        for column, (field, ylabel) in enumerate(metrics):
            axis = axes[row_index][column]
            for ratio in ratios:
                values = []
                valid_labels = []
                for page, label in zip(pages, labels):
                    group = groups[(access_order, locality, ratio, page)]
                    if group:
                        valid_labels.append(label)
                        values.append(median(group, field))
                if values:
                    axis.plot(
                        valid_labels,
                        values,
                        marker="o",
                        linewidth=1.6,
                        label=ratio,
                    )
            axis.set_ylabel(ylabel)
            axis.grid(axis="y", alpha=0.3)
            if row_index == 0:
                axis.set_title(metrics[column][1].replace("\n", " "))
            if row_index == len(patterns) - 1:
                axis.set_xlabel("Requested folio size")
            if column == 0:
                axis.text(
                    0.02,
                    0.92,
                    f"{access_order}, {locality} locality",
                    transform=axis.transAxes,
                    va="top",
                    ha="left",
                    fontweight="bold",
                )
            if row_index == 0 and column == len(metrics) - 1:
                axis.legend(title="Access ratio", ncol=2, fontsize=8)
    context = benchmark_context(rows)
    title = "Sparse anonymous-memory swap-in"
    if context:
        title += f" ({context})"
    fig.suptitle(title)
    fig.savefig(output, dpi=180)
    plt.close(fig)


def plot_sparse_summary(rows, output):
    pages = sorted({int(row["page_kb"]) for row in rows})
    labels = [page_label(page) for page in pages]
    ratios = list(dict.fromkeys(row["access_ratio"] for row in rows))
    repeat_count = len({row["repeat"] for row in rows})
    context = benchmark_context(rows)
    page_groups = defaultdict(list)
    ratio_groups = defaultdict(list)
    for row in rows:
        page = int(row["page_kb"])
        page_groups[page].append(row)
        ratio_groups[(page, row["access_ratio"])].append(row)

    positions = list(range(len(pages)))
    colors = {
        "100": "tab:blue",
        "50": "tab:orange",
        "25": "tab:green",
        "6.25": "tab:red",
        "1p": "tab:purple",
    }
    ratio_labels = {
        "100": "100%",
        "50": "50%",
        "25": "25%",
        "6.25": "6.25%",
        "1p": "1 page/folio",
    }
    dense_ratio = "100" if "100" in ratios else max(
        ratios,
        key=lambda ratio: median(
            [row for row in rows if row["access_ratio"] == ratio],
            "actual_access_pct",
        ),
    )
    fig, axes = plt.subplots(2, 2, figsize=(14, 9), constrained_layout=True)
    ax_protocol, ax_useful, ax_amplification, ax_share = axes.flat

    store_bw = [median(page_groups[page], "protocol_gib_per_sec") for page in pages]
    dense_load_bw = [
        median(ratio_groups[(page, dense_ratio)], "load_protocol_gib_per_sec")
        for page in pages
    ]
    ax_protocol.plot(positions, store_bw, "o-", linewidth=2, label="Swap-out")
    ax_protocol.plot(
        positions,
        dense_load_bw,
        "s-",
        linewidth=2,
        label=f"Swap-in ({ratio_labels.get(dense_ratio, dense_ratio)} access)",
    )
    ax_protocol.set_title("Protocol throughput")
    ax_protocol.set_ylabel("GiB/s")
    ax_protocol.legend()

    for ratio in ratios:
        useful = [
            median(ratio_groups[(page, ratio)], "accessed_scan_gib_per_sec")
            for page in pages
        ]
        ax_useful.plot(
            positions,
            useful,
            marker="o",
            linewidth=1.8,
            color=colors.get(ratio),
            label=ratio_labels.get(ratio, ratio),
        )
    ax_useful.set_title("Useful-access throughput")
    ax_useful.set_ylabel("GiB/s")
    ax_useful.legend(ncol=2, fontsize=9)

    for ratio in ratios:
        amplification = [
            median(ratio_groups[(page, ratio)], "measured_read_amplification")
            for page in pages
        ]
        ax_amplification.plot(
            positions,
            amplification,
            marker="o",
            linewidth=1.8,
            color=colors.get(ratio),
            label=ratio_labels.get(ratio, ratio),
        )
    ax_amplification.set_yscale("log", base=2)
    ax_amplification.set_title("Normalized read amplification")
    ax_amplification.set_ylabel("Remote bytes / useful bytes")
    ax_amplification.legend(ncol=2, fontsize=9)

    store_share = [median(page_groups[page], "large_store_pct") for page in pages]
    load_share = [median(page_groups[page], "large_load_pct") for page in pages]
    pmd_load_fallback = (
        2048 in pages
        and median(page_groups[2048], "large_load_pct") < 50.0
    )
    ax_share.plot(positions, store_share, "o-", linewidth=2, label="Store")
    ax_share.plot(positions, load_share, "s-", linewidth=2, label="Load")
    ax_share.set_title("Requested-order byte share")
    ax_share.set_ylabel("Percent")
    ax_share.set_ylim(-4, 104)
    ax_share.legend()

    for axis in axes.flat:
        axis.set_xticks(positions, labels)
        axis.set_xlabel("Requested folio size")
        axis.grid(axis="y", alpha=0.3)
        if pmd_load_fallback:
            fallback_position = pages.index(2048)
            axis.axvspan(
                fallback_position - 0.35,
                fallback_position + 0.35,
                color="0.85",
                alpha=0.65,
                zorder=0,
            )
    if pmd_load_fallback:
        ax_protocol.annotate(
            "2 MiB swap-in uses 4 KiB loads",
            xy=(pages.index(2048), dense_load_bw[pages.index(2048)]),
            xytext=(-145, 25),
            textcoords="offset points",
            arrowprops={"arrowstyle": "->", "color": "0.3"},
            fontsize=9,
        )
    fig.suptitle("Hermit sparse anonymous-memory swap I/O", fontsize=16)
    fig.text(
        0.5,
        -0.015,
        f"Medians over {repeat_count} repeats and 4 order/locality combinations"
        + (f"; {context}. " if context else ". ")
        + "Requested access ratios are quantized to 4 KiB base pages.",
        ha="center",
        fontsize=9,
    )
    fig.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(fig)


def plot_effective_throughput_by_density(rows, output):
    """Plot application-visible (not overfetch-inclusive) swap-in throughput."""
    pages = sorted({int(row["page_kb"]) for row in rows})
    labels = [page_label(page) for page in pages]
    positions = list(range(len(pages)))
    ratios = list(dict.fromkeys(row["access_ratio"] for row in rows))
    repeat_count = len({row["repeat"] for row in rows})
    context = benchmark_context(rows)
    groups = defaultdict(list)
    for row in rows:
        groups[(int(row["page_kb"]), row["access_ratio"])].append(row)

    styles = {
        "100": ("tab:orange", "s", "100% access"),
        "50": ("tab:green", "^", "50% access"),
        "25": ("tab:red", "D", "25% access"),
        "6.25": ("tab:purple", "P", "6.25% access"),
        "1p": ("tab:brown", "X", "1 page/folio"),
    }
    fig, axis = plt.subplots(figsize=(14, 8), constrained_layout=True)
    for ratio in ratios:
        color, marker, label = styles.get(ratio, (None, "o", ratio))
        values = [
            median(groups[(page, ratio)], "accessed_scan_gib_per_sec")
            for page in pages
        ]
        axis.plot(
            positions,
            values,
            marker=marker,
            color=color,
            linewidth=2.4,
            markersize=8,
            label=label,
        )

    if 2048 in pages:
        fallback_position = pages.index(2048)
        axis.axvspan(
            fallback_position - 0.35,
            fallback_position + 0.35,
            color="0.85",
            alpha=0.65,
            zorder=0,
        )
        axis.annotate(
            "2 MiB swap-in uses 4 KiB loads",
            xy=(fallback_position, max(
                median(groups[(2048, ratio)], "accessed_scan_gib_per_sec")
                for ratio in ratios
            )),
            xytext=(-210, -45),
            textcoords="offset points",
            arrowprops={"arrowstyle": "->", "color": "0.3"},
            fontsize=11,
        )
    axis.set_xticks(positions, labels)
    axis.set_xlabel("Requested folio size")
    axis.set_ylabel("Effective application throughput (GiB/s)")
    axis.set_title("Effective application throughput by access density")
    axis.grid(axis="y", alpha=0.3)
    axis.legend(ncol=2, loc="upper right")
    fig.suptitle("Hermit sparse anonymous-memory swap I/O", fontsize=20)
    fig.text(
        0.5,
        -0.015,
        f"dnet-61; {context}; medians over {repeat_count} repeats and "
        "4 order/locality combinations. Only application-accessed 4 KiB pages are counted.",
        ha="center",
        fontsize=10,
    )
    fig.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--effective-output", type=Path)
    args = parser.parse_args()
    with args.csv.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("no benchmark rows")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if "access_ratio" in rows[0]:
        plot_sparse(rows, args.output)
        if args.summary_output:
            args.summary_output.parent.mkdir(parents=True, exist_ok=True)
            plot_sparse_summary(rows, args.summary_output)
            print(args.summary_output)
        if args.effective_output:
            args.effective_output.parent.mkdir(parents=True, exist_ok=True)
            plot_effective_throughput_by_density(rows, args.effective_output)
            print(args.effective_output)
    else:
        plot_full_scan(rows, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
