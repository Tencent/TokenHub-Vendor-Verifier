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

两条数据来源
------------
1. `response_usage`：`response_messages` 里能取到 usage 明细 → 逐条重算，
   加 `--write-back` 时把结果写回同级 `benchmark_summary.json`。
2. `summary_native`：响应体里没有 usage 明细（如 glm-5.3-flash 按 OpenAI 规范把
   usage 放在独立 chunk，evalscope 落库时只保存了有 choices 的 chunk），
   此时回退读取 `benchmark_summary.json` 的原生命中率，**绝不写回覆盖**
   （否则会把 55%~75% 覆盖成 0）。逐条 P50/P90/零命中无明细，显示 `-`。

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

# CSV 列固定，不依赖 results 是否为空（results 为空时 results[0] 会 IndexError）
FIELDNAMES = [
    "run", "source", "requests", "prompt_tokens", "cached_tokens",
    "hit_ratio", "hit_ratio_p50", "hit_ratio_p90", "zero_hit_requests",
]

SOURCE_RESPONSE = "response_usage"
SOURCE_SUMMARY = "summary_native"


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


def read_summary_hit(db_path):
    """读 evalscope 原生命中率（百分数）。文件或字段缺失返回 None。"""
    sj = os.path.join(os.path.dirname(db_path), "benchmark_summary.json")
    try:
        with open(sj, encoding="utf-8") as f:
            val = json.load(f).get("KV Cache Hit Rate (%)")
    except Exception:
        return None
    try:
        return None if val is None else float(val)
    except (TypeError, ValueError):
        return None


def load_rows(db_path, stats=None):
    """返回 [{prompt_tokens, cached_tokens, completion_tokens}, ...]。

    stats 若传入 dict，会被累加计数（含跳过原因），便于在没有可用明细时说明原因。
    """
    st = stats if stats is not None else {}
    for k in ("total", "success", "no_success", "no_messages", "bad_payload",
              "no_usage", "no_cached"):
        st.setdefault(k, 0)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(result)")
    cols = [r[1] for r in cur.fetchall()]
    cur.execute("SELECT * FROM result")
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()

    out = []
    for r in rows:
        st["total"] += 1
        if r.get("success") != 1:
            st["no_success"] += 1
            continue
        st["success"] += 1
        rm = r.get("response_messages")
        if not rm:
            st["no_messages"] += 1
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
            st["bad_payload"] += 1
            continue

        usage = extract_usage(obj)
        if not usage:
            st["no_usage"] += 1
            continue
        pt = usage.get("prompt_tokens") or r.get("prompt_tokens") or 0
        details = usage.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens")
        if cached is None:
            cached = usage.get("prompt_cache_hit_tokens")   # DeepSeek 顶层字段
        if cached is None:
            st["no_cached"] += 1
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


def fmt_pct(v):
    return "-" if v is None else f"{v:.1%}"


def fmt_num(v):
    return "-" if v is None else str(v)


def _merge_stats(dst, src):
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def _empty_reason(dbs, stats):
    """无任何可用数据时给出可读原因，而不是抛 IndexError。"""
    if not stats.get("total"):
        return f"[warn] 扫描到 {len(dbs)} 个 benchmark_data.db，但 result 表为空"
    parts = []
    if stats.get("no_success"):
        parts.append(f"未成功请求 {stats['no_success']} 条")
    if stats.get("no_messages"):
        parts.append(f"无 response_messages {stats['no_messages']} 条")
    if stats.get("bad_payload"):
        parts.append(f"响应体反序列化失败 {stats['bad_payload']} 条")
    if stats.get("no_usage"):
        parts.append(f"响应不含 usage {stats['no_usage']} 条"
                     f"（OpenAI 规范下 usage 是独立 chunk，evalscope 只落库有 choices 的 chunk）")
    if stats.get("no_cached"):
        parts.append(f"usage 不含 cached_tokens/prompt_cache_hit_tokens "
                     f"{stats['no_cached']} 条（供应商未回传缓存计量，属测不到而非零命中）")
    detail = "；".join(parts) or "无可用记录"
    return (f"[warn] 扫描到 {len(dbs)} 个 benchmark_data.db、{stats['total']} 条请求，"
            f"但没有可统计的缓存明细：{detail}")


