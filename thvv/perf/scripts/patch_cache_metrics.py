#!/usr/bin/env python3
"""从 evalscope 结果库重算缓存命中率（修正 evalscope 解析缺陷）。

为什么需要
----------
evalscope `plugin/api/default_api.py` 解析流式 usage 的写法是：

    if data.get('choices'):
        ...处理 delta...
    elif usage := data.get('usage'):
        ...提取 prompt_tokens_details.cached_tokens...

这个 `elif` 只在「choices 为空」时生效（OpenAI 规范下 usage 单独一个 chunk）。
但 DeepSeek 等厂商把 usage 挂在**最后一个有 choices 的 chunk** 上，
于是 cached_tokens 永远提取不到，报告里的 `KV Cache Hit Rate (%)` 恒为 0。

本脚本直接读 `benchmark_data.db` 的 `response_messages`（pickle），自行解析 usage，
重算每个 run 的真实命中率，并输出 CSV / 控制台表格。

用法
----
  python3 scripts/patch_cache_metrics.py --results-dir results
  python3 scripts/patch_cache_metrics.py --results-dir results --filter perf-1k-20261116-1943
  python3 scripts/patch_cache_metrics.py --results-dir results --out /tmp/cache.csv
"""

import argparse
import base64
import csv
import json
import os
import pickle
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PERF_DIR = os.path.dirname(HERE)


def extract_usage(obj):
    """从 response_messages（chunk 列表）里找 usage，兼容 choices 非空的厂商。"""
    if isinstance(obj, dict):
        if obj.get("usage"):
            return obj["usage"]
        return None
    if not isinstance(obj, list):
        return None
    for ch in obj:
        if isinstance(ch, dict) and ch.get("usage"):
            return ch["usage"]
    return None


def load_rows(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(result)")
    cols = [r[1] for r in cur.fetchall()]
    cur.execute("SELECT * FROM result")
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()

    out = []
    for r in rows:
        if r.get("success") != 1:
            continue
        rm = r.get("response_messages")
        if not rm:
            continue
        obj = None
        try:
            if isinstance(rm, (bytes, bytearray)):
                obj = pickle.loads(rm)
            elif isinstance(rm, str):
                try:
                    obj = pickle.loads(base64.b64decode(rm))
                except Exception:
                    obj = json.loads(rm)
            else:
                obj = pickle.loads(rm)
        except Exception:
            continue

        usage = extract_usage(obj)
        if not usage:
            continue
        pt = usage.get("prompt_tokens") or r.get("prompt_tokens") or 0
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens")
        if cached is None:
            cached = usage.get("prompt_cache_hit_tokens")   # DeepSeek 顶层字段
        if cached is None:
            continue
        out.append({
            "prompt_tokens": int(pt),
            "cached_tokens": int(cached),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        })
    return out


def percentile(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


def main():
    ap = argparse.ArgumentParser(description="重算 evalscope 结果库的缓存命中率")
    ap.add_argument("--results-dir", default=os.path.join(PERF_DIR, "results"))
    ap.add_argument("--filter", nargs="+", help="只处理名字包含这些子串的 run 目录")
    ap.add_argument("--out", default="", help="输出 CSV 路径（可选）")
    ap.add_argument("--write-back", action="store_true",
                    help="把重算的命中率写回同级 benchmark_summary.json，"
                         "让 HTML/Excel 报告直接显示真实值")
    args = ap.parse_args()

    dbs = []
    for root, _dirs, files in os.walk(args.results_dir):
        if "benchmark_data.db" in files:
            dbs.append(os.path.join(root, "benchmark_data.db"))
    if args.filter:
        dbs = [d for d in dbs if any(f in d for f in args.filter)]
    dbs.sort()
    if not dbs:
        sys.exit("未找到 benchmark_data.db")

    results = []
    for db in dbs:
        rows = load_rows(db)
        if not rows:
            continue
        total_pt = sum(r["prompt_tokens"] for r in rows)
        total_cached = sum(r["cached_tokens"] for r in rows)
        ratios = [r["cached_tokens"] / r["prompt_tokens"] for r in rows if r["prompt_tokens"]]
        hit_ratio = total_cached / total_pt if total_pt else 0
        if args.write_back:
            sj = os.path.join(os.path.dirname(db), "benchmark_summary.json")
            if os.path.exists(sj):
                with open(sj, encoding="utf-8") as f:
                    summ = json.load(f)
                summ["KV Cache Hit Rate (%)"] = round(hit_ratio * 100, 2)
                summ["Eligible Cache Hit Rate (%)"] = round(hit_ratio * 100, 2)
                with open(sj, "w", encoding="utf-8") as f:
                    json.dump(summ, f, ensure_ascii=False, indent=2)
        results.append({
            "run": os.path.relpath(os.path.dirname(os.path.dirname(db)), args.results_dir),
            "requests": len(rows),
            "prompt_tokens": total_pt,
            "cached_tokens": total_cached,
            "hit_ratio": total_cached / total_pt if total_pt else 0,
            "hit_ratio_p50": percentile(ratios, 0.5),
            "hit_ratio_p90": percentile(ratios, 0.9),
            "zero_hit_requests": sum(1 for r in ratios if r == 0),
        })

    hdr = f"{'run':<42}{'条数':<7}{'命中率':<10}{'P50':<9}{'P90':<9}{'零命中':<8}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        print(f"{r['run']:<42}{r['requests']:<7}{r['hit_ratio']:<10.1%}"
              f"{r['hit_ratio_p50']:<9.1%}{r['hit_ratio_p90']:<9.1%}{r['zero_hit_requests']:<8}")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
        print(f"\nCSV: {args.out}")


if __name__ == "__main__":
    main()
