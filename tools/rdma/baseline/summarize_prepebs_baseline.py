#!/usr/bin/env python3
"""Summarize archived baseline CSVs without conflating folio counters with WRs."""
import argparse
import csv
import json
import os
from pathlib import Path
import statistics

p = argparse.ArgumentParser()
p.add_argument('root', type=Path)
p.add_argument('--extra-results', type=Path, action='append', default=[],
               help='Additional results/ directory from an isolated continuation; raw files remain separate')
args = p.parse_args()
root = args.root
metrics = ['get_qps', 'normalized_read_amplification', 'throughput_ops', 'achieved_qps', 'useful_gib_per_sec',
           'accessed_scan_gib_per_sec', 'workset_scan_gib_per_sec',
           'measured_read_amplification', 'load_protocol_gib_per_sec', 'train_sec', 'train_metric_value',
           'read_avg_us', 'read_p95_us', 'read_p99_us', 'read_p99',
           'miss_rate_pct', 'skipped_txs_pct', 'wait_before_sec', 'pswpin_delta', 'pswpout_delta',
           'large_store_pct', 'large_load_pct', 'remote_bytes_per_useful_byte']
groups = {}
issues = []
observations = []
files = []
excluded_files = []
memcached_audit = []
def order_counters(path):
    return {int(parts[0]): [int(v) for v in parts[1:]] for line in path.read_text().splitlines()
            if (parts := line.split()) and parts[0].isdigit() and len(parts) == 6}
def key_values(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)
result_roots = [root / 'results', *args.extra_results]
def relative(path):
    return os.path.relpath(path, root)
paths = [(source, path) for source in result_roots for path in sorted(source.rglob('*.csv'))]
for source, path in paths:
    if path.name not in {'swapio-summary.csv', 'redis-swapio-summary.csv',
                         'ycsb-swapio-summary.csv', 'xgboost-swapio-summary.csv',
                         'mutilate_load_vs_latency.csv'}:
        continue
    suite = path.relative_to(source).parts[0]
    rows = list(csv.DictReader(path.open()))
    if path.name == 'mutilate_load_vs_latency.csv':
        config = dict(line.split('=', 1) for line in
            (path.parent / 'page-config.txt').read_text().splitlines() if '=' in line)
        for row in rows:
            row['page_kb'] = config['page_kb']
        if suite == 'memcached-tuned':
            stage_file = source.parent / 'stages.tsv'
            page_stages = list(csv.DictReader(stage_file.open(), delimiter='\t')) if stage_file.exists() else []
            matches = [r for r in page_stages if r['page_kb'] == config['page_kb'] and r['exit_code'] == '0']
            if len(matches) != 1 or len(rows) != 9:
                excluded_files.append({'file':relative(path),'rows':len(rows),
                    'reason':'tuned page has not completed all nine samples with exit code 0'})
                continue
    if suite == 'memcached-tuned':
        old = order_counters(path.parent / 'page-state-before.txt')
        new = order_counters(path.parent / 'page-state-after.txt')
        load_bytes = sum((v[2] - old[k][2]) * v[0] for k, v in new.items())
        large_bytes = sum((v[2] - old[k][2]) * v[0] for k, v in new.items() if k > 0)
        errors = sum(v[-1] - old[k][-1] for k, v in new.items())
        sample_issues = []
        if errors:
            sample_issues.append(f'phase backend errors={errors}')
        for row in rows:
            for field in ['miss_rate_pct', 'skipped_txs_pct', 'memcached_evictions',
                          'hermit_swapout_backend_store_errors_delta',
                          'hermit_swapout_large_folio_fallbacks_delta']:
                if float(row[field]):
                    sample_issues.append(f'{field}={row[field]}')
            suffix = f"{row['offered_qps']}-r{row['repeat']}.kv"
            before = key_values(path.parent / f'counters-before-{suffix}')
            after = key_values(path.parent / f'counters-after-{suffix}')
            for field in ['cgroup_oom', 'cgroup_oom_kill']:
                if int(after[field]) != int(before[field]):
                    sample_issues.append(f'{suffix}: {field} changed')
        issues.extend(f'{relative(path)}: {issue}' for issue in sample_issues)
        memcached_audit.append(dict(page_kb=int(config['page_kb']), samples=len(rows),
            phase_large_load_pct=100 * large_bytes / load_bytes if load_bytes else None,
            phase_load_gib=load_bytes / 2**30, order_errors=errors,
            sample_issues=sample_issues, source=relative(path)))
    files.append({'file': relative(path), 'rows': len(rows)})
    for index, row in enumerate(rows, 2):
        # Preserve distinct sparsity/load settings rather than pooling them.
        condition = '|'.join(f'{k}={row[k]}' for k in
            ['access_ratio', 'access_ratio_label', 'access_order', 'access_locality',
             'active_ratio_pct', 'active_ratio_requested', 'offered_qps', 'requested_qps', 'target_qps', 'load'] if row.get(k))
        key = (suite, row.get('page_kb', row.get('page_size_kb', '')), condition)
        groups.setdefault(key, []).append(row)
        for field in ['target_errors_delta', 'swapin_target_errors_delta', 'backend_errors_delta',
                      'hermit_swapout_backend_store_errors_delta', 'checksum_errors']:
            if row.get(field) and float(row[field]) != 0:
                issues.append(f'{relative(path)}:{index}: {field}={row[field]}')
        if row.get('read_ok') and row.get('read_ops') and row['read_ok'] != row['read_ops']:
            issues.append(f'{relative(path)}:{index}: YCSB read failures')
        if any(row.get(k) and float(row[k]) <= 0 for k in ['pswpin_delta', 'swapin_pswpin_delta']):
            issues.append(f'{relative(path)}:{index}: no swap-in')
        for field in ['target_fallback_delta', 'swapin_target_fallback_delta',
                      'hermit_swapout_large_folio_fallbacks_delta']:
            if row.get(field) and float(row[field]) != 0:
                observations.append(f'{relative(path)}:{index}: {field}={row[field]}')
        if row.get('wait_status') and row['wait_status'] != 'stable':
            observations.append(f'{relative(path)}:{index}: wait_status={row["wait_status"]}')
