# encoding:utf-8
"""从 usage_ledger.jsonl 统计 Chat 协议的缓存命中率（严格口径）。

口径（统一按 OpenAI Chat Completions 协议）：
  - 命中字段：usage.prompt_tokens_details.cached_tokens（唯一认可的标准字段）
  - 分母：usage.prompt_tokens（Chat 协议下为含缓存命中在内的全量输入）
  - Token 命中率 = Σcached_tokens / Σprompt_tokens（只在字段合规的调用上累加）
  - 调用命中占比 = cached_tokens > 0 的调用数 / 字段合规的调用数

严格要求（任一不满足即判"不合规"）：
  - 每次调用（含流式末帧）都必须返回 usage；
  - usage.prompt_tokens_details 必须是对象，且 cached_tokens 必须为整数（未命中返回 0，不能缺省或 null）；
  - 0 <= cached_tokens <= prompt_tokens。
  非标准字段（如 prompt_cache_hit_tokens、cache_read_input_tokens）只记录出现次数，不计入命中。

用法：
  python3 cache_stats.py <数据集结果目录 | usage_ledger.jsonl> [--model 模型名]
"""
import argparse
import json
import os
import sys

LEDGER_NAME = 'usage_ledger.jsonl'
CHAT_API = 'OpenAICompatibleAPI'
CACHE_FIELD = 'usage.prompt_tokens_details.cached_tokens'

# 常见非标准缓存字段（只记录，不计入命中）
_NONSTANDARD_USAGE_KEYS = (
    'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens',
    'cache_read_input_tokens', 'cache_creation_input_tokens', 'cached_tokens',
)

# 按请求消息条数（对话深度）分桶
_DEPTH_BUCKETS = ((1, 1, '1'), (2, 4, '2-4'), (5, 10, '5-10'), (11, 20, '11-20'),
                  (21, 50, '21-50'), (51, 10 ** 9, '>50'))

_REASON_TEXT = {
    'usage_missing': '未返回 usage',
    'details_missing': 'usage 中缺少 prompt_tokens_details',
    'cached_missing': 'prompt_tokens_details.cached_tokens 缺失或非整数',
    'invalid': 'cached_tokens 越界（<0 或 > prompt_tokens）',
}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def ledger_path_of(path):
    return path if path.endswith('.jsonl') else os.path.join(path, LEDGER_NAME)


def load_ledger(path):
    """读取 ledger；文件不存在返回 None（区别于"有文件但无调用"的空列表）。"""
    path = ledger_path_of(path)
    if not os.path.isfile(path):
        return None
    records = []
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                records.append(rec)
    return records


def classify(usage):
    """返回 (status, prompt_tokens, cached_tokens)。status ∈ reported / usage_missing /
    details_missing / cached_missing / invalid。"""
    if not isinstance(usage, dict):
        return 'usage_missing', None, None
    prompt = usage.get('prompt_tokens')
    prompt = prompt if _is_int(prompt) else None
    details = usage.get('prompt_tokens_details')
    if not isinstance(details, dict):
        return 'details_missing', prompt, None
    cached = details.get('cached_tokens')
    if not _is_int(cached):
        return 'cached_missing', prompt, None
    if prompt is None or cached < 0 or cached > prompt:
        return 'invalid', prompt, cached
    return 'reported', prompt, cached


def _depth_label(n):
    if not _is_int(n) or n < 1:
        return 'unknown'
    for lo, hi, label in _DEPTH_BUCKETS:
        if lo <= n <= hi:
            return label
    return 'unknown'


def _new_acc():
    return {'calls': 0, 'reported_calls': 0, 'prompt_tokens': 0, 'cached_tokens': 0, 'hit_calls': 0}


def _finish_acc(acc):
    acc['hit_rate'] = (acc['cached_tokens'] / acc['prompt_tokens']) if acc['prompt_tokens'] else None
    acc['hit_call_ratio'] = (acc['hit_calls'] / acc['reported_calls']) if acc['reported_calls'] else None
    return acc


def _add(acc, status, prompt, cached):
    acc['calls'] += 1
    if status == 'reported':
        acc['reported_calls'] += 1
        acc['prompt_tokens'] += prompt
        acc['cached_tokens'] += cached
        if cached > 0:
            acc['hit_calls'] += 1


