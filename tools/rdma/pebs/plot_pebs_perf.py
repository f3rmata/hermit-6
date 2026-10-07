#!/usr/bin/env python3
"""Validate and summarize measured PEBS matrices, including ineffective sampling."""
import argparse,csv,json,statistics,re
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
p=argparse.ArgumentParser();p.add_argument('run',type=Path);p.add_argument('--allow-partial',action='store_true');a=p.parse_args()
manifest=json.loads((a.run/'manifest.json').read_text())
rows=list(csv.DictReader((a.run/'summary.csv').open()))
if not rows:raise ValueError('no measurements')
issues=[]
if not (a.run/'status').exists() or (a.run/'status').read_text().strip()!='completed':issues.append('run is not completed')
expected=[x[0] for x in manifest['cases']]
repeats=manifest['args']['repeats']
if len(rows)!=len(expected)*repeats:issues.append('unexpected total row count')
if set(r['case'] for r in rows)-set(expected):issues.append('unexpected case names')
for c in expected:
 selected=[r for r in rows if r['case']==c]
 if len(selected)!=repeats or {int(r['repeat']) for r in selected}!=set(range(1,repeats+1)):issues.append(c+': incomplete or duplicate repeats')
for r in rows:
 if r.get('valid')!='1':issues.append(f"{r['case']}/{r['repeat']}: invalid sample")
 run=a.run/f"r{r['repeat']}-{r['case']}"
 raw=(run/'measure.log').read_text()
 for label in ['Misses','Skipped TXs']:
  count=re.search(r'^'+label+r' = (\d+)',raw,re.M)
  if not count or int(count[1]):issues.append(f"{r['case']}/{r['repeat']}: nonzero or missing {label} count")
 for key in ['oom_delta','oom_kill_total','order_errors_delta','memcached_evictions']:
  if float(r[key]):issues.append(f"{r['case']}/{r['repeat']}: {key} is nonzero")
 if r['case'].startswith(('static','policy')) and float(r['sampled_delta'])<=0:issues.append(f"{r['case']}/{r['repeat']}: no usable PEBS samples")
restore=a.run/'restore.json'
if restore.exists():
 saved=json.loads(restore.read_text())
 if saved.get('cleanup_errors') or saved['pebs_enabled']!='0':issues.append('cleanup failed')
 if saved.get('controls')!=manifest['controls_before']:issues.append('controls were not restored')
 if saved.get('thp')!={k:re.search(r'\[([^]]+)\]',v)[1] for k,v in json.loads((a.run/'thp.json').read_text()).items()}:issues.append('THP settings were not restored')
else:issues.append('restoration is not recorded')
old=set((a.run/'dmesg-before.txt').read_text().splitlines())
new=(a.run/'dmesg-after.txt')
if not new.exists():issues.append('final kernel log is missing')
events=[l for l in new.read_text().splitlines() if l not in old and any(t in l for t in ['Memory cgroup out of memory:','BUG:','WARNING:','Oops:','soft lockup'])] if new.exists() else []
issues.extend(events)
perf_adjustments=[l for l in new.read_text().splitlines() if l not in old and 'perf: interrupt took too long' in l] if new.exists() else []
(a.run/'validation.json').write_text(json.dumps({'issues':issues,'rows':len(rows),'expected_rows':len(expected)*repeats,'kernel_events':events,'perf_rate_adjustments':perf_adjustments},indent=2)+'\n')
if issues and not a.allow_partial:raise ValueError('validation failed: '+str(issues))
for r in rows:
 seconds=float(r.get('client_seconds') or manifest['args']['duration'])
 requests=float(r.get('total_requests') or float(r['qps'])*seconds)
 for field in ['server_cpu_seconds','sampler_cpu_seconds','system_busy_cpu_seconds']:
  r[field.replace('_seconds','_cores')]=float(r[field])/seconds
  r[field.replace('_seconds','_us_per_op')]=float(r[field])*1e6/requests
 r['rdma_read_kib_per_op']=float(r['read_bytes'])/1024/requests
 r['rdma_write_kib_per_op']=float(r['write_bytes'])/1024/requests
 r['decisions_per_second']=sum(float(r[f'decisions_order{i}']) for i in range(10))/seconds
 r['unknown_pct']=100*float(r['unknown_delta'])/max(float(r['unknown_delta'])+sum(float(r[f'decisions_order{i}']) for i in range(10)),1)
case_order=['off-original-mask','off','static-low','policy-low','static-medium','policy-medium','static-high','policy-high','static-adaptive','policy-adaptive']
cases=[c for c in case_order if any(r['case']==c for r in rows)]
metrics=['qps','read_p99_us','update_p99_us','server_cpu_cores','sampler_cpu_cores','system_busy_cpu_cores','server_cpu_us_per_op','sampler_cpu_us_per_op','system_busy_cpu_us_per_op','samples_per_second','rdma_read_kib_per_op','rdma_write_kib_per_op','large_load_pct','large_wr_read_pct','large_wr_write_pct','decisions_per_second','unknown_pct','lost_delta','throttled_delta']
summary={}
for c in cases:
 summary[c]={}
 for m in metrics:
  values=[float(r[m]) for r in rows if r['case']==c]
  summary[c][m]={'n':len(values),'median':statistics.median(values),'mean':statistics.mean(values),'stdev':statistics.stdev(values) if len(values)>1 else 0,'min':min(values),'max':max(values)}
