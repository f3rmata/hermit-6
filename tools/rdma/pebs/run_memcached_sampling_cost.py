#!/usr/bin/env python3
"""Compare PEBS sampling and policy at fixed concurrency, without peak calibration."""
import argparse
import csv
import json
from pathlib import Path
import statistics as st
import subprocess
import sys
import os

DRIVER = Path(__file__).with_name('pebs_perf_matrix.py')


def load_rows(folder):
    if (folder/'status').read_text().strip() != 'completed':
        raise RuntimeError(f'Incomplete measurement: {folder}')
    with (folder/'summary.csv').open() as f:
        rows = list(csv.DictReader(f))
    if not rows or any(int(r['valid']) != 1 for r in rows):
        raise RuntimeError(f'Invalid measurement: {folder}')
    return rows


def summarize(folder):
    rows=load_rows(folder)
    groups={case:[r for r in rows if r['case']==case] for case in dict.fromkeys(r['case'] for r in rows)}
    baseline={r['repeat']:r for r in groups['off-same-pages']}
    base4={r['repeat']:r for r in groups.get('off',[])}
    result=[]
    for case, samples in groups.items():
        reductions=[100*(1-float(r['qps'])/float(baseline[r['repeat']]['qps'])) for r in samples]
        qps=[float(r['qps']) for r in samples]
        gains4=[100*(float(r['qps'])/float(base4[r['repeat']]['qps'])-1) for r in samples] if base4 else []
        matched={r['repeat']:r for r in groups.get(case.replace('policy-','static-'),[])}
        strategy=[100*(float(r['qps'])/float(matched[r['repeat']]['qps'])-1) for r in samples] if case.startswith('policy-') and matched else []
        result.append(dict(case=case,qps_gain_vs_off_pct_median=st.median(gains4) if gains4 else None,
            qps_gain_vs_static_pct_median=st.median(strategy) if strategy else None,n=len(samples),qps_median=st.median(qps),qps_min=min(qps),qps_max=max(qps),
            paired_qps_loss_pct_median=st.median(reductions),paired_qps_loss_pct_min=min(reductions),paired_qps_loss_pct_max=max(reductions),
            read_p99_us_median=st.median(float(r['read_p99_us']) for r in samples),
            server_cpu_capacity_pct=st.median(float(r['server_cpu_capacity_pct']) for r in samples),
            client_cpu_capacity_pct=st.median(float(r['client_cpu_capacity_pct']) for r in samples),
            system_cpu_us_per_op=st.median(float(r['system_busy_cpu_seconds'])/int(r['total_requests'])*1e6 for r in samples),
            sampled_per_sec=st.median(float(r['samples_per_second']) for r in samples),
            lost_cases=sum(int(r['lost_delta'])>0 for r in samples),
            throttled_cases=sum(int(r['throttled_delta'])>0 for r in samples),
            perf_changed_cases=sum(int(r['perf_controls_changed'])>0 for r in samples)))
    rates=set()
    for path in folder.glob('r*/before/perf-controls.json'):
        rates.add(json.loads(path.read_text())['perf_event_max_sample_rate'])
    flags=['Fixed concurrency; no peak or saturation check was performed.', 'All cases preserve system page allocation; off is not a guaranteed 4 KiB baseline. Inspect pages-*/page-sizes.json for actual resident folio sizes.']
    if folder.parent.name=='resident' and any(int(r['read_bytes'])>0 or int(r['write_bytes'])>0 for r in rows):
        flags.append('Resident phase has RDMA traffic; cannot interpret as a no-paging sampling-cost test')
    if len(rates)>1:flags.append('Perf sample-rate cap differs between runs')
    if any(r['lost_cases'] or r['throttled_cases'] or r['perf_changed_cases'] for r in result):
        flags.append('Sampling quality changed or lost/throttled counters increased; inspect raw runs')
    if max(r['client_cpu_capacity_pct'] for r in result)>=85:
        flags.append('Client CPU is near capacity; client bottleneck is possible')
    with (folder/'sampling-cost.csv').open('w') as f:
        writer=csv.DictWriter(f,fieldnames=result[0]);writer.writeheader();writer.writerows(result)
    (folder/'sampling-cost.json').write_text(json.dumps(dict(rows=result,quality_flags=flags,perf_rate_caps=sorted(rates),load_selection='fixed-concurrency-no-calibration'),indent=2)+'\n')
    lines=['# Memcached 双基线与 PEBS 采样和策略对照', '', '固定并发对照；未执行峰值或满载校准。', '',
           '相对同页基线的吞吐损失 = 100 × (1 − 当前配置 QPS / 同一重复的 off-same-pages QPS)。正值为衰减；只有 static 行可称为采样开销，policy 行为采样加策略的净效果。范围不是置信区间。', '',
           '| 配置 | QPS 中位数 | 配对吞吐损失 % [min, max] | read p99 µs | 服务端 CPU % | 客户端 CPU % |',
           '|---|---:|---:|---:|---:|---:|']
    for r in result:
        lines.append(f"| {r['case']} | {r['qps_median']:.0f} | {r['paired_qps_loss_pct_median']:.2f} [{r['paired_qps_loss_pct_min']:.2f}, {r['paired_qps_loss_pct_max']:.2f}] | {r['read_p99_us_median']:.1f} | {r['server_cpu_capacity_pct']:.1f} | {r['client_cpu_capacity_pct']:.1f} |")
    lines+=['','| 配置 | 相对 off（自然页分配） 的吞吐提升 % | 相对同频 static 的策略提升 % |','|---|---:|---:|']
    for r in result:
        fmt=lambda v: '—' if v is None else f'{v:+.2f}'
        lines.append(f"| {r['case']} | {fmt(r['qps_gain_vs_off_pct_median'])} | {fmt(r['qps_gain_vs_static_pct_median'])} |")
    lines+=['','hermit_pebsd 与服务端同插槽绑核；硬件 PEBS 事件仍覆盖所有在线 CPU（包括客户端），故开销包含全局采样和处理成本。CPU 百分比按各自绑定的逻辑 CPU 数归一化。吞吐平台不保证服务端全部核心满忙；锁、网络或 RDMA 等瓶颈仍可能限制吞吐。', '', '质量提示：'+'；'.join(flags or ['未触发自动质量提示，仍需检查逐核利用率及重复波动。'])]
    (folder/'SAMPLING-COST.md').write_text('\n'.join(lines)+'\n')
    return result,flags


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--user',required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--server-cpus',default='0-7',help='dnet-61 physical cores on NUMA node 0')
    p.add_argument('--client-cpus',default='16-31',help='dnet-61 physical cores on NUMA node 1')
    p.add_argument('--sampler-cpus',help='default shares Memcached CPUs; must be on same socket')
    p.add_argument('--phases',nargs='+',choices=['resident','pressure'],default=['resident','pressure'])
    p.add_argument('--connections',type=int,default=32,help='Fixed client connection parameter for all cases (default: 32); no calibration')
    p.add_argument('--repeats',type=int,default=5)
    p.add_argument('--duration',type=int,default=120)
    p.add_argument('--warmup',type=int,default=30)
    p.add_argument('--dry-run',action='store_true')
    args=p.parse_args()
    if args.connections<1:p.error('connections must be positive')
    if min(args.repeats,args.duration,args.warmup)<=0:p.error('repeats/duration/warmup must be positive')
    if len(set(args.phases))!=len(args.phases):p.error('duplicate phases')
    base=[sys.executable,str(DRIVER),'--user',args.user,'--server-cpus',args.server_cpus,
          '--client-cpus',args.client_cpus,'--thp','preserve',
          '--records','800000','--qps','0','--require-fixed']
    base+=['--sampler-cpus',args.sampler_cpus or args.server_cpus]
    def command(phase):
        return base+['--memory-mb',str(2048 if phase=='resident' else 640),
            '--output',str(args.output/phase/'measurement'),'--connections',str(args.connections),
            '--cases','off,off-same-pages,static-low,static-medium,static-high,policy-low,policy-medium,policy-high',
            '--repeats',str(args.repeats),'--duration',str(args.duration),'--warmup',str(args.warmup)]
    if args.dry_run:
        print(json.dumps({phase:{'measurement':command(phase),
            'selection':'fixed-concurrency-no-calibration'} for phase in args.phases},indent=2))
        return
    if os.geteuid()!=0:p.error('run with sudo; benchmarks drop privileges to --user')
    if args.output.exists():p.error('output must be a new directory')
    args.output.mkdir(parents=True)
    (args.output/'plan.json').write_text(json.dumps(vars(args)|{
        'output':str(args.output),'load_selection':'fixed-concurrency-no-calibration'},indent=2)+'\n')
    for phase in args.phases:
        (args.output/phase).mkdir(exist_ok=True)
        print(f'{phase}: fixed connections={args.connections}; no peak calibration',flush=True)
        subprocess.run(command(phase),check=True)
        summarize(args.output/phase/'measurement')
    (args.output/'status').write_text('completed\n')


if __name__ == '__main__': main()