def main():
    ap = argparse.ArgumentParser(description="重算 evalscope 结果库的缓存命中率")
    ap.add_argument("--results-dir", default=os.path.join(PERF_DIR, "results"))
    ap.add_argument("--filter", nargs="+", help="只处理名字包含这些子串的 run 目录")
    ap.add_argument("--out", default="", help="输出 CSV 路径（可选）")
    ap.add_argument("--write-back", action="store_true",
                    help="把重算的命中率写回同级 benchmark_summary.json，"
                         "让 HTML/Excel 报告直接显示真实值（仅 response_usage 来源）")
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
    stats = {}
    n_native = 0
    skipped = []
    for db in dbs:
        db_stats = {}
        rows = load_rows(db, db_stats)
        _merge_stats(stats, db_stats)
        run_name = os.path.relpath(os.path.dirname(os.path.dirname(db)), args.results_dir)

        if not rows:
            # OpenAI 规范下 usage 是独立 chunk，evalscope 已解析并写入 summary；
            # 响应体里没有明细可重算。此时沿用原生值，绝不覆盖成 0。
            native = read_summary_hit(db)
            if native is None:
                skipped.append(run_name)
                continue
            n_native += 1
            results.append({
                "run": run_name,
                "source": SOURCE_SUMMARY,
                "requests": db_stats.get("success", 0),
                "prompt_tokens": None,
                "cached_tokens": None,
                "hit_ratio": native / 100.0,
                "hit_ratio_p50": None,
                "hit_ratio_p90": None,
                "zero_hit_requests": None,
            })
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
            "run": run_name,
            "source": SOURCE_RESPONSE,
            "requests": len(rows),
            "prompt_tokens": total_pt,
            "cached_tokens": total_cached,
            "hit_ratio": total_cached / total_pt if total_pt else 0,
            "hit_ratio_p50": percentile(ratios, 0.5),
            "hit_ratio_p90": percentile(ratios, 0.9),
            "zero_hit_requests": sum(1 for r in ratios if r == 0),
        })

    hdr = (f"{'run':<42}{'来源':<15}{'条数':<7}{'命中率':<10}"
           f"{'P50':<9}{'P90':<9}{'零命中':<8}")
    print(hdr)
    print("-" * 120)
    for r in results:
        print(f"{r['run']:<42}{r['source']:<15}{fmt_num(r['requests']):<7}"
              f"{fmt_pct(r['hit_ratio']):<10}{fmt_pct(r['hit_ratio_p50']):<9}"
              f"{fmt_pct(r['hit_ratio_p90']):<9}{fmt_num(r['zero_hit_requests']):<8}")

    if n_native:
        print(f"\n[info] {n_native} 个 run 响应体内无 usage 明细，沿用 benchmark_summary.json "
              f"原生命中率（source={SOURCE_SUMMARY}，未写回）")
    if skipped:
        print(f"[warn] {len(skipped)} 个 run 既无 usage 明细、summary 里也没有命中率，已跳过："
              f"{', '.join(skipped[:5])}{' ...' if len(skipped) > 5 else ''}")
    if not results:
        print(_empty_reason(dbs, stats))

    if args.out:
        # results 为空时也写表头，避免下游/交付目录缺文件
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES)
            w.writeheader()
            for r in results:
                w.writerow({k: ("-" if r[k] is None else r[k]) for k in FIELDNAMES})
        print(f"\nCSV: {args.out}" + ("" if results else "（仅表头，本次无可统计数据）"))


if __name__ == "__main__":
    main()
