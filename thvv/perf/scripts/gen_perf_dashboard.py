#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""性能压测报告生成器（唯一报告出口，对齐效果评测的单 HTML 形态）。

输出的 性能测试报告.html 为单文件、无外部依赖（无 CDN / 无 JS 库），离线可直接打开：
  一、总体结论        指标卡片 + 处置建议
  二、并发梯度对比    按并发梯度做矩阵，一行一个并发档，横向比指标
  三、失败分析        失败原因聚合统计（不再逐条罗列）
  四、失败请求明细    逐条罗列所有失败请求（HTTP状态码 / 请求Body / 响应Body /
                      TTFT / TTLT，不提取 Request ID，与效果评测口径一致）

用法:
  python3 scripts/gen_perf_dashboard.py --results-dir results --out 性能测试报告.html
  python3 scripts/gen_perf_dashboard.py --run-dir results/perf-1k-20260831-2143
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple


def find_dbs(root: str) -> List[Tuple[str, str, int, int, str]]:
    """在结果目录下递归查找 benchmark_data.db。

    返回 (db路径, bucket, parallel, number, vendor) 元组列表。
    目录约定：.../perf-<bucket>-<ts>/<vendor>/parallel_<p>_number_<n>/benchmark_data.db
    """
    found = []
    for dirpath, _dirs, files in os.walk(root, followlinks=True):
        if "benchmark_data.db" not in files:
            continue
        db = os.path.join(dirpath, "benchmark_data.db")
        parts = dirpath.replace("\\", "/").split("/")
        bucket = parallel = number = vendor = ""
        for part in parts:
            m = re.match(r"^perf-([0-9a-zA-Z]+)-\d{8}-\d{4}$", part)
            if m:
                bucket = m.group(1)
            m = re.match(r"^parallel_(\d+)_number_(\d+)$", part)
            if m:
                parallel, number = int(m.group(1)), int(m.group(2))
        # vendor 是 parallel_xx_number_xx 的上一级
        if "parallel_" in dirpath:
            vendor = os.path.basename(os.path.dirname(dirpath))
        found.append((db, bucket or "?", parallel or 0, number or 0, vendor or "?"))
    return sorted(found, key=lambda x: (x[1], x[2]))


def load_rows(db: str) -> List[dict]:
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM result")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except sqlite3.Error:
        return []


