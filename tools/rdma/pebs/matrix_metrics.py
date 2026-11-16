"""Counter formats shared by the application matrix drivers."""
import csv
import re
from pathlib import Path

WR_FIELDS = ('write_wrs', 'read_wrs', 'write_bytes', 'read_bytes')
POLICY_FIELDS = ('observed_4k', 'cold_observations', 'invalid_epochs', 'late_samples',
                 'predicted_4k', 'refault_4k', 'ranked_candidates')
PAGE_SIZES = (4, 16, 32, 64, 128, 256, 512, 1024, 2048)

# Named policy_features ablations. Each preset only clears bits from the
# default mask, so one matrix can compare the full policy against a single
# disabled mechanism and still pair every policy cell with that block's
# off/static cells. Values follow HERMIT_POLICY_* in include/linux/hermit_policy.h.
POLICY_MASKS = {
    'default': 0x1bf,   # SWAPIN|PEBS|SORT|OBSERVE|FLUSH|BATCH|LARGE|MGLRU
    'no-pebs': 0x1bd,   # PEBS footprint vote removed
    'no-batch': 0x19f,  # SG writeout of cold dirty pages removed
    'medium-on': 0x3bf,  # default + MEDIUM (fit-driven 16-64 KiB swap-in orders)
}
DEFAULT_POLICY_MASK = POLICY_MASKS['default']
# Bits 0..9 exist; 0x400 is the next free bit.
POLICY_MASK_MAX = 0x3ff


def policy_mask(value):
    """Resolve a preset name, decimal mask or 0x mask to (label, mask)."""
    text = str(value).strip()
    if text in POLICY_MASKS:
        return text, POLICY_MASKS[text]
    try:
        mask = int(text, 0)
    except ValueError:
        raise ValueError(f'unknown policy_features preset or mask {value!r}; '
                         f'known presets: {", ".join(POLICY_MASKS)}') from None
    if mask < 0 or mask > POLICY_MASK_MAX:
        raise ValueError(f'policy_features mask {value!r} is outside '
                         f'0..{POLICY_MASK_MAX:#x}')
    return 'f%x' % mask, mask


def policy_masks(values):
    """Ordered unique [(label, mask)] from --policy-features values.

    Accepts preset names, raw masks, and comma-separated combinations, so
    ``--policy-features default no-pebs`` and ``--policy-features default,no-pebs``
    are equivalent.
    """
    result = []
    for value in values or ():
        for item in str(value).split(','):
            if not item.strip():
                continue
            entry = policy_mask(item)
            if entry not in result:
                result.append(entry)
    if not result:
        raise ValueError('no policy_features preset selected')
    return result


def policy_suffix(label):
    """Case-label suffix; the default mask keeps the historical case names."""
    return '' if label in (None, 'default') else '-' + label


def counter_delta(before, after):
    if before.keys() != after.keys():
        raise ValueError('counter schema changed')
    result = {k: after[k] - before[k] for k in before}
    if any(v < 0 for v in result.values()):
        raise ValueError('counter reset during measurement')
    return result


def wr_sections(text):
    """Keep order and page-count namespaces distinct, including identical sizes."""
    result, section = {}, None
    seen = set()
    for line in text.splitlines():
        cells = line.split()
        if not cells:
            continue
        if cells[0] in ('order', 'pages'):
            section = cells[0]
            prefix = 'batch_' if section == 'pages' else ''
            if cells != [section, *(prefix + f for f in WR_FIELDS)] or section in seen:
                raise ValueError('invalid or duplicate WR table header')
            seen.add(section)
            continue
        if section is None or len(cells) != 5 or not all(c.isdigit() for c in cells):
            raise ValueError('malformed WR counter row')
        index = int(cells[0])
        if (section == 'order' and index > 9) or (section == 'pages' and index < 1):
            raise ValueError('invalid WR size')
        key = (section, index)
        if key in result:
            raise ValueError('duplicate WR counter row')
        result[key] = dict(zip(WR_FIELDS, map(int, cells[1:])))
    if 'order' not in seen or not result:
        raise ValueError('missing WR order table')
    return result


def wr_delta(before, after):
    a, b = wr_sections(before), wr_sections(after)
    if a.keys() != b.keys():
        raise ValueError('WR table schema changed')
    result = {}
    for (section, index), values in b.items():
        delta = counter_delta(a[section, index], values)
        size = (4 << index) if section == 'order' else 4 * index
        row = result.setdefault(size, dict.fromkeys(
            (*WR_FIELDS, *('batch_' + f for f in WR_FIELDS)), 0))
        for name, value in delta.items():
            row[name] += value
            if section == 'pages':
                row['batch_' + name] += value
    return result


