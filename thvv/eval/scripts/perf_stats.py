# encoding:utf-8
"""从 usage_ledger.jsonl 统计评测过程中的 TPM / TPS 变化（时间窗口口径）。

数据来源：usage_ledger.jsonl（每次调用的时间戳 + 原始 usage），不依赖评测框架。
统计口径：
  - 按固定时间窗（默认 60s）切分，窗口内所有被测模型端点调用（含用户模拟器等辅助调用）的
    prompt_tokens + completion_tokens 计入；
  - TPM = 窗口内 (prompt + completion) tokens × 60 / 窗口秒数；
  - TPS = 窗口内 (prompt + completion) tokens / 窗口秒数；
  - 分位统计只取「活跃窗口」（窗口内有调用），空闲窗口单独计数，
    避免评测自身的空档把 TPM 均值压低。

用法：
  python3 perf_stats.py <数据集结果目录 | usage_ledger.jsonl> [--model 模型名] [--window 60]
"""
import argparse
import json

import statistics
import sys
from datetime import datetime, timezone

import cache_stats


def _quantile(vals, q):
    vals = sorted(vals)
    if not vals:
        return None
    k = (len(vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _stats(vals):
    if not vals:
        return {'n': 0, 'avg': None, 'p50': None, 'p90': None, 'p95': None,
                'min': None, 'max': None, 'stdev': None, 'cv': None}
    avg = statistics.mean(vals)
    return {
        'n': len(vals),
        'avg': avg,
        'p50': _quantile(vals, 0.5),
        'p90': _quantile(vals, 0.9),
        'p95': _quantile(vals, 0.95),
        'min': min(vals),
        'max': max(vals),
        'stdev': statistics.stdev(vals) if len(vals) > 1 else 0.0,
        'cv': (statistics.stdev(vals) / avg) if len(vals) > 1 and avg else 0.0,
    }


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%H:%M:%S')


def choose_window(duration_sec, target_windows=15):
    """按运行时长自动选窗：窗口数不超过 target_windows 的前提下取最小的候选窗。"""
    for c in (5, 10, 15, 20, 30, 60, 120, 180, 300, 600):
        if duration_sec / c <= target_windows:
            return c
    return 900


def compute(records, model_name=None, window_sec=None):
    """按窗口统计 TPM/TPS 变化。records 为 None 表示没有 ledger。
    window_sec 为 None 时按运行时长自动选窗。"""
    base = {'protocol': 'openai_chat', 'model': model_name}
    if records is None:
        return dict(base, available=False, window_sec=None,
                    reason='未找到 usage_ledger.jsonl（本次运行未开启 usage 记录）')

    rows = sorted(cache_stats._chat_rows(records, model_name), key=lambda r: r.get('ts') or 0)
    rows = [r for r in rows if isinstance(r.get('ts'), (int, float))]
    if not rows:
        return dict(base, available=False, window_sec=None,
                    reason='ledger 中没有可用的时间戳记录')

    t0, t1 = rows[0]['ts'], rows[-1]['ts']
    duration = max(t1 - t0, 1e-6)
    auto = window_sec is None
    window_sec = int(window_sec or choose_window(duration))
    base['window_sec'] = window_sec
    base['window_auto'] = auto
    n_win = max(1, int(duration / window_sec) + 1)

    buckets = [{'calls': 0, 'prompt': 0, 'completion': 0, 'cached': 0, 'subject_calls': 0} for _ in range(n_win)]
    total = {'calls': 0, 'prompt': 0, 'completion': 0}
    for r in rows:
        status, prompt, _cached = cache_stats.classify(r.get('usage'))
        usage = r.get('usage') if isinstance(r.get('usage'), dict) else {}
        completion = usage.get('completion_tokens')
        completion = completion if isinstance(completion, int) and not isinstance(completion, bool) else 0
        prompt_v = prompt or 0
        idx = min(int((r['ts'] - t0) / window_sec), n_win - 1)
        b = buckets[idx]
        b['calls'] += 1
        b['prompt'] += prompt_v
        b['completion'] += completion
        if r.get('role') == 'subject':
            b['subject_calls'] += 1
        total['calls'] += 1
        total['prompt'] += prompt_v
        total['completion'] += completion

    windows = []
    for i, b in enumerate(buckets):
        tokens = b['prompt'] + b['completion']
        covered = min(window_sec, max(0.0, t1 - (t0 + i * window_sec)))
        windows.append({
            'idx': i,
            'start': _iso(t0 + i * window_sec),
            'offset_sec': i * window_sec,
            'calls': b['calls'],
            'subject_calls': b['subject_calls'],
            'prompt_tokens': b['prompt'],
            'completion_tokens': b['completion'],
            'total_tokens': tokens,
            'coverage_sec': round(covered, 1),
            'partial': covered < window_sec - 1e-6,  # 末尾不足一个窗口
            'tpm': tokens * 60.0 / window_sec,
            'tps': tokens / float(window_sec),
        })

    active = [w for w in windows if w['calls'] > 0]
    # 分位/波动统计排除末尾不完整窗口（会天然偏低，拉大波动）
    full = [w for w in active if not w['partial']] or active
    tpm_vals = [w['tpm'] for w in full]
    tps_vals = [w['tps'] for w in full]
    out_tpm = [w['completion_tokens'] * 60.0 / window_sec for w in full]  # 仅输出 token 的 TPM
    total_tokens = total['prompt'] + total['completion']
    tpm_stats = _stats(tpm_vals)
    peak = max(full, key=lambda w: w['tpm']) if full else None
    trough = min(full, key=lambda w: w['tpm']) if full else None

    return dict(
        base,
        available=True,
        duration_sec=duration,
        window_count=len(windows),
        active_windows=len(active),
        idle_windows=len(windows) - len(active),
        partial_windows=sum(1 for w in active if w['partial']),
        calls=total['calls'],
        prompt_tokens=total['prompt'],
        completion_tokens=total['completion'],
        total_tokens=total_tokens,
        tpm_overall=total_tokens * 60.0 / duration,
        tpm=_hook_stats(tpm_stats, peak, trough),
        tps=_stats(tps_vals),
        output_tpm=_stats(out_tpm),
        windows=windows,
    )


def _hook_stats(st, peak, trough):
    """在分位统计上补充峰值/谷值所在窗口，便于定位波动。"""
    st = dict(st)
    st['peak_window'] = peak['start'] if peak else None
    st['trough_window'] = trough['start'] if trough else None
    return st


def compute_for_dirs(dirs, model_name=None, window_sec=60):
    """合并多个数据集目录的 ledger（用于整次运行汇总）。"""
    merged, found = [], False
    for d in dirs:
        recs = cache_stats.load_ledger(d)
        if recs is not None:
            found = True
            merged.extend(recs)
    return compute(merged if found else None, model_name, window_sec)


def summary_line(stats):
    if not stats.get('available'):
        return f"吞吐（TPM）：无数据（{stats.get('reason')}）"
    t = stats.get('tpm') or {}
    return (f"吞吐 TPM 全程均值 {stats['tpm_overall']:,.0f}｜"
            f"活跃窗口 Avg {t.get('avg') or 0:,.0f} / P50 {t.get('p50') or 0:,.0f} / "
            f"P90 {t.get('p90') or 0:,.0f} / 峰值 {t.get('max') or 0:,.0f}"
            f"（{stats['active_windows']}/{stats['window_count']} 个窗口有调用）")


def main():
    ap = argparse.ArgumentParser(description='统计评测过程的 TPM/TPS 变化（按时间窗口）')
    ap.add_argument('path', help='数据集结果目录或 usage_ledger.jsonl 路径')
    ap.add_argument('--model', default=None, help='只统计该模型的调用')
    ap.add_argument('--window', type=int, default=None, help='时间窗秒数（默认按时长自动选窗）')
    args = ap.parse_args()
    stats = compute(cache_stats.load_ledger(args.path), args.model, args.window)
    print(summary_line(stats))
    print(json.dumps(stats, indent=2, ensure_ascii=False))
    if not stats.get('available'):
        sys.exit(2)


if __name__ == '__main__':
    main()