def _mean(vals: List[float]) -> Optional[float]:
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _pct(sorted_vals: List[float], p: float) -> Optional[float]:
    """分位数（线性插值），与 gen_report_from_db.py xlsx 口径一致。"""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def compute_stats(rows: List[dict]) -> dict:
    """从原始行计算聚合指标。"""
    total = len(rows)
    ok_rows = [r for r in rows if int(r.get("success") or 0) == 1]
    fail_rows = [r for r in rows if int(r.get("success") or 0) != 1]

    lat, ttft, tpot, rates, itl = [], [], [], [], []
    prompt_tok, compl_tok = [], []
    for r in ok_rows:
        v = r.get("latency")
        if v is not None:
            lat.append(float(v))
        v = r.get("first_chunk_latency")
        if v is not None:
            ttft.append(float(v))
        v = r.get("time_per_output_token")
        if v is not None:
            tpot.append(float(v))
        v = r.get("prompt_tokens")
        if v is not None:
            prompt_tok.append(float(v))
        v = r.get("completion_tokens")
        if v is not None:
            compl_tok.append(float(v))
        # Rate(t/s)：completion / (latency - ttft)，条件与 xlsx 口径一致
        if r.get("completion_tokens") is not None and r.get("latency") is not None:
            c = float(r["completion_tokens"] or 0)
            l = float(r["latency"] or 0)
            t = float(r["first_chunk_latency"] or 0)
            if c >= 5 and (l - t) >= 0.1:
                rates.append(c / (l - t))
        # ITL：inter_token_latencies JSON 列表
        v = r.get("inter_token_latencies")
        if v:
            try:
                vals = json.loads(v) if isinstance(v, str) else v
                if isinstance(vals, list):
                    itl.extend(x for x in vals if isinstance(x, (int, float)))
            except (json.JSONDecodeError, TypeError):
                pass

    lat_s, ttft_s = sorted(lat), sorted(ttft)
    itl_s, rates_s = sorted(itl), sorted(rates)
    total_out = sum(compl_tok)
    total_in = sum(prompt_tok)

    # 墙钟吞吐：与 xlsx 相同，max(completed_time) - min(start_time)
    starts = [float(r["start_time"]) for r in ok_rows if r.get("start_time") is not None]
    ends = [float(r["completed_time"]) for r in ok_rows if r.get("completed_time") is not None]
    wall = (max(ends) - min(starts)) if starts and ends and max(ends) > min(starts) else 0.0
    output_tps = (total_out / wall) if wall > 0 else None
    input_tps = (total_in / wall) if wall > 0 else None
    tpm = (output_tps + input_tps) * 60 if (output_tps is not None and input_tps is not None) else None
    lat_mean = _mean(lat)

    return {
        "total": total,
        "success": len(ok_rows),
        "failed": len(fail_rows),
        "success_rate": (len(ok_rows) / total) if total else 0.0,
        # TTFT（首 token 时延）
        "ttft_mean": _mean(ttft),
        "ttft_min": min(ttft) if ttft else None,
        "ttft_max": max(ttft) if ttft else None,
        "ttft_p50": _pct(ttft_s, 50),
        "ttft_p90": _pct(ttft_s, 90),
        "ttft_p95": _pct(ttft_s, 95),
        "ttft_p99": _pct(ttft_s, 99),
        # TTLT（总时延）
        "latency_mean": lat_mean,
        "latency_p50": _pct(lat_s, 50),
        "latency_p90": _pct(lat_s, 90),
        "latency_p95": _pct(lat_s, 95),
        "latency_p99": _pct(lat_s, 99),
        # Rate（生成速率）与 ITL（token 间隔）
        "rate_mean": _mean(rates),
        "rate_p50": _pct(rates_s, 50),
        "rate_p90": _pct(rates_s, 90),
        "rate_p95": _pct(rates_s, 95),
        "rate_p99": _pct(rates_s, 99),
        "itl_mean": _mean(itl),
        "itl_p50": _pct(itl_s, 50),
        "itl_p90": _pct(itl_s, 90),
        "itl_p95": _pct(itl_s, 95),
        "itl_p99": _pct(itl_s, 99),
        # Tokens
        "prompt_tokens_mean": _mean(prompt_tok),
        "completion_tokens_mean": _mean(compl_tok),
        "total_tokens": int(total_in + total_out),
        # 吞吐（墙钟口径）与总时延
        "avg_total_ms": (lat_mean * 1000) if lat_mean is not None else None,
        "output_tps": output_tps,
        "input_tps": input_tps,
        "output_tpm": (output_tps * 60) if output_tps is not None else None,
        "input_tpm": (input_tps * 60) if input_tps is not None else None,
        "tpm": tpm,
        # 兼容旧引用
        "tpot_mean": _mean(tpot),
        "decode_tps": _mean(rates),
        "wall_clock": wall,
    }