def policy_counters(text):
    result = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit() or parts[0] in result:
            raise ValueError('malformed policy_stats')
        result[parts[0]] = int(parts[1])
    if not set(POLICY_FIELDS) <= result.keys():
        raise ValueError('missing base-page policy counters')
    return result


def policy_delta(before, after):
    """Absent on legacy kernels; a missing half of a snapshot pair is an error."""
    if before is None and after is None:
        return None
    if before is None or after is None:
        raise ValueError('missing policy_stats snapshot')
    return counter_delta(policy_counters(before), policy_counters(after))


def policy_activity(mode, ceiling, decisions, policy, wr):
    if policy is None:
        required = mode == 1 and ceiling > 4
        activity = dict(required=required, application_decisions=decisions,
                        path_exercised=decisions > 0 if decisions is not None else None)
        issues = []
        if required and decisions is None:
            issues.append('missing application-phase PEBS decision counters')
        elif required and decisions == 0:
            issues.append('policy selection was not exercised: zero application-phase decisions')
        return activity, issues
    # Even a single-page batch proves the new swap-in path ran. Requiring
    # speculative pages or multi-page WRs would reject a correct cold fallback.
    reads = sum(r['read_wrs'] for r in wr.values())
    writes = sum(r['write_wrs'] for r in wr.values())
    batch_reads = sum(r.get('batch_read_wrs', 0) for r in wr.values())
    evidence = batch_reads + sum(policy.get(k, 0) for k in
                                ('observed_4k', 'predicted_4k', 'ranked_candidates',
                                 'mglru_candidates'))
    required = mode == 1 and reads + writes > 0
    activity = dict(required=required, application_decisions=decisions,
                    path_exercised=mode == 1 and evidence > 0,
                    evidence_source='base-page-v1', policy_counters=policy,
                    batch_read_wrs=batch_reads, read_wrs=reads, write_wrs=writes)
    issues = []
    if required and not activity['path_exercised']:
        issues.append('remote I/O without application-phase base-page policy activity')
    return activity, issues


def page_ceiling_settings(kb, saved):
    if kb not in PAGE_SIZES:
        raise ValueError('unsupported allocation ceiling')
    sizes = {int(m[1]) for p in saved if (m := re.search(r'hugepages-(\d+)kB/enabled$', p))}
    if kb not in (4, 2048) and kb not in sizes:
        raise ValueError(f'THP size {kb} KiB is unavailable')
    result = {}
    for path in saved:
        match = re.search(r'hugepages-(\d+)kB/enabled$', path)
        size = int(match[1]) if match else 2048
        result[path] = 'always' if 16 <= size <= kb else 'never'
    return result


def read_optional(path):
    path = Path(path)
    return path.read_text() if path.exists() else None


def write_comparisons(output):
    """Reuse the 4 KiB/off cell as an explicit baseline, without another run."""
    path = Path(output) / 'summary.csv'
    if not path.exists():
        return
    with path.open() as f:
        rows = list(csv.DictReader(f))
    def key(row, page=None):
        return (row.get('suite', 'memcached'), row['repeat'],
                row['max_page_kb'] if page is None else str(page))
    baselines = {key(r): r for r in rows if r['case'] == 'off'}
    def valid(row):
        return row is not None and row.get('usable', row.get('valid')) in ('True', '1')
    def number(row, metric):
        value = row.get(metric)
        if metric == 'throughput':
            value = row.get('throughput', row.get('qps'))
        return float(value) if value not in (None, '') else None
    paired = []
    for row in rows:
        for name, base_key in [('off_same_page', key(row)), ('4KiB-only', key(row, 4))]:
            if not row['max_page_kb'] and name == '4KiB-only':
                continue  # Preserve-system runs have no defined page ceiling.
            base = baselines.get(base_key)
            result = dict(suite=key(row)[0], repeat=row['repeat'], max_page_kb=row['max_page_kb'],
                          case=row['case'], baseline=name, baseline_page_kb=base_key[2],
                          baseline_status='missing' if base is None else 'ok' if valid(base) else 'invalid',
                          valid_pair=valid(row) and valid(base))
            for metric in ('throughput', 'read_p99_us', 'read_wrs', 'write_wrs',
                           'read_bytes', 'write_bytes', 'pswpin', 'pswpout'):
                a, b = (number(base, metric), number(row, metric)) if result['valid_pair'] else (None, None)
                result['baseline_' + metric], result['case_' + metric] = a, b
                field = metric + ('_gain_pct' if metric == 'throughput' else '_reduction_pct')
                result[field] = ((b / a - 1) if metric == 'throughput' else (1 - b / a)) * 100 if a is not None and a > 0 and b is not None else None
            paired.append(result)
    if paired:
        with (Path(output) / 'paired.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=paired[0])
            writer.writeheader()
            writer.writerows(paired)