(a.run/'aggregate.json').write_text(json.dumps(summary,indent=2)+'\n')
with (a.run/'aggregate.csv').open('w') as f:
 w=csv.DictWriter(f,fieldnames=['case','metric','n','median','mean','stdev','min','max']);w.writeheader()
 for c in cases:
  for m,v in summary[c].items():w.writerow({'case':c,'metric':m,**v})
colors=['#777777' if c.startswith('off') else '#377eb8' if c.startswith('static') else '#e07a22' for c in cases]
plot_metrics=[('qps','Achieved QPS'),('read_p99_us','Read p99 (us, log scale)'),('system_busy_cpu_cores','Whole-system busy CPU (cores)'),('sampler_cpu_cores','Sampler thread CPU (cores)'),('samples_per_second','Usable samples / second (system-wide)'),('rdma_read_kib_per_op','RDMA read KiB / operation'),('large_wr_read_pct','Large WR share of read bytes (%)'),('decisions_per_second','Policy decisions / second')]
fig,axes=plt.subplots(4,2,figsize=(15,16),layout='constrained')
for ax,(m,title) in zip(axes.flat,plot_metrics):
 med=[summary[c][m]['median'] for c in cases]
 err=[[med[i]-summary[c][m]['min'] for i,c in enumerate(cases)],[summary[c][m]['max']-med[i] for i,c in enumerate(cases)]]
 ax.bar(range(len(cases)),med,yerr=err,color=colors,capsize=3)
 if m=='read_p99_us':ax.set_yscale('log')
 ax.set_xticks(range(len(cases)),cases,rotation=45,ha='right',fontsize=8);ax.set_title(title);ax.grid(axis='y',alpha=.2)
fig.suptitle(f"PEBS performance | memory.high {manifest['args']['memory_mb']} MiB | {manifest['args']['page_kb']} KiB folios\nMedians and min–max; blue: sampling only, orange: policy; n={repeats}" + (' | INCOMPLETE/INVALID' if issues else ''))
fig.savefig(a.run/'performance.png',dpi=150);fig.savefig(a.run/'performance.pdf');plt.close(fig)
comparisons=[]
for c in cases:
 refs=[]
 if c=='off':refs=[('static_mask_effect','off-original-mask')]
 elif c.startswith('static'):refs=[('sampling_vs_off','off')]
 elif c.startswith('policy'):refs=[('policy_vs_sampling','static'+c[len('policy'):]),('net_vs_off','off'),('net_vs_4k','off-original-mask')]
 for kind,ref in refs:
  if ref not in cases:continue
  for m in ['qps','read_p99_us','system_busy_cpu_us_per_op','server_cpu_us_per_op','rdma_read_kib_per_op','rdma_write_kib_per_op']:
   vals=[]
   for repeat in range(1,repeats+1):
    x=[r for r in rows if r['case']==c and int(r['repeat'])==repeat];y=[r for r in rows if r['case']==ref and int(r['repeat'])==repeat]
    if len(x)==len(y)==1 and float(y[0][m])>0:vals.append(100*(float(x[0][m])/float(y[0][m])-1))
   if vals:comparisons.append({'kind':kind,'case':c,'reference':ref,'metric':m,'n':len(vals),'median_change_pct':statistics.median(vals),'min_change_pct':min(vals),'max_change_pct':max(vals),'paired_changes_pct':vals})
(a.run/'comparisons.json').write_text(json.dumps(comparisons,indent=2)+'\n')
lines=['# PEBS 实机性能结果','',f'完成 {len(rows)} 行，计划 {len(expected)*repeats} 行。验收问题 {len(issues)} 条。中位数及最小到最大范围；配对变化按同轮次计算。','', '| 配置 | n | QPS | read p99 µs | 系统忙碌 CPU 核 | 采样线程 CPU 核 | 样本每秒 | 决策每秒 |','|---|---:|---:|---:|---:|---:|---:|---:|']
for c in cases:lines.append('| '+c+f" | {summary[c]['qps']['n']} | "+' | '.join(f'{summary[c][m]["median"]:.3f}' for m in ['qps','read_p99_us','system_busy_cpu_cores','sampler_cpu_cores','samples_per_second','decisions_per_second'])+' |')
lines+=['','变化百分比为 (实验/对照−1)×100：吞吐正值更好；延迟和单位请求 CPU 负值更好。','', '| 实验 | 对照 | 指标 | 配对变化中位数 % | 最小 % | 最大 % |','|---|---|---|---:|---:|---:|']
for r in comparisons:lines.append(f"| {r['case']} | {r['reference']} | {r['metric']} | {r['median_change_pct']:+.2f} | {r['min_change_pct']:+.2f} | {r['max_change_pct']:+.2f} |")
lines+=['','固定负载的 QPS 不代表吞吐上限。采样计数覆盖全机可用地址样本，不能等同于目标 memcg 的样本数；采样线程 CPU 不包含全部中断和内核开销。策略收益须结合实际 decisions 与 WR 分布判断。少量重复及 min–max 不构成统计显著性结论。','', '![性能对比](performance.png)']
if perf_adjustments:
 lines+=['',f'内核另外记录 {len(perf_adjustments)} 次 perf 中断耗时过长并降低全局采样速率上限；这与 PEBS ring 的 lost/throttled 记录是不同指标。完整日志见 dmesg-before.txt、dmesg-after.txt，不能声称 perf 未进行任何限速调整。']
(a.run/'REPORT.md').write_text('\n'.join(lines)+'\n');print(json.dumps({'rows':len(rows),'issues':issues,'report':str(a.run/'REPORT.md')}))