def _decode_response_messages(raw: Any) -> List[Any]:
    """解码 evalscope 的 response_messages 列。

    该列是 base64(pickle(list))，直接按文本匹配状态码会失效
    （曾导致 429 全部被误归为"其他"）。解码失败时返回空列表。
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return []
    import base64
    import pickle
    for fn in (lambda: pickle.loads(base64.b64decode(raw)),
               lambda: json.loads(raw)):
        try:
            val = fn()
            return val if isinstance(val, list) else [val]
        except Exception:
            continue
    return []


# 状态码 → 原因分类；非 4xx/5xx 走文本兜底
_STATUS_LABELS = {
    429: "限流",
    408: "超时", 504: "超时", 502: "服务端错误",
    503: "服务端错误", 500: "服务端错误", 501: "服务端错误",
}
_TEXT_LABELS = (
    ("rate limit", "限流"), ("too many requests", "限流"),
    ("timeout", "超时"), ("timed out", "超时"),
    ("connection", "连接失败"),
    ("internal server error", "服务端错误"),
    ("bad gateway", "服务端错误"), ("service unavailable", "服务端错误"),
)


def failure_reasons(rows: List[dict]) -> Counter:
    """聚合失败原因：先解码取状态码，再按文本兜底。"""
    cnt: Counter = Counter()
    for r in rows:
        if int(r.get("success") or 0) == 1:
            continue
        msgs = _decode_response_messages(r.get("response_messages"))
        status = None
        for msg in msgs:
            if isinstance(msg, dict):
                for key in ("status_code", "http_status", "code"):
                    v = msg.get(key)
                    if isinstance(v, int) and 100 <= v < 600:
                        status = v
                        break
                if status:
                    break
        if status:
            label = _STATUS_LABELS.get(status)
            if label is None:
                label = "客户端错误(4xx)" if 400 <= status < 500 else "服务端错误"
            cnt[label] += 1
            continue

        low = json.dumps(msgs, ensure_ascii=False, default=str).lower()
        for key, label in _TEXT_LABELS:
            if key in low:
                cnt[label] += 1
                break
        else:
            cnt["其他"] += 1
    return cnt


def _fmt(v: Any, prec: int = 2, dash: str = '-') -> str:
    if v is None:
        return dash
    try:
        return f"{float(v):.{prec}f}"
    except (TypeError, ValueError):
        return str(v)


def render(dbs: List[Tuple[str, str, int, int, str]], title: str,
           model_hint: str = "") -> str:
    groups = defaultdict(list)
    vendors = set()
    fail_items: List[dict] = []
    for db, bucket, parallel, number, vendor in dbs:
        rows = load_rows(db)
        if not rows:
            continue
        st = compute_stats(rows)
        st.update({"bucket": bucket, "parallel": parallel,
                   "number": number, "vendor": vendor})
        st["_failure_reasons"] = failure_reasons(rows)
        groups[bucket].append(st)
        vendors.add(vendor)
        # 收集失败请求明细（逐条，第四章用）
        for r in rows:
            if int(r.get("success") or 0) == 1:
                continue
            fail_items.append(
                _failed_detail(r, bucket, parallel, len(fail_items) + 1))

    vendor_label = " / ".join(sorted(vendors)) if vendors else "-"
    all_stats = [s for lst in groups.values() for s in lst]
    overall_total = sum(s["total"] for s in all_stats)
    overall_fail = sum(s["failed"] for s in all_stats)
    overall_rate = (1 - overall_fail / overall_total) if overall_total else 0.0

    P = []
    P.append(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
"Microsoft YaHei",sans-serif;background:#f8fafc;color:#0f172a;font-size:14px;
line-height:1.6;padding:24px 32px 80px}}
h1{{font-size:24px;margin:0 0 6px}}
h2{{font-size:18px;margin:30px 0 12px;padding-bottom:6px;border-bottom:2px solid #e2e8f0}}
h3{{font-size:15px;margin:16px 0 8px;color:#334155}}
.meta{{color:#64748b;font-size:13px;margin-bottom:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin:14px 0}}
.card{{background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:14px 16px}}
.card .lbl{{color:#64748b;font-size:12px}}
.card .val{{font-size:21px;font-weight:600;margin-top:4px}}
.card.ok .val{{color:#16a34a}}
.card.warn .val{{color:#dc2626}}
.card.info .val{{color:#2563eb}}
.table-wrap{{overflow-x:auto;margin:8px 0 22px}}
.table-wrap table{{margin:0;min-width:100%}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e8f0;
border-radius:8px;overflow:hidden;margin:8px 0 22px;font-size:13px}}
thead th{{background:#f1f5f9;font-weight:600;padding:10px 11px;text-align:right;
border-bottom:1px solid #e2e8f0;white-space:nowrap}}
thead th:first-child{{text-align:left}}
tbody td{{padding:9px 11px;border-bottom:1px solid #f1f5f9;text-align:right;
font-variant-numeric:tabular-nums;white-space:nowrap}}
tbody td:first-child{{text-align:left;font-weight:600}}
tbody tr:hover td{{background:#f8fafc}}
.bad{{color:#dc2626;font-weight:600}}
.good{{color:#16a34a;font-weight:600}}
.note{{background:#fff;border-left:4px solid #2563eb;border-radius:0 8px 8px 0;
padding:12px 16px;margin:12px 0 20px}}
.note ul{{margin:6px 0 0 18px}}
.note li{{margin:4px 0}}
.bar{{display:flex;align-items:center;gap:10px;margin:6px 0}}
.bar .n{{width:110px;flex:none;font-size:12px;color:#334155}}
.bar .t{{flex:1;background:#f1f5f9;border-radius:4px;height:16px;overflow:hidden}}
.bar .f{{height:100%;border-radius:4px;background:#dc2626}}
.bar .v{{width:80px;flex:none;text-align:right;font-size:12px;color:#475569}}
.footer{{color:#94a3b8;font-size:12px;margin-top:44px;padding-top:16px;
border-top:1px solid #e2e8f0;text-align:center}}
.fail-table td{{white-space:normal;vertical-align:top}}
.fail-table td.summary{{max-width:460px;word-break:break-all}}
.fail-table details summary{{cursor:pointer;color:#2563eb;font-size:12px;user-select:none}}
.fail-table .k{{color:#64748b;font-size:11px;font-weight:600;margin-top:8px}}
.fail-table pre{{margin:4px 0 0;padding:8px 10px;background:#f8fafc;border:1px solid #e2e8f0;
border-radius:6px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
white-space:pre-wrap;word-break:break-word;max-height:240px;overflow:auto}}
</style></head><body>
<h1>{title}</h1>
<p class="meta">供应商 <strong>{vendor_label}</strong>""" + (
    f" ｜ 模型 <strong>{model_hint}</strong>" if model_hint else "") +
    f""" ｜ 档位 {len(groups)} 个 ｜ 并发档 {len(all_stats)} 组</p>

<h2>一、总体结论</h2>
<div class="grid">
  <div class="card info"><div class="lbl">总请求数</div><div class="val">{overall_total}</div></div>
  <div class="card ok"><div class="lbl">成功</div><div class="val">{overall_total - overall_fail}</div></div>
  <div class="card warn"><div class="lbl">失败</div><div class="val">{overall_fail}</div></div>
  <div class="card {'ok' if overall_rate >= 0.95 else 'warn'}"><div class="lbl">总成功率</div>
    <div class="val">{overall_rate:.1%}</div></div>
</div>
<div class="note">{overall_note(all_stats, overall_rate, overall_fail)}</div>
""")

    # ── 二、并发梯度矩阵（每个 bucket 一张表；指标与 xlsx 性能指标 sheet 完全同口径）──
    P.append("<h2>二、并发梯度对比</h2>")
    for bucket in sorted(groups):
        stats = sorted(groups[bucket], key=lambda s: s["parallel"])
        P.append(f"<h3>档位 {bucket}</h3>")
        P.append('<div class="table-wrap"><table><thead><tr>'
                 '<th>并发</th><th>总请求</th><th>成功</th><th>失败</th><th>成功率</th>'
                 '<th>Avg TTFT(s)</th><th>Min TTFT(s)</th><th>Max TTFT(s)</th>'
                 '<th>P50 TTFT(s)</th><th>P90 TTFT(s)</th><th>P95 TTFT(s)</th><th>P99 TTFT(s)</th>'
                 '<th>Avg TTLT(s)</th><th>P50 TTLT(s)</th><th>P90 TTLT(s)</th><th>P95 TTLT(s)</th><th>P99 TTLT(s)</th>'
                 '<th>Avg Rate(t/s)</th><th>P50 Rate(t/s)</th><th>P90 Rate(t/s)</th><th>P95 Rate(t/s)</th><th>P99 Rate(t/s)</th>'
                 '<th>Avg ITL(s)</th><th>P50 ITL(s)</th><th>P90 ITL(s)</th><th>P95 ITL(s)</th><th>P99 ITL(s)</th>'
                 '<th>Avg Prompt Tokens</th><th>Avg Completion Tokens</th><th>Total Tokens</th>'
                 '<th>Avg Cached Tokens</th><th>Avg Cache Hit Ratio</th><th>Avg Total Time(ms)</th>'
                 '<th>Output TPM</th><th>Output TPS</th><th>Input TPM</th><th>TPM</th>'
                 '</tr></thead><tbody>')
        for s in stats:
            rate = s["success_rate"]
            cls = "good" if rate >= 0.95 else ("bad" if rate < 0.8 else "")
            cells = [
                str(s["parallel"]), str(s["total"]), str(s["success"]), str(s["failed"]),
                f'<span class="{cls}">{rate:.2%}</span>',
                # 秒/速率类 3 位小数，与 xlsx "0.000" 格式一致
                _fmt(s["ttft_mean"], 3), _fmt(s["ttft_min"], 3), _fmt(s["ttft_max"], 3),
                _fmt(s["ttft_p50"], 3), _fmt(s["ttft_p90"], 3), _fmt(s["ttft_p95"], 3), _fmt(s["ttft_p99"], 3),
                _fmt(s["latency_mean"], 3), _fmt(s["latency_p50"], 3), _fmt(s["latency_p90"], 3),
                _fmt(s["latency_p95"], 3), _fmt(s["latency_p99"], 3),
                _fmt(s["rate_mean"], 3), _fmt(s["rate_p50"], 3), _fmt(s["rate_p90"], 3),
                _fmt(s["rate_p95"], 3), _fmt(s["rate_p99"], 3),
                _fmt(s["itl_mean"], 3), _fmt(s["itl_p50"], 3), _fmt(s["itl_p90"], 3),
                _fmt(s["itl_p95"], 3), _fmt(s["itl_p99"], 3),
                _fmt(s["prompt_tokens_mean"], 1), _fmt(s["completion_tokens_mean"], 1),
                f"{s['total_tokens']:,}",
                "-", "-",  # Avg Cached Tokens / Avg Cache Hit Ratio：数据源未提供
                _fmt(s["avg_total_ms"]),
                _fmt(s["output_tpm"]), _fmt(s["output_tps"]),
                _fmt(s["input_tpm"]), _fmt(s["tpm"]),
            ]
            P.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        P.append("</tbody></table></div>")

    # ── 三、失败分析 ──
    P.append("<h2>三、失败分析</h2>")
    agg: Counter = Counter()
    for s in all_stats:
        agg.update(s.get("_failure_reasons") or {})
    if agg:
        total_fail = sum(agg.values()) or 1
        P.append('<div class="card" style="margin-bottom:18px">')
        for reason, cnt in agg.most_common():
            pct = cnt / total_fail * 100
            P.append(
                f'<div class="bar"><div class="n">{reason}</div>'
                f'<div class="t"><div class="f" style="width:{pct:.1f}%"></div></div>'
                f'<div class="v">{cnt} ({pct:.0f}%)</div></div>'
            )
        P.append("</div>")
        P.append(failure_advice(agg))
    else:
        P.append('<p class="meta">✅ 本次压测无失败请求。</p>')

    # ── 四、失败请求明细（逐条，仅在有失败时输出）──
    if fail_items:
        P.append(render_failure_details(fail_items))

    P.append('<div class="footer">由 THVV · TokenHub Vendor Verifier 生成</div>')
    P.append("</body></html>")
    return "\n".join(P)