# 同一道题内被测模型的第 N 次调用（观察缓存预热：首轮必然未命中，之后应逐步命中）
_TURN_BUCKETS = ((1, 1, '第 1 次'), (2, 2, '第 2 次'), (3, 5, '第 3-5 次'), (6, 10, '第 6-10 次'),
                 (11, 20, '第 11-20 次'), (21, 10 ** 9, '第 21 次及以后'))

ROLE_LABELS = {
    'subject': '被测模型（答题 / Agent 轮次）',
    'aux': '同模型辅助调用（如 tau2 用户模拟器）',
    'unknown': '未归属到题目',
}


def _turn_label(n):
    for lo, hi, label in _TURN_BUCKETS:
        if lo <= n <= hi:
            return label
    return None


def sample_key(rec):
    """ledger 行 → (数据集名, sample_id)。数据集名与 reviews/<model>/<benchmark>_<subset>.jsonl 对齐。"""
    bm, subset, sid = rec.get('benchmark'), rec.get('subset'), rec.get('sample_id')
    if bm is None or sid is None:
        return None
    return (f'{bm}_{subset}' if subset not in (None, '') else str(bm), sid)


def _chat_rows(records, model_name):
    return [r for r in (records or []) if r.get('api') == CHAT_API
            and (not model_name or r.get('model') == model_name)]


def per_sample(records, model_name=None, role='subject'):
    """按题统计：{(数据集名, sample_id): acc}。role=None 表示该题全部同模型调用。
    acc 额外含 non_compliant（该题不合规调用数）。ledger 无题目归属时返回空 dict。"""
    out = {}
    for r in _chat_rows(records, model_name):
        key = sample_key(r)
        if key is None or (role and r.get('role') != role):
            continue
        status, prompt, cached = classify(r.get('usage'))
        acc = out.setdefault(key, dict(_new_acc(), non_compliant=0))
        _add(acc, status, prompt, cached)
        if status != 'reported':
            acc['non_compliant'] += 1
    for acc in out.values():
        _finish_acc(acc)
    return out


