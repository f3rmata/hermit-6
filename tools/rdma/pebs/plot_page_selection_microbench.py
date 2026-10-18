#!/usr/bin/env python3
"""Analyze saved measurements only; create CSV/JSON and Matplotlib PNGs."""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import median


def load_rows(root):
    manifest = json.loads((root/'manifest.json').read_text())
    if manifest['schema'] != 'page-selection-v1':
        raise ValueError('Unexpected result schema')
    rows = []
    for path in sorted(root.glob('0*/p*-e*/result.json')):
        r = json.loads(path.read_text())
        access, out = r['access_delta'], r['pageout_delta']
        row = {k: r[k] for k in ['repeat', 'scenario', 'case', 'phase', 'epoch', 'density_pages', 'valid']}
        row.update(path=str(path.relative_to(root)), warnings='; '.join(r['warnings']),
                   access_seconds=r['access']['seconds'], pageout_seconds=r['pageout']['seconds'],
                   settle_seconds=r['pageout']['settle_seconds'],
                   page_coverage_bytes=r['access']['page_coverage_bytes'],
                   read_instructions=r['access']['read_instructions'],
                   checksum_errors=r['access']['checksum_errors'])
        for label, delta in [('access', access), ('pageout', out)]:
            for metric in ['read_bytes', 'write_bytes', 'read_wrs', 'write_wrs']:
                row[f'{label}_{metric}'] = sum(v[metric] for v in delta['wr'].values())
            for metric in ['sampled', 'lost', 'throttled', 'unknown']:
                row[f'{label}_{metric}'] = delta['pebs'][metric]
            row[f'{label}_samples_per_second'] = delta['pebs']['sampled'] / delta['snapshot_seconds']
            row[f'{label}_snapshot_seconds'] = delta['snapshot_seconds']
            row[f'{label}_fallback_4k'] = sum(v['fallback_4k'] for v in delta['order'].values())
            row[f'{label}_errors'] = sum(v['errors'] for v in delta['order'].values())
        row['read_to_coverage'] = row['access_read_bytes'] / row['page_coverage_bytes']
        for stage in ['before', 'evicted', 'after']:
            pages = json.loads((path.parent/f'pages-{stage}'/'page-sizes.json').read_text())
            for metric in ['resident_mapped_bytes', 'swapped_mapped_bytes', 'unknown_resident_bytes',
                           'not_present_mapped_bytes']:
                row[f'{stage}_{metric}'] = pages[metric]
            row[f'{stage}_folio_mapped_bytes'] = {k: v['mapped_bytes'] for k, v in pages['folio_size_kib'].items()}
        rows.append(row)
    if not rows:
        raise ValueError('No measured epochs; will not fabricate figures')
    return manifest, rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('results', type=Path)
    args = ap.parse_args()
    root = args.results.resolve()
    manifest, rows = load_rows(root)
    out = root/'analysis'
    out.mkdir(exist_ok=False)
    with (out/'epochs.csv').open('w') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader()
        writer.writerows({k: json.dumps(v, sort_keys=True) if isinstance(v, dict) else v
                         for k, v in r.items()} for r in rows)
    (out/'epochs.json').write_text(json.dumps(rows, indent=2) + '\n')
    expected_epochs = manifest['args']['epochs']
    by_run = defaultdict(list)
    for row in rows:
        by_run[row['scenario'], row['case'], row['repeat']].append(row)
    aggregates = []
    for spec in manifest['runs']:
        group = by_run[spec['scenario'], spec['case'], spec['repeat']]
        wanted = {(p, e) for p in range(1, len(spec['phases']) + 1)
                  for e in range(1, expected_epochs + 1)}
        if {(r['phase'], r['epoch']) for r in group} != wanted or not all(r['valid'] for r in group):
            continue
        seconds = sum(r['access_seconds'] for r in group)
        aggregates.append({k: spec[k] for k in ['scenario', 'case', 'repeat']} | {
            'access_seconds': seconds,
            'pageout_seconds': sum(r['pageout_seconds'] for r in group),
            'read_mib': sum(r['access_read_bytes'] for r in group) / 1024**2,
            'write_mib': sum(r['pageout_write_bytes'] + r['access_write_bytes'] for r in group) / 1024**2,
            'samples_per_second': sum(r['access_sampled'] for r in group) /
                                  sum(r['access_snapshot_seconds'] for r in group),
            'read_to_coverage': sum(r['access_read_bytes'] for r in group) /
                                sum(r['page_coverage_bytes'] for r in group)})
    (out/'runs.json').write_text(json.dumps(aggregates, indent=2) + '\n')
    summary = []
    for scenario in manifest['args']['scenarios'].split(','):
        for case in manifest['args']['cases'].split(','):
            group = [r for r in aggregates if r['scenario'] == scenario and r['case'] == case]
            for metric in ['access_seconds', 'pageout_seconds', 'read_mib', 'write_mib',
                           'samples_per_second', 'read_to_coverage']:
                values = [r[metric] for r in group]
                summary.append({'scenario': scenario, 'case': case, 'metric': metric, 'n': len(values),
                                'median': median(values) if values else None,
                                'min': min(values) if values else None, 'max': max(values) if values else None})
    with (out/'summary.csv').open('w') as f:
        writer = csv.DictWriter(f, fieldnames=list(summary[0])); writer.writeheader(); writer.writerows(summary)
    (out/'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.size': 9, 'figure.dpi': 120})
    cases = manifest['args']['cases'].split(',')
    scenarios = manifest['args']['scenarios'].split(',')
    colors = dict(zip(cases, plt.get_cmap('tab10').colors))
    for scenario in scenarios:
        fig, axes = plt.subplots(2, 3, figsize=(15, 8))
        for ax, (metric, title) in zip(axes.flat, [
            ('access_seconds', 'Total timed access (s)'), ('pageout_seconds', 'Total pageout calls (s)'),
            ('read_mib', 'Access RDMA reads (MiB)'), ('write_mib', 'Pageout + access RDMA writes (MiB)'),
            ('samples_per_second', 'Global samples / snapshot second'),
            ('read_to_coverage', 'RDMA read bytes / selected page coverage')]):
            for x, case in enumerate(cases):
                values = [r[metric] for r in aggregates if r['scenario'] == scenario and r['case'] == case]
                if values:
                    middle = median(values)
                    ax.errorbar(x, middle, yerr=[[middle - min(values)], [max(values) - middle]],
                                fmt='o', color=colors[case], capsize=4)
                    ax.scatter([x] * len(values), values, color=colors[case], s=12, alpha=.5)
                    ax.annotate(f'n={len(values)}', (x, max(values)), xytext=(0, 7),
                                textcoords='offset points', ha='center', fontsize=7)
            ax.set_xticks(range(len(cases)), cases, rotation=35, ha='right')
            ax.set_title(title); ax.set_ylim(bottom=0); ax.grid(axis='y', alpha=.2)
        fig.suptitle(f'{scenario}: complete valid runs; median and min–max across repeats (not CI)')
        fig.tight_layout(); fig.savefig(out/f'{scenario}-metrics.png'); plt.close(fig)

        # Denominator is the full mapping; swapped/unknown pages remain visible.
        fig, axes = plt.subplots(1, 3, figsize=(16, 5), sharey=True)
        sizes = sorted({int(k) for r in rows if r['scenario'] == scenario
                        for stage in ['before', 'evicted', 'after']
                        for k in r[f'{stage}_folio_mapped_bytes']})
        components = [(f'{s} KiB', str(s)) for s in sizes] + [
            ('swapped', 'swapped_mapped_bytes'), ('unknown resident', 'unknown_resident_bytes'),
            ('not present', 'not_present_mapped_bytes')]
        valid_keys = {(r['scenario'], r['case'], r['repeat']) for r in aggregates}
        for ax, stage in zip(axes, ['before', 'evicted', 'after']):
            bottoms = [0.] * len(cases)
            for label, component in components:
                vals = []
                for case in cases:
                    group = [r for r in rows if r['scenario'] == scenario and r['case'] == case
                             and (scenario, case, r['repeat']) in valid_keys]
                    amount = sum(r[f'{stage}_folio_mapped_bytes'].get(component, 0)
                                 if component.isdigit() else r[f'{stage}_{component}'] for r in group)
                    vals.append(100 * amount / (len(group) * manifest['args']['workset_mb'] * 1024**2)
                                if group else float('nan'))
                ax.bar(range(len(cases)), vals, bottom=bottoms, label=label)
                bottoms = [a + b for a, b in zip(bottoms, vals)]
            ax.set_xticks(range(len(cases)), cases, rotation=35, ha='right'); ax.set_title(stage)
            ax.set_ylim(0, 100)
        axes[0].set_ylabel('Mapping bytes (%) — mean over epoch snapshots')
        axes[-1].legend(loc='upper left', bbox_to_anchor=(1, 1))
        fig.suptitle(f'{scenario}: observed folios, not requested allocation or WR size')
        fig.tight_layout(); fig.savefig(out/f'{scenario}-folios.png'); plt.close(fig)
    switch = [r for r in rows if r['scenario'] == 'switch' and r['valid']]
    if switch:
        fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
        for ax, (metric, title, scale) in zip(axes, [
            ('access_seconds', 'Timed access (s)', 1),
            ('access_read_bytes', 'Access RDMA reads (MiB)', 1024**2),
            ('access_samples_per_second', 'Global samples / snapshot second', 1)]):
            for case in cases:
                points = defaultdict(list)
                for r in switch:
                    if r['case'] == case:
                        points[(r['phase'] - 1) * expected_epochs + r['epoch']].append(r[metric] / scale)
                xs = sorted(points)
                if xs:
                    ax.plot(xs, [median(points[x]) for x in xs], marker='.', label=case, color=colors[case])
                    ax.fill_between(xs, [min(points[x]) for x in xs], [max(points[x]) for x in xs],
                                    color=colors[case], alpha=.1)
            for x in [expected_epochs + .5, expected_epochs * 2 + .5]:
                ax.axvline(x, linestyle='--', color='gray')
            ax.set_ylabel(title); ax.set_ylim(bottom=0); ax.grid(alpha=.2)
        axes[0].legend(ncol=4); axes[-1].set_xlabel('Epoch (same process and mapping; no warmup at transitions)')
        fig.suptitle('Sparse → dense → sparse; median and min–max of available valid epochs')
        fig.tight_layout(); fig.savefig(out/'switch-timeline.png'); plt.close(fig)
    status = (root/'status').read_text().strip()
    report = [f'Campaign status: {status}', f'Complete valid runs: {len(aggregates)}/{len(manifest["runs"])}',
              f'Observed epochs: {len(rows)}; invalid: {sum(not r["valid"] for r in rows)}',
              f'Epochs with quality notes: {sum(bool(r["warnings"]) for r in rows)}',
              'No ranking or expected winner is imposed. Missing/invalid runs are not zero measurements.',
              'Counters are system-wide; concurrent RDMA traffic may contaminate attribution.',
              'PEBS sampled counts include other processes; target-attributed samples are unavailable.',
              'Snapshot windows include control latency, not just worker timing. Ring consumption is asynchronous.',
              'RDMA/coverage is a ratio, not pure read amplification: some selected pages may already be resident.',
              'MADV_PAGEOUT is controlled reclaim, not a sustained memory.high application workload.',
              'Pageout syscall timing and settling time are separate; settling does not prove hardware completion.',
              'Folio snapshots are non-atomic and outside timed accesses, but can perturb caches and policy cooling.',
              'Fixed sizes are allocation requests with fallback; examine observed distributions.',
              'Different density scenarios perform different amounts of work; compare cases within each scenario.',
              'Min–max across repeats is not a confidence interval; epochs are not independent repetitions.',
              'See summary.csv/json for median/min/max and available repeat counts; empty cells mean missing.',
              'See epochs.csv/json for per-cycle lost/throttled/fallback/errors, traffic in both directions and folios.']
    (out/'REPORT.txt').write_text('\n'.join(report) + '\n')
    print('\n'.join(report[:4])); print(out)


if __name__ == '__main__':
    main()