def overall_note(all_stats: List[dict], rate: float, fail: int) -> str:
    items = ["<ul>"]
    if fail == 0:
        items.append("<li>✅ 全部请求成功，无失败项。</li>")
    else:
        items.append(f"<li>⚠️ 共 <strong>{fail}</strong> 条请求失败，"
                     f"总成功率 <strong>{rate:.1%}</strong>。</li>")
    if all_stats:
        best = max(all_stats, key=lambda s: s["success_rate"])
        worst = min(all_stats, key=lambda s: s["success_rate"])
        items.append(
            f"<li>成功率最高：并发 <strong>{best['parallel']}</strong>"
            f"（{best['bucket']}，{best['success_rate']:.1%}）；"
            f"最低：并发 <strong>{worst['parallel']}</strong>"
            f"（{worst['bucket']}，{worst['success_rate']:.1%}）。</li>")
        fastest = min([s for s in all_stats if s["ttft_mean"]],
                      key=lambda s: s["ttft_mean"], default=None)
        if fastest:
            items.append(f"<li>首字节延迟最优：并发 "
                         f"<strong>{fastest['parallel']}</strong>，TTFT "
                         f"<strong>{fastest['ttft_mean']:.2f}s</strong>。</li>")
    items.append("</ul>")
    return "\n".join(items)


