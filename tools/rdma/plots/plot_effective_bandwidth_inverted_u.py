#!/usr/bin/env python3
"""Plot effective swap-in bandwidth, including the fixed-chunk prediction.

The fixed-chunk curve predicts what `chunk64k` access should measure: for
folios <= 64 KiB the whole folio is useful; for larger folios only 64 KiB
per folio is useful, so effective bandwidth falls by ~1/folio size.  The
prediction uses the measured full-scan (100% access) remote-load protocol
bandwidth for each folio size.
"""

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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chunk-kib", type=int, default=64)
    parser.add_argument("--show-ratios", default="100,50,25,1p")
    parser.add_argument("--chunk-ratio", default="chunk64k")
    args = parser.parse_args()

    with args.csv.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("no benchmark rows")

    page_groups = defaultdict(list)
    ratio_groups = defaultdict(list)
    full_load_groups = defaultdict(list)
    for row in rows:
        page = int(row["page_kb"])
        page_groups[page].append(row)
        ratio_groups[(page, row["access_ratio"])].append(row)
        if row["access_ratio"] == "100":
            full_load_groups[page].append(row)

    pages = sorted(page_groups)
    labels = [page_label(page) for page in pages]
    positions = list(range(len(pages)))

    styles = {
        "100": ("tab:blue", "o", "100% access"),
        "50": ("tab:orange", "s", "50% access"),
        "25": ("tab:green", "^", "25% access"),
        "1p": ("tab:red", "X", "1 page/folio"),
    }

    fig, (ax_eff, ax_amp) = plt.subplots(
        2, 1, figsize=(12, 9), sharex=True, constrained_layout=True
    )

    for ratio in args.show_ratios.split(","):
        ratio = ratio.strip()
        color, marker, label = styles.get(ratio, (None, "o", ratio))
        values = [
            median(ratio_groups[(page, ratio)], "accessed_scan_gib_per_sec")
            if ratio_groups[(page, ratio)] else float("nan")
            for page in pages
        ]
        ax_eff.plot(positions, values, marker=marker, color=color,
                    linewidth=2.2, markersize=7, label=label)

        amplification = [
            median(ratio_groups[(page, ratio)], "measured_read_amplification")
            if ratio_groups[(page, ratio)] else float("nan")
            for page in pages
        ]
        ax_amp.plot(positions, amplification, marker=marker, color=color,
                    linewidth=2.0, markersize=6, label=label)

    # Predicted fixed-chunk effective bandwidth from full-scan protocol load.
    chunk = args.chunk_kib
    predicted = []
    for page in pages:
        if not full_load_groups[page]:
            predicted.append(float("nan"))
            continue
        load = median(full_load_groups[page], "load_protocol_gib_per_sec")
        predicted.append(load * min(1.0, chunk / page))
    ax_eff.plot(positions, predicted, marker="D", color="black",
                linestyle="--", linewidth=2.4, markersize=8,
                label=f"Predicted {chunk} KiB chunk")
    ax_amp.plot(positions, [max(1.0, page / chunk) for page in pages],
                marker="D", color="black", linestyle="--", linewidth=2.4,
                markersize=8, label=f"Predicted chunk read amplification")

    # Measured fixed-chunk curve, when the CSV contains such rows.
    measured_chunk = args.chunk_ratio
    if measured_chunk and any(row["access_ratio"] == measured_chunk for row in rows):
        chunk_values = [
            median(ratio_groups[(page, measured_chunk)],
                   "accessed_scan_gib_per_sec")
            if ratio_groups[(page, measured_chunk)] else float("nan")
            for page in pages
        ]
        ax_eff.plot(positions, chunk_values, marker="*", color="black",
                    linestyle="-", linewidth=2.8, markersize=10,
                    label=f"Measured {chunk} KiB chunk")
        chunk_amp = [
            median(ratio_groups[(page, measured_chunk)],
                   "measured_read_amplification")
            if ratio_groups[(page, measured_chunk)] else float("nan")
            for page in pages
        ]
        ax_amp.plot(positions, chunk_amp, marker="*", color="black",
                    linestyle="-", linewidth=2.8, markersize=10,
                    label=f"Measured chunk read amplification")

    if 2048 in pages:
        idx = pages.index(2048)
        ax_eff.axvspan(idx - 0.35, idx + 0.35, color="0.85", alpha=0.65,
                       zorder=0)
        ax_amp.axvspan(idx - 0.35, idx + 0.35, color="0.85", alpha=0.65,
                       zorder=0)
        ratios_for_annotation = [
            ratio.strip() for ratio in args.show_ratios.split(",")
        ] + ([measured_chunk] if measured_chunk else [])
        ax_eff.annotate(
            "2 MiB swap-in uses 4 KiB loads",
            xy=(idx, max(
                median(ratio_groups[(2048, ratio)],
                       "accessed_scan_gib_per_sec")
                for ratio in ratios_for_annotation
                if ratio and ratio_groups[(2048, ratio)]
            )),
            xytext=(-220, -55),
            textcoords="offset points",
            arrowprops={"arrowstyle": "->", "color": "0.3"},
            fontsize=10,
        )

    ax_eff.set_xticks(positions, labels)
    ax_eff.set_ylabel("Effective bandwidth (GiB/s)")
    ax_eff.set_title(
        "Effective swap-in bandwidth: measured sparse access vs "
        f"{chunk} KiB fixed-chunk"
    )
    ax_eff.grid(axis="y", alpha=0.3)
    ax_eff.legend(ncol=3, fontsize=9, loc="upper right")

    ax_amp.set_yscale("log", base=2)
    ax_amp.set_xticks(positions, labels)
    ax_amp.set_xlabel("Requested folio size")
    ax_amp.set_ylabel("Read amplification (remote / useful bytes)")
    ax_amp.set_title("Read amplification")
    ax_amp.grid(axis="y", alpha=0.3, which="both")
    ax_amp.legend(ncol=3, fontsize=9)

    fig.text(
        0.5, -0.015,
        f"Medians over {len({row['repeat'] for row in rows})} repeats. "
        "Chunk curve = measured full-scan remote-load bandwidth "
        "x (chunk / folio) for folios larger than the chunk.",
        ha="center", fontsize=9,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180, bbox_inches="tight")
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()