summary = []
# Check every order, including errors outside the requested folio size.
for after in sorted(path for source in result_roots for path in source.rglob('order-after*.txt')):
    before = after.with_name(after.name.replace('order-after', 'order-before', 1))
    if not before.exists():
        continue
    old, new = order_counters(before), order_counters(after)
    for order, values in new.items():
        if order in old and values[-1] > old[order][-1]:
            issues.append(f'{relative(after)}: order {order} backend errors increased')
for path in sorted(path for source in result_roots for path in source.rglob('memory-events*.txt')):
    events = dict(line.split() for line in path.read_text().splitlines() if len(line.split()) == 2)
    if int(events.get('oom_kill', 0)):
        issues.append(f'{relative(path)}: oom_kill={events["oom_kill"]}')
before_log = root / 'state/dmesg-before.txt'
seen_lines = set(before_log.read_text().splitlines()) if before_log.exists() else set()
kernel_events = []
for path in sorted(path for source in result_roots for path in (source.parent / 'state').glob('dmesg-after*.txt')):
    for line in path.read_text().splitlines():
        if line not in seen_lines and any(token in line for token in
                ['Memory cgroup out of memory:', 'BUG:', 'WARNING:', 'Oops:', 'soft lockup']):
            kernel_events.append({'file': relative(path), 'line': line})
            issues.append(f'{relative(path)}: {line}')
        seen_lines.add(line)
for (suite, page, condition), rows in sorted(groups.items(), key=lambda x: (x[0][0], int(x[0][1] or 0), x[0][2])):
    for metric in metrics:
        values = [float(r[metric]) for r in rows if r.get(metric) not in (None, '', 'NA', 'nan')]
        if not values:
            continue
        mean = statistics.mean(values)
        summary.append(dict(suite=suite, page_kb=page, condition=condition, metric=metric,
            n=len(values), median=statistics.median(values), mean=mean,
            stdev=statistics.stdev(values) if len(values)>1 else 0,
            min=min(values), max=max(values)))
with (root / 'aggregate.csv').open('w') as f:
    w = csv.DictWriter(f, fieldnames=['suite','page_kb','condition','metric','n','median','mean','stdev','min','max'])
    w.writeheader(); w.writerows(summary)
expected = {'anon-1t':54, 'anon-8t':54, 'redis-chunk64k':27,
            'ycsb-redis-full':27, 'xgboost-higgs':27, 'memcached':81}
if (root / 'plan.txt').exists() and 'profile=smoke' in (root / 'plan.txt').read_text():
    expected = {'anon-1t':6}
if any((source / 'memcached-tuned').exists() for source in result_roots):
    expected['memcached-tuned'] = 81
counts = {}
for (suite, _, _), rows in groups.items():
    counts[suite] = counts.get(suite, 0) + len(rows)
for suite, n in expected.items():
    if counts.get(suite, 0) != n:
        issues.append(f'{suite}: expected {n} rows, found {counts.get(suite, 0)}')
smoke = expected == {'anon-1t':6}
pages = {4, 64, 2048} if smoke else {4, 16, 32, 64, 128, 256, 512, 1024, 2048}
for suite in expected:
    suite_groups = [(page, condition, rows) for (s, page, condition), rows in groups.items() if s == suite]
    for page in pages:
        selected = [(condition, rows) for pg, condition, rows in suite_groups if int(pg) == page]
        nconditions = 2 if suite.startswith('anon-') else 3 if suite.startswith('memcached') else 1
        if len(selected) != nconditions:
            issues.append(f'{suite}/{page} KiB: expected {nconditions} conditions, found {len(selected)}')
        for condition, rows in selected:
            repeats = 1 if smoke else 3
            if len(rows) != repeats or {r.get('repeat') for r in rows} != {str(i) for i in range(1, repeats + 1)}:
                issues.append(f'{suite}/{page} KiB/{condition}: incomplete or duplicate repeats')