def failure_advice(agg: Counter) -> str:
    """按失败原因给出可操作建议。"""
    tips = {
        "限流": "降低并发或请求速率后重跑；若为账户额度限制，需联系供应商提额。",
        "超时": "确认 read-timeout 是否足够（当前 perf 默认 600s）；"
                "若端点本身慢，需供应商优化或降低 max_tokens。",
        "连接失败": "检查端点连通性与网络链路。",
        "服务端错误": "5xx 属供应商侧问题，需供应商排查；注意压测工具不会自动重试。",
    }
    out = ['<div class="note"><strong>处置建议</strong><ul>']
    for reason, _cnt in agg.most_common():
        tip = tips.get(reason)
        if tip:
            out.append(f"<li><strong>{reason}</strong>：{tip}</li>")
    if len(out) == 1:
        out.append("<li>其他类型失败：请查看报告中「失败请求明细」逐条排查。</li>")
    out.append("</ul></div>")
    return "\n".join(out)


# 失败请求 Body 截断长度（报告为单文件 HTML，避免体积失控）
_FAIL_BODY_CHARS = 2000


def _failed_detail(row: dict, bucket: str, parallel: int, no: int) -> dict:
    """提取单条失败请求的明细（不提取 Request ID，与效果评测口径一致）。"""
    msgs = _decode_response_messages(row.get("response_messages"))
    status = ""
    if msgs and isinstance(msgs[-1], dict):
        sc = msgs[-1].get("status_code") or msgs[-1].get("http_status")
        if sc:
            status = str(sc)
    if not status and row.get("status_code"):
        status = str(row["status_code"])

    body = str(row.get("error") or "")
    if not body and msgs:
        try:
            body = json.dumps(msgs, ensure_ascii=False, default=str)
        except Exception:
            body = str(msgs)

    summary = " ".join(body.split())[:140]
    return {
        "no": no,
        "bucket": bucket,
        "parallel": parallel,
        "status": status,
        "summary": summary,
        "request": str(row.get("request") or "")[:_FAIL_BODY_CHARS],
        "response": body[:_FAIL_BODY_CHARS],
        "ttft": row.get("first_chunk_latency"),
        "latency": row.get("latency"),
    }