def _quantile(vals, q):
    vals = sorted(vals)
    if not vals:
        return None
    k = (len(vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def compute(records, model_name=None):
    """统计缓存命中。records 为 None 表示没有 ledger（未开启记录）。"""
    base = {'protocol': 'openai_chat', 'field': CACHE_FIELD, 'model': model_name}
    if records is None:
        return dict(base, verdict='no_data', calls=0,
                    verdict_reason='未找到 usage_ledger.jsonl（本次运行未开启 usage 记录）')

    rows = sorted(_chat_rows(records, model_name), key=lambda r: r.get('ts') or 0)

    total = _new_acc()
    fail = {k: 0 for k in _REASON_TEXT}
    nonstd = {}
    by_depth, by_tools, by_role, by_turn = {}, {}, {}, {}
    turn_counter = {}
    prompt_all = 0

    for r in rows:
        usage = r.get('usage')
        status, prompt, cached = classify(usage)
        if isinstance(usage, dict):
            for k in _NONSTANDARD_USAGE_KEYS:
                if k in usage:
                    nonstd[k] = nonstd.get(k, 0) + 1
        if prompt:
            prompt_all += prompt

        accs = [total,
                by_depth.setdefault(_depth_label(r.get('n_messages')), _new_acc())]
        n_tools = r.get('n_tools')
        accs.append(by_tools.setdefault(
            'with_tools' if _is_int(n_tools) and n_tools > 0 else 'without_tools', _new_acc()))
        role = r.get('role') if r.get('role') in ('subject', 'aux') else 'unknown'
        accs.append(by_role.setdefault(role, _new_acc()))
        key = sample_key(r)
        if role == 'subject' and key is not None:
            turn_counter[key] = turn_counter.get(key, 0) + 1
            label = _turn_label(turn_counter[key])
            if label:
                accs.append(by_turn.setdefault(label, _new_acc()))

        for acc in accs:
            _add(acc, status, prompt, cached)
        if status != 'reported':
            fail[status] += 1

    _finish_acc(total)

    samples = per_sample(rows, model_name, role='subject')
    rates = [a['hit_rate'] for a in samples.values() if a['hit_rate'] is not None]
    sample_dist = {
        'n_samples': len(samples),
        'n_with_rate': len(rates),
        'min': min(rates) if rates else None,
        'p10': _quantile(rates, 0.1),
        'p50': _quantile(rates, 0.5),
        'p90': _quantile(rates, 0.9),
        'max': max(rates) if rates else None,
        'mean': (sum(rates) / len(rates)) if rates else None,
        'zero_hit_samples': sum(1 for a in samples.values() if a['reported_calls'] and a['hit_calls'] == 0),
    }
    n = total['calls']
    non_compliant = n - total['reported_calls']
    if n == 0:
        verdict, reason = 'no_data', '未记录到被测模型的 Chat 调用'
    elif non_compliant:
        verdict = 'non_compliant'
        parts = [f'{_REASON_TEXT[k]} {v} 次' for k, v in fail.items() if v]
        reason = f'{non_compliant}/{n} 次调用缓存字段不合规：' + '；'.join(parts)
    else:
        verdict, reason = 'compliant', f'{n} 次调用均返回合规的 {CACHE_FIELD}'

    order = [label for _, _, label in _DEPTH_BUCKETS] + ['unknown']
    return dict(
        base,
        verdict=verdict,
        verdict_reason=reason,
        calls=n,
        reported_calls=total['reported_calls'],
        field_coverage=(total['reported_calls'] / n) if n else None,
        failures={k: v for k, v in fail.items() if v},
        prompt_tokens=total['prompt_tokens'],
        cached_tokens=total['cached_tokens'],
        prompt_tokens_all=prompt_all,
        hit_rate=total['hit_rate'],
        hit_calls=total['hit_calls'],
        hit_call_ratio=total['hit_call_ratio'],
        nonstandard_fields=nonstd,
        by_depth=[dict(_finish_acc(by_depth[k]), bucket=k) for k in order if k in by_depth],
        by_tools={k: _finish_acc(v) for k, v in by_tools.items()},
        by_role={k: _finish_acc(by_role[k]) for k in ('subject', 'aux', 'unknown') if k in by_role},
        by_turn=[dict(_finish_acc(by_turn[label]), bucket=label)
                 for _, _, label in _TURN_BUCKETS if label in by_turn],
        per_sample_dist=sample_dist,
    )


def compute_for_dirs(dirs, model_name=None):
    """合并多个数据集目录的 ledger 统计（用于整次运行汇总）。"""
    merged, found = [], False
    for d in dirs:
        recs = load_ledger(d)
        if recs is not None:
            found = True
            merged.extend(recs)
    return compute(merged if found else None, model_name)


def fmt_pct(v):
    return '—' if v is None else f'{v * 100:.2f}%'


def summary_line(stats):
    """一行文字摘要，用于控制台输出。"""
    if stats.get('verdict') == 'no_data':
        return f"缓存命中：无数据（{stats.get('verdict_reason')}）"
    line = (f"缓存命中率 {fmt_pct(stats.get('hit_rate'))}"
            f"（{stats.get('cached_tokens', 0):,} / {stats.get('prompt_tokens', 0):,} tokens）"
            f"｜命中调用 {stats.get('hit_calls', 0)}/{stats.get('reported_calls', 0)}"
            f"｜字段合规 {stats.get('reported_calls', 0)}/{stats.get('calls', 0)}")
    if stats.get('verdict') == 'non_compliant':
        line += f"｜❌ 不合规：{stats.get('verdict_reason')}"
    return line


def main():
    ap = argparse.ArgumentParser(description='统计 usage_ledger.jsonl 中 Chat 协议的缓存命中率')
    ap.add_argument('path', help='数据集结果目录或 usage_ledger.jsonl 路径')
    ap.add_argument('--model', default=None, help='只统计该被测模型名的调用（排除 Judge 等）')
    args = ap.parse_args()
    stats = compute(load_ledger(args.path), args.model)
    print(summary_line(stats))
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    if stats['verdict'] == 'non_compliant':
        sys.exit(2)


if __name__ == '__main__':
    main()
