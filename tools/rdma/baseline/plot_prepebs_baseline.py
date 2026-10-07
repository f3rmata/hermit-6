#!/usr/bin/env python3
"""Render median/min/max baseline plots from aggregate.csv."""
import argparse
import csv
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
p=argparse.ArgumentParser()
p.add_argument('root',type=Path)
a=p.parse_args()
rows=list(csv.DictReader((a.root/'aggregate.csv').open()))
fig,axes=plt.subplots(2,3,figsize=(15,8.5),layout='constrained')
def curve(ax,suite,metric,label,condition=None):
    r=[x for x in rows if x['suite']==suite and x['metric']==metric and
       (condition is None or condition in x['condition'])]
    if not r:return
    r.sort(key=lambda x:int(x['page_kb']))
    x=[int(v['page_kb']) for v in r]; y=[float(v['median']) for v in r]
    lo=[float(v['min']) for v in r]; hi=[float(v['max']) for v in r]
    if suite.startswith('memcached'):
        ax.errorbar(x, y, yerr=[[v-l for v,l in zip(y,lo)], [h-v for v,h in zip(y,hi)]],
                    fmt='o',label=label,markersize=4,capsize=3)
        for px,py,row in zip(x,y,r):
            if suite == 'memcached' or int(row['n']) != 3:
                ax.annotate(f"n={row['n']}",(px,py),xytext=(0,7),textcoords='offset points',ha='center',fontsize=7)
    else:
        ax.plot(x,y,'o-',label=label,markersize=4)
        ax.fill_between(x,lo,hi,alpha=.15)
for ax,ratio,title in [(axes[0,0],'100','Anon: full scan'),(axes[0,1],'chunk64k','Anon: 64 KiB chunks')]:
    for t in (1,8):curve(ax,f'anon-{t}t','accessed_scan_gib_per_sec',f'{t} thread' + ('s' if t > 1 else ''),f'access_ratio={ratio}|')
    ax.set_title(title);ax.set_ylabel('Useful scan GiB/s')
curve(axes[0,2],'redis-chunk64k','get_qps','GETRANGE 64 KiB')
axes[0,2].set_title('Redis: 2 MiB values');axes[0,2].set_ylabel('GET/s')
curve(axes[1,0],'ycsb-redis-full','throughput_ops','Whole-value READ')
axes[1,0].set_title('YCSB Redis: uniform reads');axes[1,0].set_ylabel('Operations/s')
curve(axes[1,1],'xgboost-higgs','train_sec','HIGGS, 30 rounds')
axes[1,1].set_title('XGBoost (lower is better)');axes[1,1].set_ylabel('Training seconds')
mc_suite = 'memcached-tuned' if any(r['suite'] == 'memcached-tuned' for r in rows) else 'memcached'
for load in (100000,250000,500000):curve(axes[1,2],mc_suite,'achieved_qps',f'Offered {load//1000}k',f'offered_qps={load}')
axes[1,2].set_title('Memcached: reclaim mode 1, headroom 256 MiB' if mc_suite == 'memcached-tuned' else
                    'Memcached: completed samples (OOM failures)')
axes[1,2].set_ylabel('Achieved QPS')
axes[1,2].margins(y=.15)
for ax in axes.flat:
    ax.set_xscale('log',base=2); ax.set_xticks([4,16,64,256,1024,2048],labels=['4','16','64','256','1024','2048'])
    ax.set_xlabel('Configured page size (KiB)');ax.grid(alpha=.25)
    if ax.lines:ax.legend(fontsize=8)
fig.suptitle('Pre-PEBS Hermit baseline — medians and min–max ranges\nPage setting changes allocation and transfer policy; Redis/YCSB at 2 MiB did not form large folios',fontsize=12)
fig.savefig(a.root/'baseline.png',dpi=180)
fig.savefig(a.root/'baseline.pdf')
