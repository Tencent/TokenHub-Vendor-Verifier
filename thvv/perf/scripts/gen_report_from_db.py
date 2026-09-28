"""Generate performance report from benchmark_data.db.

Usage:
  python3 scripts/gen_report_from_db.py                         # 自动扫描 results/ 下所有压测目录
  python3 scripts/gen_report_from_db.py --vendor unknown --model glm-5.3-flash
  python3 scripts/gen_report_from_db.py --out report.xlsx       # 指定输出文件名

输出：性能测试报告_{vendor}_{model}_{timestamp}.xlsx

Sheets（基于 references/性能测试报告模板.xlsx 填充，禁止从零创建）：
  - 矩阵压测汇总   40 列标准格式（2 元数据 + 38 指标），按输入长度档位升序（1k → 200k）
  - 失败请求详情   14 列，逐条记录失败请求（API Key 脱敏 + 长文本截断）
  - 模板说明       模板版本与使用说明（模板自带，不修改）

染色规则（模板说明 Sheet + 腾讯验收口径，SLA 3 项）：
  - 成功率 Col H：=100% 绿 #C6EFCE，≥95% 黄 #FFEB9C，<95% 红 #FFC7CE
  - SLA：col 9 Avg TTFT / col 13 P90 TTFT 超档位阈值（sla_eval.TIERS）、
         col 21 Avg Rate（OTPS 口径）≤ 30 t/s → 红底白字加粗（C00000+FFFFFF）
"""
import argparse
import os
import json
import math
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill


_TEMPLATE_XLSX = Path(__file__).resolve().parent.parent / "references" / "性能测试报告模板.xlsx"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sla_eval import RATE_MIN_TOKENS_PER_SEC, classify_tier, get_tier_thresholds  # noqa: E402


_VENDOR_CHART_JS = Path(__file__).resolve().parent.parent.parent / "scripts" / "vendor" / "chart.umd.min.js"


def _bucket_num(bucket: str) -> int:
    """bucket（如 1k/9k/16k/200k）转数值，用于输入长度升序排序。"""
    m = re.search(r"(\d+)", str(bucket))
    return int(m.group(1)) if m else 10**9


def _mask_api_key(text: str) -> str:
    """失败请求详情 Headers/Body 中的 API Key 脱敏为 sk-***。"""
    if not text:
        return text
    return re.sub(r"(sk-|Bearer\s+)[A-Za-z0-9_\-]{8,}", r"\1***", text)


def _truncate(text, limit: int = 2000) -> str:
    """长文本截断并标注（模板规则：G/I 列超 2000 字符截断）。"""
    if text is None:
        return "-"
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit] + f" ...[已截断，原文共 {len(s)} 字符]"


def _chart_js_inline_or_cdn() -> str:
    """优先内联本地 vendor/chart.umd.min.js，失败则回退 CDN。"""
    try:
        if _VENDOR_CHART_JS.is_file():
            return f"<script>{_VENDOR_CHART_JS.read_text(encoding='utf-8')}</script>"
    except Exception:
        pass
    return '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>'


def percentile(data, p):
    if not data:
        return None
    s = sorted(data)
    k = (len(s) - 1) * p / 100
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def safe_avg(data):
    return sum(data) / len(data) if data else None


def _window_midpoints(t0: float, t_end: float, window_sec: float, step_sec: float | None = None) -> list[tuple[float, float, float]]:
    """生成滑动窗口 (lo, hi, mid_relative)。"""
    if t0 is None or t_end is None or t_end <= t0:
        return []
    step_sec = step_sec or window_sec / 2
    windows = []
    lo = t0
    while lo <= t_end:
        hi = lo + window_sec
        mid = lo + window_sec / 2 - t0
        windows.append((lo, hi, round(mid, 1)))
        lo += step_sec
    return windows


def _point_rate_trends(events: list[tuple[float, float]], t0: float, t_end: float, window_sec: float = 60.0) -> list[tuple[float, float]]:
    """Counter+rate 的离线近似：点事件按滑动窗口聚合为每分钟速率。"""
    import bisect
    events = sorted((float(ts), float(v)) for ts, v in events if ts is not None and v is not None)
    if not events:
        return []
    times = [ts for ts, _ in events]
    prefix_sum = [0.0]
    for _, value in events:
        prefix_sum.append(prefix_sum[-1] + value)

    def _range_sum(lo, hi):
        left = bisect.bisect_left(times, lo)
        right = bisect.bisect_left(times, hi)
        return prefix_sum[right] - prefix_sum[left]

    window_min = window_sec / 60.0
    trends = []
    for lo, hi, mid in _window_midpoints(t0, t_end, window_sec):
        trends.append((mid, round(_range_sum(lo, hi) / window_min, 1)))
    return trends


def _interval_rate_trends(intervals: list[tuple[float, float, float]], t0: float, t_end: float, window_sec: float = 60.0) -> list[tuple[float, float]]:
    """把区间内持续产生的 token 按重叠时间摊分到滑动窗口，近似 output token rate。"""
    norm = []
    for start, end, tokens in intervals:
        if start is None or end is None or tokens is None:
            continue
        start, end, tokens = float(start), float(end), float(tokens)
        if tokens <= 0:
            continue
        if end <= start:
            end = start + 1e-6
        norm.append((start, end, tokens / max(end - start, 1e-6)))
    if not norm:
        return []

    window_min = window_sec / 60.0
    trends = []
    for lo, hi, mid in _window_midpoints(t0, t_end, window_sec):
        token_sum = 0.0
        for start, end, rate_per_sec in norm:
            overlap = max(0.0, min(hi, end) - max(lo, start))
            if overlap > 0:
                token_sum += rate_per_sec * overlap
        trends.append((mid, round(token_sum / window_min, 1)))
    return trends


