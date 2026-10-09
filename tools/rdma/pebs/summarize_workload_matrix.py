#!/usr/bin/env python3
"""Summarize a completed baseline-matched workload matrix without changing raw data."""
import argparse
import collections
import csv
import json
import os
from pathlib import Path
import re
import statistics


def write_csv(path, rows):
    with path.open('w') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    root = args.run
    plan = json.loads((root/'plan.json').read_text())
    completion = json.loads((root/'completion.json').read_text())
    rows = []
    for directory in sorted(root.glob('[0-9]*')):
        case = json.loads((directory/'case.json').read_text())
        status = json.loads((directory/'status.json').read_text())
        with next((directory/'results').glob('*summary.csv')).open() as f:
            records = list(csv.DictReader(f))
        if len(records) != 1 or not status['usable']:
            raise ValueError(f'Invalid sample: {directory}')
        record = records[0]
        metric = 'swapin_scan_sec' if case['suite'].startswith('anon') else {
            'redis':'bench_sec', 'ycsb':'run_sec', 'xgboost':'train_sec'}[case['suite']]
        before, after = [json.loads((directory/(tag+'.json')).read_text()) for tag in ('before','after')]
        def decisions(state):
            return sum(int(v) for v in re.findall(r'^\s*\d+:\s*(\d+)',state['controls']['pebs_order_stats'],re.M))
        rows.append(dict(suite=case['suite'],page_kb=case['page_kb'],case=case['case'],repeat=case['repeat'],
            metric=metric,seconds=float(record[metric]),large_load_pct=float(record['large_load_pct']),
            load_bytes=float(record['total_load_bytes']),store_bytes=float(record['total_store_bytes']),
            sampled=status['pebs_delta']['sampled'],lost=status['pebs_delta']['lost'],
            throttled=status['pebs_delta']['throttled'],decisions=decisions(after)-decisions(before),
            perf_rate=after['perf']['perf_event_max_sample_rate'],perf_changed=status['perf_rate_changed'],
            source=directory.name))
    if len(rows) != plan['samples'] or not completion['completed']:
        raise ValueError('Incomplete campaign')
    out = root/'analysis'; out.mkdir(exist_ok=True)
    write_csv(out/'samples.csv',rows)
    groups = collections.defaultdict(list)
    for row in rows: groups[row['suite'],row['page_kb'],row['case']].append(row)
    med = lambda group, key='seconds': statistics.median(row[key] for row in group)
    aggregate = []
    for (suite,page,case), group in sorted(groups.items()):
        off = groups[suite,page,'off']
        static = groups[suite,page,case.replace('policy-','static-')]
        aggregate.append(dict(suite=suite,page_kb=page,case=case,n=len(group),
            median_sec=med(group),min_sec=min(r['seconds'] for r in group),max_sec=max(r['seconds'] for r in group),
            time_change_vs_off_pct=100*(med(group)/med(off)-1),
            time_change_vs_static_pct=100*(med(group)/med(static)-1),
            median_load_bytes=med(group,'load_bytes'),load_change_vs_off_pct=100*(med(group,'load_bytes')/med(off,'load_bytes')-1),
            large_load_pct=med(group,'large_load_pct'),lost_cases=sum(r['lost']>0 for r in group),
            policy_decisions=sum(r['decisions'] for r in group)))
    write_csv(out/'aggregate.csv',aggregate)
    audit=dict(samples=len(rows),groups=len(groups),restore_errors=completion['restore_errors'],
        sampled=sum(r['sampled'] for r in rows),lost_counter_delta=sum(r['lost'] for r in rows),
        lost_cases=sum(r['lost']>0 for r in rows),throttled=sum(r['throttled'] for r in rows),
        perf_rates=sorted(set(r['perf_rate'] for r in rows)),perf_changed_cases=sum(r['perf_changed'] for r in rows))
    (out/'audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    suites = list(dict.fromkeys(row['suite'] for row in rows))
    periods = list(dict.fromkeys(r['load_period'] for r in plan['plan'] if r['mode'] is not None))
    periods.sort(reverse=True)
    lines=['| 负载 | 模式 | '+ ' | '.join(map(str,periods))+' |','|---|---|'+ '|'.join(['---:']*len(periods))+'|']
    for suite in suites:
        for mode in ('static','policy'):
            values=[statistics.median(r['time_change_vs_off_pct'] for r in aggregate if r['suite']==suite and r['case']==f'{mode}-{p}') for p in periods]
            lines.append('| '+suite+' | '+mode+' | '+' | '.join(f'{v:+.2f}%' for v in values)+' |')
    (out/'tables.md').write_text('\n'.join(lines)+'\n')
    os.environ.setdefault('MPLCONFIGDIR','/tmp/hermit-matplotlib')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    pages=sorted(set(r['page_kb'] for r in rows))
    for mode in ('static','policy'):
        fig,axes=plt.subplots(1,len(periods),figsize=(17,4.8),squeeze=False,layout='constrained')
        matrices=[]
        for p in periods:
            matrices.append(np.array([[next(r['time_change_vs_off_pct'] for r in aggregate if (r['suite'],r['page_kb'],r['case'])==(suite,page,f'{mode}-{p}')) for page in pages] for suite in suites]))
        limit=max(3,max(float(np.max(np.abs(m))) for m in matrices))
        for ax,p,matrix in zip(axes[0],periods,matrices):
            im=ax.imshow(matrix,cmap='RdBu_r',vmin=-limit,vmax=limit,aspect='auto')
            ax.set_xticks(range(len(pages)),pages,rotation=45);ax.set_yticks(range(len(suites)),suites)
            ax.set_title(f'Load period {p}');ax.set_xlabel('Requested page size (KiB)')
            for y in range(len(suites)):
                for x in range(len(pages)): ax.text(x,y,f'{matrix[y,x]:+.1f}',ha='center',va='center',fontsize=7)
        fig.colorbar(im,ax=list(axes[0]),label='Time change vs off (%) / positive = slower',shrink=.8)
        fig.suptitle(f'{mode}: median of 3 repetitions per cell; Redis/YCSB 2048 KiB actually use 4 KiB')
        for ext in ('png','pdf'):fig.savefig(out/f'{mode}-time-change.{ext}',dpi=180)
        plt.close(fig)
    fig,axes=plt.subplots(2,4,figsize=(16,8),layout='constrained')
    for ax,suite in zip(axes.flat,suites):
        for case in ('off','static-1999','policy-1999'):
            data=[next(r for r in aggregate if (r['suite'],r['page_kb'],r['case'])==(suite,page,case)) for page in pages]
            centers=np.array([r['median_sec'] for r in data])
            ax.errorbar(range(len(pages)),centers,yerr=[centers-np.array([r['min_sec'] for r in data]),np.array([r['max_sec'] for r in data])-centers],label=case,marker='.',capsize=2)
        ax.set_title(suite);ax.set_xticks(range(len(pages)),pages,rotation=60);ax.set_ylabel('Measured phase time (s)');ax.grid(alpha=.2)
    axes.flat[-1].axis('off');axes.flat[0].legend(fontsize=8)
    fig.suptitle('Off / medium static / medium policy: median and min-max, n=3 (not confidence intervals)')
    for ext in ('png','pdf'):fig.savefig(out/f'phase-time.{ext}',dpi=180)
    plt.close(fig)
    print(json.dumps(audit,indent=2))
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