completion = root / 'completion.txt'
if not completion.exists() or 'exit_code=0\n' not in completion.read_text():
    issues.append('suite has not completed successfully')
stages = root / 'stages.tsv'
stage_rows = list(csv.DictReader(stages.open(), delimiter='\t')) if stages.exists() else []
for suite in expected:
    if suite == 'memcached-tuned':
        tuned_roots = [source.parent for source in result_roots if (source / suite).exists()]
        accepted_stages = []
        for tuned in tuned_roots:
            stage_file = tuned / 'stages.tsv'
            records = list(csv.DictReader(stage_file.open(), delimiter='\t')) if stage_file.exists() else []
            accepted_stages.extend(r for r in records if r['exit_code'] == '0')
        if len(accepted_stages) != len(pages) or {r['page_kb'] for r in accepted_stages} != {str(p) for p in pages}:
            issues.append('memcached-tuned: incomplete or duplicate successful page stages')
        continue
    matches = [r for r in stage_rows if r['suite'] == suite]
    if len(matches) != 1 or matches[0]['exit_code'] != '0':
        issues.append(f'{suite}: missing or unsuccessful stage completion')
run_status = []
for source in result_roots:
    run = source.parent
    before = run / 'state/dmesg-before.txt'
    known = set(before.read_text().splitlines()) if before.exists() else set()
    oom = set()
    for log in (run / 'state').glob('dmesg-after*.txt'):
        oom.update(line for line in log.read_text().splitlines()
                   if line not in known and 'Memory cgroup out of memory:' in line)
    done = run / 'completion.txt'
    stages = run / 'stages.tsv'
    run_status.append({'root':relative(run),
        'completion':dict(line.split('=', 1) for line in done.read_text().splitlines() if '=' in line) if done.exists() else None,
        'stages':list(csv.DictReader(stages.open(), delimiter='\t')) if stages.exists() else [],
        'new_oom_kills':len(oom)})
(root / 'memcached-tuned-audit.json').write_text(json.dumps(sorted(memcached_audit, key=lambda r: r['page_kb']), indent=2) + '\n')
(root / 'validation.json').write_text(json.dumps({'files':files,'excluded_files':excluded_files,'counts':counts,'issues':issues,
    'observations':observations,'kernel_events':kernel_events,'run_status':run_status}, indent=2)+'\n')
lines = ['# PEBS 前 Hermit baseline', '',
         '以下统计来自本目录和显式传入的续测目录；是否全部完成还须核对各目录的 stages.tsv 与 completion.txt。',
         'memcached 为默认回收配置，memcached-tuned 为独立的历史推荐回收配置；两者不合并取中位数。', '',
         '| 测试 | 页大小 KiB | 条件 | 指标 | n | 中位数 | 最小值 | 最大值 |',
         '|---|---:|---|---|---:|---:|---:|---:|']
for r in summary:
    if r['metric'] in {'get_qps','throughput_ops','achieved_qps','useful_gib_per_sec','accessed_scan_gib_per_sec','train_sec','read_p99_us'}:
        lines.append(f"| {r['suite']} | {r['page_kb']} | {r['condition']} | {r['metric']} | {r['n']} | {r['median']:.6g} | {r['min']:.6g} | {r['max']:.6g} |")
lines += ['', '## 数据解释', '',
    '- 页档同时控制 THP 分配策略和静态 remote_order_mask；不能把收益单独归因于传输大小。',
    '- order_stats 按 folio 记账；检查 large_*_pct、fallback 与 errors 后再解释大页收益。',
    '- 历史脚本用轮询估计写出耗时，短写出可能产生不可信的 protocol_gib_per_sec；本报告不把它作为主要指标。',
    '- XGBoost 的 load_protocol_gib_per_sec 用整个训练时间作分母，不代表 RDMA 链路峰值。',
    '- 本轮是 PEBS 前静态 Hermit baseline；不是 native Linux 对照，也不是 PEBS 开关对照。',
    '', '## 自动校验', '', *(issues or ['样本矩阵和阶段完成状态通过；已读取数据中未发现 checksum/backend error、OOM kill 或无 swap-in 行。']),
    '', f'另有 {len(observations)} 条 fallback 或非 stable 状态记录，详见 validation.json 的 observations。']
(root / 'summary.md').write_text('\n'.join(lines)+'\n')
print(json.dumps({'files':len(files),'groups':len(groups),'issues':len(issues)}))
