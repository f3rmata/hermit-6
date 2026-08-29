#!/usr/bin/env python3
"""Plot the measured chunk64k inverted-U from anon sparse swap data."""

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
    args = parser.parse_args()

    with args.csv.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise SystemExit("no benchmark rows")

    groups = defaultdict(list)
    for row in rows:
        groups[(row["access_ratio"], int(row["page_kb"]))].append(row)

    pages = sorted({int(row["page_kb"]) for row in rows})
    labels = [page_label(page) for page in pages]
    positions = list(range(len(pages)))

    full_values = []
    chunk_values = []
    chunk_amplification = []
    protocol_values = []
    for page in pages:
        full_values.append(
            median(groups[("100", page)], "accessed_scan_gib_per_sec")
            if groups[("100", page)] else float("nan"))
        chunk_values.append(
            median(groups[("chunk64k", page)], "accessed_scan_gib_per_sec")
            if groups[("chunk64k", page)] else float("nan"))
        chunk_amplification.append(
            median(groups[("chunk64k", page)], "measured_read_amplification")
            if groups[("chunk64k", page)] else float("nan"))
        protocol_values.append(
            median(groups[("100", page)], "load_protocol_gib_per_sec")
            if groups[("100", page)] else float("nan"))

    fig, (ax_eff, ax_amp) = plt.subplots(
        2, 1, figsize=(12, 9), sharex=True, constrained_layout=True)

    # Effective bandwidth panel.
    ax_eff.plot(positions, full_values, marker="o", linewidth=2.4,
                markersize=8, color="tab:blue", label="100% full scan")
    ax_eff.plot(positions, chunk_values, marker="*", linewidth=3.0,
                markersize=13, color="black", label="chunk64k access")
    ax_eff.plot(positions, protocol_values, marker="s", linewidth=2.0,
                markersize=7, color="tab:green",
                label="Remote-load protocol bandwidth (100% scan)")
    ax_eff.set_xticks(positions, labels)
    ax_eff.set_ylabel("GiB/s")
    ax_eff.set_title("Effective swap-in bandwidth: fixed 64 KiB chunk access")
    ax_eff.grid(axis="y", alpha=0.3)
    ax_eff.legend(ncol=1, loc="upper right", fontsize=9)

    # Annotate the peak and the 2 MiB fallback.
    peak_idx = pages.index(64)
    ax_eff.annotate(
        "Peak at 64 KiB",
        xy=(peak_idx, chunk_values[peak_idx]),
        xytext=(peak_idx + 0.4, chunk_values[peak_idx] + 2.0),
        arrowprops={"arrowstyle": "->", "color": "0.2"},
        fontsize=11,
    )
    if 2048 in pages:
        idx = pages.index(2048)
        ax_eff.axvspan(idx - 0.35, idx + 0.35, color="0.85", alpha=0.65,
                       zorder=0)
        ax_eff.annotate(
            "2 MiB swap-in falls back to 4 KiB loads",
            xy=(idx, chunk_values[idx]),
            xytext=(idx - 3.5, chunk_values[idx] + 3.0),
            arrowprops={"arrowstyle": "->", "color": "0.3"},
            fontsize=10,
        )

    # Read amplification panel.
    ax_amp.plot(positions, chunk_amplification, marker="*", linewidth=3.0,
                markersize=13, color="black", label="chunk64k read amplification")
    ax_amp.set_yscale("log", base=2)
    ax_amp.set_xticks(positions, labels)
    ax_amp.set_xlabel("Requested folio size")
    ax_amp.set_ylabel("Read amplification\n(remote bytes / useful bytes)")
    ax_amp.set_title("Read amplification for chunk64k access")
    ax_amp.grid(axis="y", alpha=0.3, which="both")
    ax_amp.legend(loc="upper left", fontsize=9)

    if 2048 in pages:
        idx = pages.index(2048)
        ax_amp.axvspan(idx - 0.35, idx + 0.35, color="0.85", alpha=0.65,
                       zorder=0)
        ax_amp.annotate(
            "4 KiB load fallback\n(amplification back to 1x)",
            xy=(idx, chunk_amplification[idx]),
            xytext=(idx - 3.8, chunk_amplification[idx] * 2.5),
            arrowprops={"arrowstyle": "->", "color": "0.3"},
            fontsize=10,
        )

    fig.text(
        0.5, -0.015,
        "dnet-61, Hermit 6.18, 16 GiB workset, 70% local memory, "
        "8 threads, parallel-fault swapout. Medians over 3 repeats.",
        ha="center", fontsize=9,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.output, dpi=180, bbox_inches="tight")
    plt.close(fig)
    print(args.output)


if __name__ == "__main__":
    main()