def render_failure_details(items: List[dict]) -> str:
    """逐条罗列所有失败请求；点击行内 details 展开完整请求 / 响应 Body。"""
    from html import escape
    out = [
        '<h2>四、失败请求明细</h2>',
        f'<p class="meta">共 <strong>{len(items)}</strong> 条失败请求，逐条罗列；'
        '点击「详情」展开完整请求 / 响应 Body（超长截断，原始数据见 benchmark_data.db）。</p>',
        '<table class="fail-table"><thead><tr>'
        '<th>#</th><th>档位</th><th>并发</th><th>HTTP状态码</th>'
        '<th>错误摘要</th><th>TTLT(s)</th><th>详情</th>'
        '</tr></thead><tbody>',
    ]
    for it in items:
        out.append(
            f'<tr><td>{it["no"]}</td>'
            f'<td>{escape(str(it["bucket"]))}</td>'
            f'<td>{it["parallel"]}</td>'
            f'<td>{escape(it["status"])}</td>'
            f'<td class="summary">{escape(it["summary"])}</td>'
            f'<td>{_fmt(it["latency"])}</td>'
            f'<td><details><summary>详情</summary><div>'
            f'<div class="k">请求 Body</div><pre>{escape(it["request"])}</pre>'
            f'<div class="k">响应 Body</div><pre>{escape(it["response"])}</pre>'
            f'<div class="k">TTFT {_fmt(it["ttft"])}s ｜ TTLT {_fmt(it["latency"])}s</div>'
            f'</div></details></td></tr>'
        )
    out.append('</tbody></table>')
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="生成性能压测仪表盘 HTML")
    ap.add_argument("--results-dir", default="results", help="结果根目录")
    ap.add_argument("--run-dir", default="", help="单个 run 目录（优先于 --results-dir）")
    ap.add_argument("--out", default="", help="输出 HTML 路径，默认 <root>/性能测试报告.html")
    ap.add_argument("--title", default="性能压测仪表盘")
    ap.add_argument("--model", default="", help="模型名（展示用）")
    args = ap.parse_args()

    root = args.run_dir or args.results_dir
    if not os.path.isdir(root):
        raise SystemExit(f"目录不存在: {root}")

    dbs = find_dbs(root)
    if not dbs:
        raise SystemExit(f"未在 {root} 下找到 benchmark_data.db")

    html = render(dbs, args.title, args.model)
    out = args.out or os.path.join(root, "性能测试报告.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ 仪表盘已生成: {os.path.abspath(out)}")
    print(f"   扫描到 {len(dbs)} 个并发组，大小 {os.path.getsize(out)/1024:.1f} KB")


if __name__ == "__main__":
    main()
