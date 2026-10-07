#!/usr/bin/env python3
"""Combine three validated PEBS experiment phases into shareable figures/tables."""
import argparse
import csv
import json
import re
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

PHASES = ('pressure', 'resident', 'saturation')
COLORS = {'off-original-mask': '#444444', 'off': '#999999',
          'static-medium': '#377eb8', 'policy-medium': '#e07a22', 'policy-high': '#c34c15'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('campaign', type=Path)
    args = parser.parse_args()
    root = args.campaign
    completion = dict(line.split('=', 1) for line in (root / 'completion.txt').read_text().splitlines())
    if completion['exit_code'] != '0':
        raise ValueError('Campaign did not complete successfully')
    audit = json.loads((root / 'final-audit.json').read_text())
    if audit['issues'] or audit['pebs_enabled'] != '0':
        raise ValueError('Final environment audit failed')
    stages = list(csv.DictReader((root / 'stages.tsv').open(), delimiter='\t'))
    if [s['suite'] for s in stages] != list(PHASES) or any(s['exit_code'] != '0' for s in stages):
        raise ValueError('Missing or failed stage')
    data, comparisons, counts, frequencies = {}, {}, {}, []
    for phase in PHASES:
        folder = root / phase
        validation = json.loads((folder / 'validation.json').read_text())
        if validation['issues'] or validation['rows'] != validation['expected_rows']:
            raise ValueError(f'{phase}: failed validation')
        data[phase] = json.loads((folder / 'aggregate.json').read_text())
        comparisons[phase] = json.loads((folder / 'comparisons.json').read_text())
        rows = list(csv.DictReader((folder / 'summary.csv').open()))
        counts[phase] = len(rows)
        for row in rows:
            if row['case'].startswith('off'):
                continue
            run = folder / f"r{row['repeat']}-{row['case']}"
            periods = []
            for point in ('before', 'after'):
                text = (run / point / 'pebs_order_stats').read_text()
                match = re.search(r'adaptive: (\d+) load_period: (\d+) store_period: (\d+)', text)
                if not match:
                    raise ValueError(f'Missing effective period: {run}/{point}')
                periods.append(tuple(map(int, match.groups())))
            frequencies.append({'phase': phase, 'case': row['case'], 'repeat': row['repeat'],
                                'adaptive': periods[0][0], 'load_period_before': periods[0][1],
                                'store_period_before': periods[0][2], 'load_period_after': periods[1][1],
                                'store_period_after': periods[1][2],
                                'samples_per_second': row['samples_per_second'],
                                'lost_records': row['lost_delta'], 'throttle_records': row['throttled_delta']})
    with (root / 'sampling-frequencies.csv').open('w') as file:
        writer = csv.DictWriter(file, fieldnames=list(frequencies[0]))
        writer.writeheader()
        writer.writerows(frequencies)

    def bars(ax, phase, cases, metric, title, divisor=1):
        stats = [data[phase][case][metric] for case in cases]
        med = [v['median'] / divisor for v in stats]
        err = [[(v['median'] - v['min']) / divisor for v in stats],
               [(v['max'] - v['median']) / divisor for v in stats]]
        ax.bar(range(len(cases)), med, yerr=err, capsize=4,
               color=[COLORS.get(c, '#377eb8') for c in cases])
        ax.set_xticks(range(len(cases)), cases, rotation=20, ha='right')
        ax.set_title(title)
        if metric == 'read_p99_us':
            ax.set_yscale('log')
        ax.grid(axis='y', alpha=.2)
    fig, axes = plt.subplots(2, 2, figsize=(12, 9), layout='constrained')
    pressure_cases = ['off-original-mask', 'off', 'static-medium', 'policy-medium']
    bars(axes[0, 0], 'pressure', pressure_cases + ['policy-high'], 'read_p99_us', 'Pressure: read p99 (ms, log), offered 30k QPS', 1000)
    bars(axes[0, 1], 'pressure', pressure_cases + ['policy-high'], 'rdma_read_kib_per_op', 'Pressure: RDMA read KiB / operation')
    bars(axes[1, 0], 'resident', ['off', 'static-medium', 'policy-medium'], 'system_busy_cpu_us_per_op', 'Resident: whole-system CPU us / operation')
    bars(axes[1, 1], 'saturation', pressure_cases, 'qps', 'Saturation: achieved throughput (k QPS)', 1000)
    fig.suptitle('Hermit PEBS | Memcached, 800k items, 1 KiB values\nMedians with min-max; pressure/saturation n=5, resident n=3')
    for suffix in ('png', 'pdf'):
        fig.savefig(root / f'overview.{suffix}', dpi=160)
    plt.close(fig)

    fig, axes = plt.subplots(2, 2, figsize=(12, 9), layout='constrained')
    for col, phase in enumerate(('pressure', 'resident')):
        for prefix, color in (('static', '#377eb8'), ('policy', '#e07a22')):
            cases = [f'{prefix}-{level}' for level in ('low', 'medium', 'high', 'adaptive')
                     if f'{prefix}-{level}' in data[phase]]
            for row, metric in enumerate(('system_busy_cpu_us_per_op', 'read_p99_us')):
                ax = axes[row, col]
                for case in cases:
                    x, y = data[phase][case]['samples_per_second'], data[phase][case][metric]
                    ax.errorbar(x['median'], y['median'],
                                xerr=[[x['median']-x['min']], [x['max']-x['median']]],
                                yerr=[[y['median']-y['min']], [y['max']-y['median']]],
                                fmt={'low':'v','medium':'s','high':'^','adaptive':'D'}[case.split('-')[1]],
                                color=color, alpha=.8, capsize=3, label=case)
                ax.set_xscale('log')
                if row == 1 and phase == 'pressure':
                    ax.set_yscale('log')
                ax.set_xlabel('Measured usable samples / second (system-wide)')
                ax.set_ylabel('Whole-system CPU us / op' if row == 0 else ('Read p99 (us, log scale)' if phase == 'pressure' else 'Read p99 (us)'))
                ax.set_title(phase)
                ax.grid(alpha=.2)
        for row, metric in enumerate(('system_busy_cpu_us_per_op', 'read_p99_us')):
            reference = data[phase]['off'][metric]
            axes[row, col].axhline(reference['median'], color='#777777', ls='--', lw=1, label='off baseline')
            axes[row, col].axhspan(reference['min'], reference['max'], color='#777777', alpha=.08)
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='lower center', bbox_to_anchor=(.5, -.10), ncol=4)
    fig.suptitle('Sampling rate and workload cost | medians with min-max\nBlue: sampling only; orange: policy. Adaptive also changes store period.')
    for suffix in ('png', 'pdf'):
        fig.savefig(root / f'sampling-cost.{suffix}', dpi=160, bbox_inches='tight')
    plt.close(fig)

    lines = ['# PEBS 三阶段性能汇总', '',
             f"共 {sum(counts.values())} 条有效正式测量：" + '，'.join(f'{p} {counts[p]}' for p in PHASES) + '。', '',
             '数值为中位数；方括号为最小–最大值。CPU 指整机忙碌 CPU，包含客户端和内核；固定 QPS 阶段不测最大吞吐。', '']
    metrics = [('qps', 'QPS'), ('read_p99_us', 'read p99 µs'),
               ('system_busy_cpu_us_per_op', 'CPU µs/op'), ('samples_per_second', 'samples/s'),
               ('rdma_read_kib_per_op', 'RDMA read KiB/op')]
    adjustments = sum(len(json.loads((root / phase / 'validation.json').read_text()).get('perf_rate_adjustments', [])) for phase in PHASES)
    lines += [f'独立收尾验收通过；内核记录 {adjustments} 次 perf 中断耗时过长并自动下调全局采样上限，最终上限为 {audit["perf_sysctl"]["perf_event_max_sample_rate"]}。零 ring lost/throttled 记录不代表未调整 perf 上限。详见各阶段日志与 final-audit.json。', '']
    for phase in PHASES:
        lines += [f'## {phase}', '', '| 配置 | n | ' + ' | '.join(label for _, label in metrics) + ' |',
                  '|---|---:|' + '---:|' * len(metrics)]
        for case, values in data[phase].items():
            cells = [f"{values[key]['median']:.2f} [{values[key]['min']:.2f}, {values[key]['max']:.2f}]" for key, _ in metrics]
            lines.append(f"| {case} | {values['qps']['n']} | " + ' | '.join(cells) + ' |')
        lines += ['', f'[阶段图表与完整配对统计]({phase}/REPORT.md)', '']
    lines += ['## 配对变化', '', '同一轮次计算 (实验/对照−1)×100，再取中位数与范围。吞吐正值更好；延迟、CPU 和流量负值更好。范围不是置信区间。', '',
              '| 阶段 | 实验 | 对照 | 指标 | 中位数 % | 最小 % | 最大 % |', '|---|---|---|---|---:|---:|---:|']
    for phase, records in comparisons.items():
        for r in records:
            lines.append(f"| {phase} | {r['case']} | {r['reference']} | {r['metric']} | {r['median_change_pct']:+.2f} | {r['min_change_pct']:+.2f} | {r['max_change_pct']:+.2f} |")
    lines += ['', '![三阶段概览](overview.png)', '', '![实际采样率与成本](sampling-cost.png)', '',
              '采样周期的 before/after 快照见 [sampling-frequencies.csv](sampling-frequencies.csv)。端点相同不能证明区间内周期从未改变。',
              '数据仅覆盖本次 Memcached 工作集及参数，不推论其他应用或容量。自适应同时改变 load/store 事件周期，不能与固定 store 周期组当作单变量比较。']
    (root / 'SUMMARY.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({'rows': counts, 'report': str(root / 'SUMMARY.md')}))


if __name__ == '__main__':
    main()