def _merge_rate_trends(*trend_lists: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """按时间点合并多条 rate 曲线。"""
    merged: dict[float, float] = {}
    for trends in trend_lists:
        for t, v in trends or []:
            key = round(float(t), 1)
            merged[key] = merged.get(key, 0.0) + (float(v) if v is not None else 0.0)
    return [(t, round(v, 1)) for t, v in sorted(merged.items())]


def _compute_token_trends(records: list, window_sec: float = 60.0) -> dict:
    """按业界 Counter+rate 口径离线近似 Input/Output/Total TPM 与 QPM。

    - Input TPM：prompt_tokens 按 start_time 归桶，近似请求注入/prefill 压力。
    - Output TPM：completion_tokens 按 [start_time + TTFT, completed_time] 区间均摊，
      比全部归到 completed_time 更接近流式生成阶段的真实吞吐。
    - Total TPM：Input TPM + Output TPM。
    - QPM：成功请求按 completed_time 归桶，近似 request_success_total 的 rate。
    """
    valid = [r for r in records if r.get("start_time") is not None and r.get("completed_time") is not None]
    if not valid:
        return {"input": [], "output": [], "total": [], "qpm": []}

    t0 = min(float(r["start_time"]) for r in valid)
    t_end = max(float(r["completed_time"]) for r in valid)
    if t_end <= t0:
        return {"input": [], "output": [], "total": [], "qpm": []}

    input_events = []
    output_intervals = []
    qpm_events = []
    for r in valid:
        st = float(r["start_time"])
        ct = float(r["completed_time"])
        prompt_tokens = float(r.get("prompt_tokens") or 0)
        completion_tokens = float(r.get("completion_tokens") or 0)
        if prompt_tokens > 0:
            input_events.append((st, prompt_tokens))
        if completion_tokens > 0:
            ttft = r.get("first_chunk_latency")
            output_start = st + float(ttft) if isinstance(ttft, (int, float)) and ttft >= 0 else st
            output_start = min(max(output_start, st), ct)
            output_intervals.append((output_start, ct, completion_tokens))
        qpm_events.append((ct, 1.0))

    input_trends = _point_rate_trends(input_events, t0, t_end, window_sec)
    output_trends = _interval_rate_trends(output_intervals, t0, t_end, window_sec)
    total_trends = _merge_rate_trends(input_trends, output_trends)
    qpm_trends = _point_rate_trends(qpm_events, t0, t_end, window_sec)
    return {"input": input_trends, "output": output_trends, "total": total_trends, "qpm": qpm_trends}


def _decode_response_messages(raw) -> list:
    """解码 evalscope 的 response_messages 列（base64(pickle(list))）。
    旧版可能为纯文本/JSON。解码失败返回 []。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str):
        return []
    import base64, pickle
    for fn in (lambda: pickle.loads(base64.b64decode(raw)),
               lambda: json.loads(raw)):
        try:
            val = fn()
            return val if isinstance(val, list) else [val]
        except Exception:
            continue
    return []


def _read_cache_hit(db_path) -> float | None:
    """从同 run 的 performance_summary.txt 读 Cache Hit (%)（evalscope 口径）。"""
    try:
        sm = Path(db_path).parent.parent / "performance_summary.txt"
        if not sm.exists():
            return None
        m = re.search(r"Cache Hit \(%\)\s*│\s*([0-9.]+)%", sm.read_text(encoding="utf-8", errors="ignore"))
        return float(m.group(1)) if m else None
    except Exception:
        return None


def calc_stats(db_path, label, parallel, tpm_window_sec=60.0):
    """从 benchmark_data.db 计算性能指标 + TPM/QPM 趋势 + 失败请求明细。

    返回 (stats_dict, failed_rows)；failed_rows 为 db 中 success != 1 的原始行。
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(result)")
    cols = [r[1] for r in cur.fetchall()]
    cur.execute("SELECT * FROM result")
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()

    success = [r for r in rows if r.get("success") == 1]
    failed = [r for r in rows if r.get("success") != 1]
    total = len(rows)

    if not success:
        return None, failed

    ttfts = [r["first_chunk_latency"] for r in success if r.get("first_chunk_latency") is not None]
    ttlts = [r["latency"] for r in success if r.get("latency") is not None]

    rates = []
    for r in success:
        ct = r.get("completion_tokens", 0) or 0
        lat = r.get("latency") or 0
        ttft = r.get("first_chunk_latency") or 0
        gen_t = lat - ttft
        if ct >= 5 and gen_t >= 0.1:
            rates.append(ct / gen_t)

    # TPOT(ms)：time per output token = (TTLT - TTFT) / (completion_tokens - 1) * 1000
    tpots = []
    for r in success:
        ct = r.get("completion_tokens", 0) or 0
        lat = r.get("latency") or 0
        ttft = r.get("first_chunk_latency") or 0
        gen_t = lat - ttft
        if ct >= 2 and gen_t >= 0:
            tpots.append(gen_t / (ct - 1) * 1000.0)

    prompt_tokens = [r.get("prompt_tokens", 0) or 0 for r in success]
    completion_tokens = [r.get("completion_tokens", 0) or 0 for r in success]
    total_tokens_sum = sum(prompt_tokens) + sum(completion_tokens)

    start_times = [r["start_time"] for r in success]
    completed_times = [r["completed_time"] for r in success]
    total_time_s = max(completed_times) - min(start_times)

    output_tps = sum(completion_tokens) / total_time_s if total_time_s > 0 else 0
    output_tpm = output_tps * 60
    input_tps = sum(prompt_tokens) / total_time_s if total_time_s > 0 else 0
    input_tpm = input_tps * 60
    tpm = input_tpm + output_tpm

    # TPM/QPM 滑动窗口趋势：按业界 Counter+rate 口径离线近似（默认 60s 窗口，50% 重叠）
    token_trends = _compute_token_trends(success, window_sec=float(tpm_window_sec)) if success else {
        "input": [], "output": [], "total": [], "qpm": []
    }

    return {
        "数据集": label,
        "并发数": parallel,
        "总请求数": total,
        "成功数": len(success),
        "失败数": len(failed),
        "成功率": len(success) / total if total > 0 else 0,
        "Avg TTFT(s)": safe_avg(ttfts),
        "Min TTFT(s)": min(ttfts) if ttfts else None,
        "Max TTFT(s)": max(ttfts) if ttfts else None,
        "P50 TTFT(s)": percentile(ttfts, 50),
        "P90 TTFT(s)": percentile(ttfts, 90),
        "P95 TTFT(s)": percentile(ttfts, 95),
        "P99 TTFT(s)": percentile(ttfts, 99),
        "Avg TTLT(s)": safe_avg(ttlts),
        "P50 TTLT(s)": percentile(ttlts, 50),
        "P90 TTLT(s)": percentile(ttlts, 90),
        "P95 TTLT(s)": percentile(ttlts, 95),
        "P99 TTLT(s)": percentile(ttlts, 99),
        "Avg Rate(t/s)": safe_avg(rates),
        "P50 Rate(t/s)": percentile(rates, 50),
        "P90 Rate(t/s)": percentile(rates, 90),
        "P95 Rate(t/s)": percentile(rates, 95),
        "P99 Rate(t/s)": percentile(rates, 99),
        "Avg TPOT(ms)": safe_avg(tpots),
        "P50 TPOT(ms)": percentile(tpots, 50),
        "P90 TPOT(ms)": percentile(tpots, 90),
        "P95 TPOT(ms)": percentile(tpots, 95),
        "P99 TPOT(ms)": percentile(tpots, 99),
        "Avg Prompt Tokens": safe_avg(prompt_tokens),
        "Avg Completion Tokens": safe_avg(completion_tokens),
        "Total Tokens": total_tokens_sum,
        "Avg Cached Tokens": (safe_avg(prompt_tokens) or 0) * (_read_cache_hit(db_path) or 0) / 100,
        "Avg Cache Hit Ratio": (_read_cache_hit(db_path) or 0) / 100,
        "Avg Total Time(ms)": (safe_avg(ttlts) or 0) * 1000,
        "Output TPM": output_tpm,
        "Output TPS": output_tps,
        "Input TPM": input_tpm,
        "TPM": tpm,
        # 内部用，不写入「性能指标」sheet
        "_input_tpm_trends": token_trends["input"],
        "_output_tpm_trends": token_trends["output"],
        "_total_tpm_trends": token_trends["total"],
        "_tpm_trends": token_trends["total"],  # 兼容旧 HTML 逻辑
        "_qpm_trends": token_trends["qpm"],
        "_parallel": parallel,
        "_bucket": label,
        "_avg_prompt_tokens": safe_avg(prompt_tokens),
    }, failed


# 表头以 references/性能测试报告模板.xlsx「矩阵压测汇总」Sheet 第一行为准（40 列），
# 此处仅声明数据 sheet 名，禁止从零手写表头（模板强制规则）。
_SHEET_SUMMARY = "矩阵压测汇总"
_SHEET_FAILURES = "失败请求详情"


def find_run_dirs(results_root: Path):
    """扫描 results/ 下含 benchmark_data.db 的运行目录，返回 (run_dir, db_path, vendor, bucket, parallel, run_idx)。

    排序：按输入长度档位（1k → 9k → 16k → 32k → 64k → 128k → 200k），
    同档位内按并发数升序。

    目录约定：
      bench:    results/<vendor>-<bucket>-<ts>/<model>/parallel_<P>_number_<N>/benchmark_data.db
      sla-tune: results/sla-tune-<bucket>-<ts>/<model>/sla_tuning/sla_parallel_<P>_run_<i>/benchmark_data.db
    （.bak 归档目录跳过）
    """
    runs = []
    # os.walk + followlinks: 支持 group_dir 内的 run 软链 (rglob 不跟随 symlink)
    dbs = []
    for root, dirs, files in os.walk(results_root, followlinks=True):
        if "benchmark_data.db" in files:
            dbs.append(Path(root) / "benchmark_data.db")
    for db in dbs:
        if ".bak" in str(db):
            continue
        # db 路径形如 results/<run-tag>/<model>/parallel_<P>_number_<N>/benchmark_data.db
        # 或 sla-tune：results/sla-tune-<bucket>-<ts>/<model>/sla_tuning/sla_parallel_<P>_run_<i>/benchmark_data.db
        try:
            parts = db.relative_to(results_root).parts
            run_tag = parts[0]                      # 例如 "tokenhub-1k-20260519-1008-r2" / "sla-tune-1k-20260918-1531"
            parallel_dir = parts[-2]                # 例如 "parallel_10_number_500" / "sla_parallel_8_run_0"
            m = re.match(r"parallel_(\d+)_number_(\d+)", parallel_dir)
            parallel = int(m.group(1)) if m else 0
            run_idx = None
            m = re.match(r"sla_parallel_(\d+)_run_(\d+)", parallel_dir)
            if m:
                parallel, run_idx = int(m.group(1)), int(m.group(2))
            # 解析 vendor 和 bucket，约定 <vendor>-<bucket>-<timestamp>...
            tag_parts = run_tag.split("-")
            vendor = tag_parts[0]
            if run_tag.startswith("sla-tune-"):
                # sla-tune 结构下 vendor 目录不可用，取模型名目录（parts[-3] 为 sla_tuning 的上一级）
                vendor = parts[-3] if len(parts) >= 4 else "sla-tune"
            # 找第一个形如 1k/9k/16k/50k 的 token 档
            bucket = next((p for p in tag_parts if re.fullmatch(r"\d+k", p)), "?")
            runs.append((run_tag, db, vendor, bucket, parallel, run_idx))
        except Exception:
            continue
    runs.sort(key=lambda r: (_bucket_num(r[3]), r[4], r[0], r[5] if r[5] is not None else 0))
    return runs


# 染色常量（模板说明 Sheet 规则）
_FILL_SR_FULL = PatternFill("solid", fgColor="C6EFCE")    # 成功率 =100% 绿
_FILL_SR_WARN = PatternFill("solid", fgColor="FFEB9C")    # 成功率 ≥95% 黄
_FILL_SR_FAIL = PatternFill("solid", fgColor="FFC7CE")    # 成功率 <95% 红
_FILL_SLA = PatternFill("solid", fgColor="C00000")        # SLA 超阈值红底
_FONT_SLA = Font(bold=True, color="FFFFFF")               # SLA 白字加粗


def _style_summary_cell(cell, header, value):
    """按表头类型设置数字格式。"""
    if header.startswith(("Avg ", "Min ", "Max ", "P50 ", "P90 ", "P95 ", "P99 ")):
        if "(s)" in header or "(t/s)" in header:
            cell.number_format = "0.000"
        elif "(ms)" in header:
            cell.number_format = "0.00"
        elif "Tokens" in header:
            cell.number_format = "0.0"
    elif header in ("成功率", "Avg Cache Hit Ratio"):
        if isinstance(value, (int, float)):
            cell.number_format = "0.00%" if 0 <= value <= 1 else "0.00"
    elif header == "Total Tokens":
        cell.number_format = "0"
    elif header in ("Output TPM", "Output TPS", "Input TPM", "TPM"):
        cell.number_format = "0.00"
    elif header in ("总请求数", "成功数", "失败数", "并发数"):
        cell.number_format = "0"


_SLA_TIER_ORDER = ["<4K", "<8K", "<32K", "<64K", "<128K", "<256K"]


# bucket → tier 硬映射，与 run.sh sla_params_for_bucket / evalscope auto-tune 完全一致；
# classify_tier（按实际平均输入长度归档）在 32k 档（输入 28k~36k 跨 <32K/<64K 边界）会抖动错档
_BUCKET_TIER = {
    "1k": "<4K",
    "9k": "<32K",
    "16k": "<32K",
    "32k": "<64K",
    "64k": "<128K",
    "128k": "<256K",
    "200k": "<256K",
}


def _tier_for_bucket(bucket):
    return _BUCKET_TIER.get(str(bucket).lower())


def _avg_group(group, key):
    vals = [g.get(key) for g in group if g.get(key) is not None]
    return (sum(vals) / len(vals)) if vals else None


def build_sla_bucket_rows(rows):
    """按测试档位（bucket：1k/9k/.../200k）汇总，输出每档满足 SLA 的最佳并发数据。

    SLA 判定 3 项（腾讯验收口径，run 级），与 sla_eval / evalscope auto-tune 一致：
      1. TTFT：Avg TTFT 对标 P50 阈值 + P90 TTFT 对标 P90 阈值
         （该档 run 平均输入长度归入 sla_eval 6 档取阈值）
      2. 成功率：= 100%
      3. OTPS：Avg Rate(t/s)（单请求生成速率）≥ 30 t/s
    最佳并发 = 满足 SLA 的 run 中并发最大者（该档位满足 SLA 的最大吞吐点）。
    """
    bucket_rows = {}
    for r in rows:
        b = r.get("_bucket")
        if b:
            bucket_rows.setdefault(b, []).append(r)

    result = []
    for bucket in sorted(bucket_rows, key=_bucket_num):
        rs = bucket_rows[bucket]
        # tier 优先按 bucket 名硬映射（与 run.sh sla_params_for_bucket / auto-tune 一致），
        # 避免 classify_tier 按实际平均输入归档在档位边界抖动（如 32k 档输入 28k~36k 跨 <32K/<64K）
        tier = _tier_for_bucket(bucket) or classify_tier(
            safe_avg([r.get("_avg_prompt_tokens") for r in rs
                      if isinstance(r.get("_avg_prompt_tokens"), (int, float))]))
        th = get_tier_thresholds(tier)
        # 同并发多 run（sla-tune num_runs>1 产生 run_0/run_1）聚合平均后再判定，
        # 与 evalscope auto-tune 的 num_runs 平均判定口径一致
        by_parallel = {}
        for r in rs:
            by_parallel.setdefault(r.get("_parallel", 0), []).append(r)
        agg_rows = []
        for _p, group in by_parallel.items():
            agg = dict(group[0])
            for k in ("Avg TTFT(s)", "P50 TTFT(s)", "P90 TTFT(s)", "成功率",
                      "Avg Rate(t/s)", "TPM", "_avg_prompt_tokens"):
                if k in group[0] or any(k in g for g in group):
                    agg[k] = _avg_group(group, k)
            agg_rows.append(agg)
        # 达标口径：Avg TTFT 对标 P50 阈值（非 P50 TTFT），与 sla_eval / auto-tune 一致
        passing = [
            r for r in agg_rows
            if isinstance(r.get("Avg TTFT(s)"), (int, float)) and r["Avg TTFT(s)"] <= th["p50"]
            and isinstance(r.get("P90 TTFT(s)"), (int, float)) and r["P90 TTFT(s)"] <= th["p90"]
            and isinstance(r.get("成功率"), (int, float)) and r["成功率"] >= 0.9999
            and isinstance(r.get("Avg Rate(t/s)"), (int, float))
            and r["Avg Rate(t/s)"] >= RATE_MIN_TOKENS_PER_SEC
        ]
        # 最佳并发 = 达标 run 中并发最大
        best = max(passing, key=lambda r: r.get("_parallel", 0)) if passing else None
        result.append({
            "bucket": bucket,
            "sla_pass": best is not None,
            "p50_threshold": th["p50"],
            "p90_threshold": th["p90"],
            "best_parallel": best.get("_parallel") if best else None,
            "avg": best.get("Avg TTFT(s)") if best else None,
            "p50": best.get("P50 TTFT(s)") if best else None,
            "p90": best.get("P90 TTFT(s)") if best else None,
            "success_rate": best.get("成功率") if best else None,
            "otps": best.get("Avg Rate(t/s)") if best else None,
            "tpm": best.get("TPM") if best else None,
        })
    return result


def _apply_row_colors(ws, ridx, row_stats):
    """按模板规则染色：成功率 Col H 分档 + SLA 超阈值（红底白字加粗）。"""
    # 成功率染色（Col H，0~1 小数）
    sr = row_stats.get("成功率")
    if isinstance(sr, (int, float)):
        cell = ws.cell(row=ridx, column=8)
        if sr >= 0.9999:
            cell.fill = _FILL_SR_FULL
        elif sr >= 0.95:
            cell.fill = _FILL_SR_WARN
        else:
            cell.fill = _FILL_SR_FAIL

    # SLA 染色（3 项判定，腾讯验收口径）：
    #   1. TTFT：col 9 Avg TTFT / col 13 P90 TTFT 超档位阈值 → 红底白字加粗
    #   2. 成功率：Col H 三档染色（上方已处理），<100% 即 SLA 不达标
    #   3. OTPS：col 21 Avg Rate(t/s)（OTPS 口径：单请求生成速率）< 30 → 红底白字加粗
    tier = classify_tier(row_stats.get("_avg_prompt_tokens"))
    th = get_tier_thresholds(tier)
    avg_ttft = row_stats.get("Avg TTFT(s)")
    if isinstance(avg_ttft, (int, float)) and avg_ttft > th["p50"]:
        c = ws.cell(row=ridx, column=9)
        c.fill, c.font = _FILL_SLA, _FONT_SLA
    p90_ttft = row_stats.get("P90 TTFT(s)")
    if isinstance(p90_ttft, (int, float)) and p90_ttft > th["p90"]:
        c = ws.cell(row=ridx, column=13)
        c.fill, c.font = _FILL_SLA, _FONT_SLA
    otps = row_stats.get("Avg Rate(t/s)")
    if isinstance(otps, (int, float)) and otps <= RATE_MIN_TOKENS_PER_SEC:
        c = ws.cell(row=ridx, column=21)
        c.fill, c.font = _FILL_SLA, _FONT_SLA


def main():
    ap = argparse.ArgumentParser(description="从 benchmark_data.db 生成模板对齐的性能报告（3 Sheets）")
    ap.add_argument("--results-dir", default="results", help="results 目录（默认 ./results）")
    ap.add_argument("--vendor", default="unknown", help="供应商名称（写入 Sheet1 Col A 与报告文件名）")
    ap.add_argument("--model", default="unknown", help="模型名称（写入 Sheet1 Col B 与报告文件名）")
    ap.add_argument("--out", default="", help="输出 xlsx 路径；默认 <results>/性能测试报告_{vendor}_{model}_{ts}.xlsx")
    ap.add_argument("--filter", default="", help="只匹配 run-tag 包含该字符串的实验")
    ap.add_argument("--tpm-window-sec", type=float, default=60.0, help="TPM/QPM 趋势图滑动窗口秒数，默认 60")
    ap.add_argument("--no-tpm-html", action="store_true", help="不生成 TPM 趋势 HTML（默认与 xlsx 同名伴随生成）")
    args = ap.parse_args()

    root = Path(args.results_dir)
    runs = find_run_dirs(root)
    if args.filter:
        # 支持逗号分隔多个子串（任一匹配即保留），如 "sla-tune-1k-...,sla-tune-9k-,..."
        filters = [f.strip() for f in args.filter.split(",") if f.strip()]
        runs = [r for r in runs if any(f in r[0] for f in filters)]
    if not runs:
        print(f"[warn] {root} 下未找到任何 benchmark_data.db", file=sys.stderr)
        sys.exit(1)

    rows = []
    failed_details = []
    for run_tag, db, vendor, bucket, parallel, run_idx in runs:
        stats, failed_rows = calc_stats(db, bucket, parallel, args.tpm_window_sec)
        if not stats:
            print(f"[skip] {run_tag} 无成功请求")
            if failed_rows:
                for fr in failed_rows:
                    failed_details.append((run_tag, bucket, parallel, fr))
            continue
        # sla-tune 结构下同并发有 run_0/run_1 多行，用 run 序号区分
        suffix = f" r{run_idx}" if run_idx is not None else ""
        stats["数据集"] = f"{bucket}{suffix} ({run_tag})"
        rows.append(stats)
        print(f"[{run_tag}] success={stats['成功数']}/{stats['总请求数']} | "
              f"TTFT={stats['Avg TTFT(s)']:.3f}s | TTLT={stats['Avg TTLT(s)']:.3f}s | "
              f"OutTPS={stats['Output TPS']:.2f}")
        for fr in failed_rows:
            failed_details.append((run_tag, bucket, parallel, fr))

    if not rows:
        print("[warn] 没有可用的 row", file=sys.stderr)
        sys.exit(1)

    # ---- 写 xlsx（模板强制规则：load_workbook 加载模板副本填充，禁止从零创建）----
    if not _TEMPLATE_XLSX.is_file():
        print(f"[fail] 模板不存在: {_TEMPLATE_XLSX}", file=sys.stderr)
        sys.exit(1)
    wb = openpyxl.load_workbook(_TEMPLATE_XLSX)
    ws = wb[_SHEET_SUMMARY]

    headers = [c.value for c in ws[1]]  # 表头以模板第一行为准
    for ridx, r in enumerate(rows, start=2):
        for c, h in enumerate(headers, start=1):
            if h == "供应商名称":
                v = args.vendor
            elif h == "模型名称":
                v = args.model
            else:
                v = r.get(h)
            cell = ws.cell(row=ridx, column=c, value=v)
            _style_summary_cell(cell, h, v)
            # TPM 类列（Output TPM / Input TPM / TPM）：公式换算为万单位显示（xx.xx万），
            # 原值保留在公式中（=原值/10000），便于核对；SLA 染色仍按 stats 原值判定
            if h in ("Output TPM", "Input TPM", "TPM") and isinstance(v, (int, float)):
                cell.value = f"={v}/10000"
                cell.number_format = '0.00"万"'
        _apply_row_colors(ws, ridx, r)

    widths = {"供应商名称": 14, "模型名称": 18, "数据集": 30, "并发数": 8,
              "总请求数": 10, "成功数": 8, "失败数": 8, "成功率": 9}
    for c, h in enumerate(headers, start=1):
        col_letter = ws.cell(row=1, column=c).column_letter
        ws.column_dimensions[col_letter].width = widths.get(h, 14)

    # ---- 失败请求详情 Sheet（14 列，模板自带表头）----
    ws_fail = wb[_SHEET_FAILURES]
    for i, (run_tag, bucket, parallel, fr) in enumerate(failed_details, start=2):
        req_body = fr.get("request")
        # response_messages 为 base64(pickle(list)) —— 解码为可读文本再入表,
        # 否则 Excel 里会显示 "gARdlC4=" 这类编码串
        resp_raw = fr.get("response_messages")
        objs = _decode_response_messages(resp_raw)
        if objs:
            resp_body = str(objs)
        elif isinstance(resp_raw, str) and not resp_raw.startswith("gAR") and resp_raw.strip():
            resp_body = resp_raw  # 旧版纯文本
        elif isinstance(resp_raw, str) and resp_raw.strip():
            # base64(pickle) 解不开 = 失败请求无响应体 (evalscope 存空壳 pickle)
            resp_body = "(请求失败, 无响应体)"
        else:
            resp_body = "(无响应体)"
        vals = [
            i - 1,                                   # 序号
            f"{bucket} ({run_tag})",                 # 数据集
            parallel,                                # 并发数
            "-",                                     # 请求URL（db 未记录）
            "-",                                     # 请求方法（db 未记录）
            "-",                                     # 请求Headers（db 未记录，API Key 已天然脱敏）
            _truncate(_mask_api_key(req_body)),      # 请求Body（2000 字符截断）
            "-",                                     # HTTP状态码（db 未记录）
            _truncate(resp_body),                    # 响应Body（2000 字符截断）
            "-",                                     # Request ID（db 未记录）
            fr.get("first_chunk_latency"),           # TTFT(s)
            fr.get("latency"),                       # TTLT(s)
            "请求失败",                               # 错误类型
            f"run={run_tag} 行内记录",                # 详细说明
        ]
        for c, v in enumerate(vals, start=1):
            cell = ws_fail.cell(row=i, column=c, value=v)
            if c in (11, 12) and isinstance(v, (int, float)):
                cell.number_format = "0.000"
            elif c == 3:
                cell.number_format = "0"

    # ---- SLA 档位汇总 Sheet（各测试档位满足 SLA 的最佳并发）----
    sla_rows = build_sla_bucket_rows(rows)
    ws_sla = wb.create_sheet("SLA 档位汇总")
    sla_headers = [
        "输入长度档位",
        "P50 TTFT 阈值(s)", "P90 TTFT 阈值(s)",
        "SLA 达标", "最佳并发",
        "最佳并发 Avg TTFT(s)", "最佳并发 P90 TTFT(s)",
        "成功率", "Avg Rate(OTPS t/s)", "TPM(万)",
    ]
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for c, h in enumerate(sla_headers, start=1):
        cell = ws_sla.cell(row=1, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
    ws_sla.freeze_panes = "A2"

    fill_ok = PatternFill("solid", fgColor="C6EFCE")
    fill_bad = PatternFill("solid", fgColor="FFC7CE")
    for ridx, tr in enumerate(sla_rows, start=2):
        vals = [
            tr["bucket"],
            tr["p50_threshold"], tr["p90_threshold"],
            "✓" if tr["sla_pass"] else "✗",
            tr["best_parallel"],
            tr["avg"], tr["p90"],
            tr["success_rate"],
            tr["otps"],
            tr["tpm"],
        ]
        for c, v in enumerate(vals, start=1):
            cell = ws_sla.cell(row=ridx, column=c, value=v if v is not None else "-")
            if c in (2, 3, 6, 7):
                cell.number_format = "0.000"
            elif c == 9:
                cell.number_format = "0.00"
            elif c == 5:
                cell.number_format = "0"
            elif c == 8:
                cell.number_format = "0.00%"
        # TPM 万单位：公式保留原值（与矩阵压测汇总口径一致）
        cell = ws_sla.cell(row=ridx, column=10)
        if isinstance(cell.value, (int, float)):
            cell.value = f"={cell.value}/10000"
            cell.number_format = '0.00"万"'
        # SLA 达标染色（col 4）
        ws_sla.cell(row=ridx, column=4).fill = fill_ok if tr["sla_pass"] else fill_bad
    for c, h in enumerate(sla_headers, start=1):
        col_letter = ws_sla.cell(row=1, column=c).column_letter
        ws_sla.column_dimensions[col_letter].width = 16

    out_path = Path(args.out) if args.out else (
        root / f"性能测试报告_{args.vendor}_{args.model}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f"\n[OK] Saved: {out_path}")
    print(f"     矩阵压测汇总: {len(rows)} rows x {len(headers)} cols")
    print(f"     失败请求详情: {len(failed_details)} rows")
    print(f"     SLA 档位汇总: {len(sla_rows)} rows")

    # ── TPM/QPM HTML 趋势图 ──
    if not args.no_tpm_html:
        write_tpm_html(out_path, rows)


def write_tpm_html(xlsx_path, rows):
    # 基于 Chart.js 生成 Input/Output/Total TPM & QPM 趋势 HTML，与 xlsx 同目录。
    if not rows:
        return
    from collections import defaultdict

    groups = defaultdict(list)
    for r in rows:
        label = r.get("数据集", "?")
        groups[label].append(r)

    palette_str = json.dumps([
        {"border": "#2563eb", "bg": "rgba(37,99,235,.10)"},
        {"border": "#dc2626", "bg": "rgba(220,38,38,.10)"},
        {"border": "#059669", "bg": "rgba(5,150,105,.10)"},
        {"border": "#d97706", "bg": "rgba(217,119,6,.10)"},
        {"border": "#7c3aed", "bg": "rgba(124,58,237,.10)"},
        {"border": "#0891b2", "bg": "rgba(8,145,178,.10)"},
        {"border": "#be185d", "bg": "rgba(190,24,93,.10)"},
    ], ensure_ascii=False)

    js_datasets = {}
    metric_defs = [
        ("total", "Total TPM"),
        ("input", "Input TPM"),
        ("output", "Output TPM"),
        ("qpm", "QPM"),
    ]
    for label, group in sorted(groups.items()):
        parallels = sorted(set(r.get("_parallel", 0) for r in group))
        trends_by_metric = {key: {} for key, _ in metric_defs}
        all_times = set()
        for r in group:
            p = r.get("_parallel", 0)
            trends_by_metric["total"][p] = r.get("_total_tpm_trends") or r.get("_tpm_trends", [])
            trends_by_metric["input"][p] = r.get("_input_tpm_trends", [])
            trends_by_metric["output"][p] = r.get("_output_tpm_trends", [])
            trends_by_metric["qpm"][p] = r.get("_qpm_trends", [])
            for metric_map in trends_by_metric.values():
                for t, _ in metric_map.get(p, []) or []:
                    all_times.add(round(t, 1))

        time_axis = sorted(all_times)

        def _lookup(trends, t):
            for tt, tv in trends or []:
                if abs(tt - t) < 0.1:
                    return tv
            return None

        rows_out = []
        cols = {}
        for metric_key, metric_title in metric_defs:
            cols[metric_key] = [f"P{p} {metric_title}" for p in parallels]
        for t in time_axis:
            rec = {"时间(s)": t}
            for metric_key, metric_title in metric_defs:
                for p in parallels:
                    rec[f"P{p} {metric_title}"] = _lookup(trends_by_metric[metric_key].get(p, []), t)
            rows_out.append(rec)
        js_datasets[label] = {"rows": rows_out, "cols": cols}

    labels = list(js_datasets.keys())
    if not labels:
        return
    first_label = labels[0]
    html_path = str(xlsx_path).rsplit(".", 1)[0] + "_TPM.html"
    html = _HTML_TPM_TEMPLATE.format(
        labels_json=json.dumps(js_datasets, ensure_ascii=False),
        first_label=first_label,
        palette=palette_str,
        options_html="\n".join(f'<option value="{l}">{l}</option>' for l in labels),
    )
    html = html.replace("__CHART_JS_TAG__", _chart_js_inline_or_cdn())
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"   TPM 趋势 HTML: {html_path}")


_HTML_TPM_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>TPM & QPM 趋势图</title>
__CHART_JS_TAG__
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f5f7fa;color:#333;padding:20px}}
.container{{max-width:1280px;margin:0 auto}}
h1{{font-size:22px;margin-bottom:12px;color:#1a1a2e;border-left:4px solid #4361ee;padding-left:12px}}
.desc{{font-size:13px;line-height:1.7;color:#5b6472;margin-bottom:16px;background:#fff;border-radius:10px;padding:14px 18px;box-shadow:0 2px 12px rgba(0,0,0,.06)}}
.card{{background:#fff;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.08);padding:22px;margin-bottom:20px}}
.card h2{{font-size:16px;margin-bottom:12px;color:#1f2937}}
.chart-wrap{{position:relative;height:420px;width:100%}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th{{background:#f0f3ff;color:#4361ee;font-weight:600;padding:8px 12px;text-align:center;border:1px solid #e0e4ed;white-space:nowrap}}
td{{padding:6px 12px;text-align:right;border:1px solid #e8ecf2}}
td:first-child{{text-align:left;font-weight:500;background:#fafbff}}
tr:hover td{{background:#f8faff}}
.legend{{display:flex;gap:18px;justify-content:center;margin-bottom:12px;flex-wrap:wrap}}
.legend-item{{display:flex;align-items:center;gap:6px;font-size:13px}}
.dot{{width:12px;height:12px;border-radius:50%}}
.selector-bar{{display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}}
.selector-bar label{{font-size:15px;font-weight:600;color:#1a1a2e}}
.selector-bar select{{font-size:15px;padding:6px 16px;border-radius:8px;border:1px solid #d0d5e0;background:#fff;color:#333;cursor:pointer;outline:none}}
.selector-bar select:focus{{border-color:#4361ee;box-shadow:0 0 0 2px rgba(67,97,238,.18)}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
@media(max-width:980px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body><div class="container">
<h1>性能趋势分析</h1>
<div class="desc">
  <b>口径：</b>Input TPM 按 <code>start_time</code> 归桶，近似请求注入 / prefill 压力；
  Output TPM 将 <code>completion_tokens</code> 均摊到 <code>start_time + TTFT</code> 到 <code>completed_time</code> 区间，近似流式生成吞吐；
  Total TPM = Input TPM + Output TPM；QPM 按成功请求 <code>completed_time</code> 归桶。默认 60s 滑动窗口，30s 步长。
</div>
<div class="selector-bar"><label for="labelSelect">数据档位：</label><select id="labelSelect">{options_html}</select></div>
<div class="card"><h2>Total TPM</h2><div class="legend" id="totalLegend"></div><div class="chart-wrap"><canvas id="totalChart"></canvas></div></div>
<div class="grid">
  <div class="card"><h2>Input TPM</h2><div class="legend" id="inputLegend"></div><div class="chart-wrap"><canvas id="inputChart"></canvas></div></div>
  <div class="card"><h2>Output TPM</h2><div class="legend" id="outputLegend"></div><div class="chart-wrap"><canvas id="outputChart"></canvas></div></div>
</div>
<div class="card"><h2>QPM</h2><div class="legend" id="qpmLegend"></div><div class="chart-wrap"><canvas id="qpmChart"></canvas></div></div>
<div class="card"><h2>趋势数据明细表</h2><table id="trendTable"><thead><tr></tr></thead><tbody></tbody></table></div>
</div>
<script>
window.addEventListener("load",function(){{
var ALL={labels_json};
var PALETTE={palette};
var charts={{}};
var METRICS={{
  total:{{chartId:"totalChart",legendId:"totalLegend",title:"Total TPM (tokens/min)"}},
  input:{{chartId:"inputChart",legendId:"inputLegend",title:"Input TPM (tokens/min)"}},
  output:{{chartId:"outputChart",legendId:"outputLegend",title:"Output TPM (tokens/min)"}},
  qpm:{{chartId:"qpmChart",legendId:"qpmLegend",title:"QPM (queries/min)"}}
}};
function buildSeries(RAW,cols){{
  var s={{}};
  RAW.forEach(function(r){{
    var t=r["时间(s)"];
    cols.forEach(function(k){{
      if(!s[k])s[k]=[];
      var v=r[k];
      if(v!==null&&v!==""&&v!==undefined)s[k].push({{x:t,y:Number(v)}});
    }});
  }});
  return s;
}}
function fmt(v){{return Math.abs(v)>=1000000?(v/1000000).toFixed(2)+"M":Math.abs(v)>=1000?(v/1000).toFixed(1)+"k":v;}}
function makeChart(metricKey,series){{
  var cfg=METRICS[metricKey];
  var keys=Object.keys(series).sort(function(a,b){{return parseInt((a.match(/P(\d+)/)||[])[1]||0)-parseInt((b.match(/P(\d+)/)||[])[1]||0);}});
  var leg=document.getElementById(cfg.legendId);leg.innerHTML="";
  keys.forEach(function(k,i){{
    var s=PALETTE[i%PALETTE.length];
    var d=document.createElement("div");d.className="legend-item";
    d.innerHTML='<span class="dot" style="background:'+s.border+'"></span>'+k;
    leg.appendChild(d);
  }});
  return new Chart(document.getElementById(cfg.chartId),{{
    type:"line",
    data:{{datasets:keys.map(function(k,i){{
      var s=PALETTE[i%PALETTE.length];
      return{{label:k,data:series[k],borderColor:s.border,backgroundColor:s.bg,fill:false,tension:.18,pointRadius:2.5,pointHoverRadius:7,pointBackgroundColor:s.border,pointBorderColor:"#fff",borderWidth:2.2}};
    }})}},
    options:{{
      responsive:true,maintainAspectRatio:false,interaction:{{mode:"index",intersect:false}},
      plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{title:function(i){{return"时间: "+i[0].parsed.x+"s"}},label:function(i){{return i.dataset.label+": "+Number(i.parsed.y).toLocaleString()}}}}}}}},
      scales:{{x:{{type:"linear",title:{{display:true,text:"时间 (s)"}},grid:{{color:"#eee"}},ticks:{{stepSize:30}}}},y:{{title:{{display:true,text:cfg.title}},grid:{{color:"#eee"}},ticks:{{callback:function(v){{return fmt(v)}}}}}}}}
    }}
  }});
}}
function makeTable(RAW,colsByMetric){{
  var thead=document.querySelector("#trendTable thead tr");thead.innerHTML="";
  var allCols=["时间(s)"];
  ["total","input","output","qpm"].forEach(function(k){{allCols=allCols.concat(colsByMetric[k]||[]);}});
  allCols.forEach(function(c){{var th=document.createElement("th");th.textContent=c;thead.appendChild(th);}});
  var tbody=document.querySelector("#trendTable tbody");tbody.innerHTML="";
  RAW.forEach(function(r){{
    var tr=document.createElement("tr");
    allCols.forEach(function(c){{
      var td=document.createElement("td");
      if(c==="时间(s)")td.textContent=r[c];
      else{{var v=r[c];td.textContent=(v!==null&&v!==undefined&&v!=="")?Number(v).toLocaleString():"-";}}
      tr.appendChild(td);
    }});
    tbody.appendChild(tr);
  }});
}}
function render(label){{
  var ds=ALL[label];if(!ds)return;
  Object.keys(charts).forEach(function(k){{if(charts[k]){{charts[k].destroy();charts[k]=null;}}}});
  ["total","input","output","qpm"].forEach(function(k){{
    var series=buildSeries(ds.rows,ds.cols[k]||[]);
    charts[k]=makeChart(k,series);
  }});
  makeTable(ds.rows,ds.cols);
}}
render("{first_label}");
document.getElementById("labelSelect").addEventListener("change",function(){{render(this.value);}});
}});
</script></body></html>'''


if __name__ == "__main__":
    main()
