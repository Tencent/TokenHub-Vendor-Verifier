#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sla_eval.py — 性能测试 SLA 验收评估

职责：
  1. classify_tier(avg_prompt_tokens) → 输入长度档位（<4K / <8K / <32K / <64K / <128K / <256K）
  2. get_tier_thresholds(tier)         → 该档位 TTFT P50/P90 阈值
  3. evaluate_sla(aggregated_json, user_tpm_quota)
       → 全局 SLA 结果（per_row_pass / per_row_violations / tier_summary / overall_pass）

使用方式：
  from scripts.sla_eval import (
      classify_tier, get_tier_thresholds, evaluate_sla,
      evaluate_sla_from_evalscope_group,   # v3.x evalscope 一站式适配
  )

或命令行（双入口，互斥）：
  # (1) v2.x 旧链路（matrix_aggregated.json）
  python scripts/sla_eval.py \
      --aggregated-json /path/to/matrix_aggregated.json \
      --user-tpm-quota 5000000 \
      --out /path/to/sla_evaluation.json

  # (2) v3.x evalscope 一体化压测产物（推荐）
  python scripts/sla_eval.py \
      --evalscope-group-dir /path/to/results/_group_<RUN_GROUP> \
      --user-tpm-quota 5000000 \
      --out /path/to/sla_evaluation.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 档位定义（单位：token）——上界取"不含"口径，便于用 lo <= x < hi 判定
# ---------------------------------------------------------------------------
TIERS: List[Tuple[str, int, int, Dict[str, float]]] = [
    # (tier_name, lo_inclusive, hi_exclusive, {"p50": TTFT阈值秒, "p90": TTFT阈值秒})
    ("<4K",   1_000,   4_000,   {"p50": 2.0,  "p90": 5.0}),
    ("<8K",   4_000,   8_000,   {"p50": 2.5,  "p90": 5.0}),
    ("<32K",  8_000,   32_000,  {"p50": 4.0,  "p90": 8.0}),
    ("<64K",  32_000,  64_000,  {"p50": 8.0,  "p90": 15.0}),
    ("<128K", 64_000,  128_000, {"p50": 15.0, "p90": 35.0}),
    ("<256K", 128_000, 256_000, {"p50": 30.0, "p90": 70.0}),
]

# 通用吞吐阈值。报告标红里的绝对阈值已暂停，见 ABSOLUTE_SLA_ENABLED。
RATE_MIN_TOKENS_PER_SEC = 30.0   # avg_token_rates 必须 > 30
ABSOLUTE_SLA_ENABLED = False


def classify_tier(avg_prompt_tokens: Optional[float]) -> Optional[str]:
    """按 avg_prompt_tokens 归档。越界（< 1K 或 ≥ 256K）返回 None。"""
    if avg_prompt_tokens is None:
        return None
    try:
        v = float(avg_prompt_tokens)
    except (TypeError, ValueError):
        return None
    for name, lo, hi, _ in TIERS:
        if lo <= v < hi:
            return name
    return None


def get_tier_thresholds(tier: Optional[str]) -> Dict[str, float]:
    """返回该档位的 TTFT P50/P90 阈值；未知档位给出放行值（不参与 SLA 判定）。"""
    if tier is None:
        return {"p50": float("inf"), "p90": float("inf")}
    for name, _lo, _hi, th in TIERS:
        if name == tier:
            return dict(th)
    return {"p50": float("inf"), "p90": float("inf")}


# ---------------------------------------------------------------------------
# 主入口：对 matrix_aggregated.json 做逐行 + 档位级 SLA 评估
# ---------------------------------------------------------------------------
def _safe_get(d: Any, *path, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def evaluate_sla(
    aggregated_json: Path,
    user_tpm_quota: int = 5_000_000,
) -> Dict[str, Any]:
    """
    读取矩阵压测聚合 JSON，输出 SLA 评估结果。

    返回 schema:
        {
          "user_tpm_quota": int,
          "per_row_pass":        [bool, ...],           # 与 rows 一一对应
          "per_row_violations":  [[str, ...], ...],     # 每行的不达标原因列表
          "per_row_tier":        [Optional[str], ...],  # 每行的档位
          "tier_summary": [
              {"tier": "<4K", "row_indices": [1, 3],
               "p50_ttfb_all": 1.8, "p90_ttfb_all": 4.5,
               "p50_threshold": 2.0, "p90_threshold": 5.0,
               "p50_pass": True, "p90_pass": True},
              ...
          ],
          "overall_pass": bool,
        }
    """
    data = json.loads(Path(aggregated_json).read_text(encoding="utf-8"))
    rows: List[Dict[str, Any]] = data.get("rows", []) or []

    per_row_pass: List[bool] = []
    per_row_violations: List[List[str]] = []
    per_row_tier: List[Optional[str]] = []

    # 按档位收集 TTFB 采样，用于档位级合并统计
    tier_bucket: Dict[str, Dict[str, List[float]]] = {
        name: {"avg_ttfb": [], "p50_ttfb": [], "p90_ttfb": [], "row_indices": []}
        for name, _lo, _hi, _th in TIERS
    }

    for idx, row in enumerate(rows):
        stats = row.get("stats") or {}
        success_rate_f = float(row.get("success_rate_f", 0.0))

        avg_prompt = _safe_get(stats, "avg_prompt_tokens")
        tier = classify_tier(avg_prompt)
        per_row_tier.append(tier)

        violations: List[str] = []

        # 前置门槛：成功率必须 100%
        if success_rate_f + 1e-9 < 1.0:
            violations.append(f"success_rate={success_rate_f*100:.2f}% < 100%")
            per_row_pass.append(False)
            per_row_violations.append(violations)
            # 成功率不足时，不再聚合档位统计（否则会污染合并 P50/P90）
            continue

        avg_ttfb = _safe_get(stats, "ttfb", "avg")
        p50_ttfb = _safe_get(stats, "ttfb", "p50")
        p90_ttfb = _safe_get(stats, "ttfb", "p90")
        avg_rate = _safe_get(stats, "token_rates", "avg")
        tpm_val  = _safe_get(stats, "tpm")

        th = get_tier_thresholds(tier)

        if tier is None:
            violations.append(f"avg_prompt_tokens={avg_prompt} 未归入任何档位（仅记录，不 FAIL）")
        else:
            if avg_ttfb is not None and float(avg_ttfb) > th["p50"]:
                violations.append(
                    f"Avg TTFB={avg_ttfb:.2f}s > P50 阈值 {th['p50']}s（档位 {tier}）"
                )
            if p90_ttfb is not None and float(p90_ttfb) > th["p90"]:
                violations.append(
                    f"P90 TTFB={p90_ttfb:.2f}s > P90 阈值 {th['p90']}s（档位 {tier}）"
                )
            # 收集档位桶
            bucket = tier_bucket[tier]
            if avg_ttfb is not None:
                bucket["avg_ttfb"].append(float(avg_ttfb))
            if p50_ttfb is not None:
                bucket["p50_ttfb"].append(float(p50_ttfb))
            if p90_ttfb is not None:
                bucket["p90_ttfb"].append(float(p90_ttfb))
            bucket["row_indices"].append(idx)

        if avg_rate is not None and float(avg_rate) <= RATE_MIN_TOKENS_PER_SEC:
            violations.append(
                f"avg_token_rates={avg_rate:.2f} t/s ≤ {RATE_MIN_TOKENS_PER_SEC} t/s"
            )
        if tpm_val is not None and float(tpm_val) > user_tpm_quota:
            violations.append(
                f"TPM={tpm_val:.0f} > 用户配额 {user_tpm_quota}"
            )

        per_row_pass.append(len(violations) == 0)
        per_row_violations.append(violations)

    # 档位级合并统计（简化实现：取每轮 p50/p90 的平均作为"合并 P50/P90"近似值；
    # 如果后续 matrix_runner 提供原始 samples 列表，可在此替换为真正的合并分位数）
    tier_summary: List[Dict[str, Any]] = []
    for name, _lo, _hi, th in TIERS:
        bucket = tier_bucket[name]
        if not bucket["row_indices"]:
            continue
        p50_list = bucket["p50_ttfb"] or bucket["avg_ttfb"]
        p90_list = bucket["p90_ttfb"] or bucket["avg_ttfb"]
        p50_all = sum(p50_list) / len(p50_list) if p50_list else None
        p90_all = sum(p90_list) / len(p90_list) if p90_list else None
        p50_pass = (p50_all is None) or (p50_all <= th["p50"])
        p90_pass = (p90_all is None) or (p90_all <= th["p90"])
        tier_summary.append({
            "tier": name,
            "row_indices": bucket["row_indices"],
            "p50_ttfb_all": p50_all,
            "p90_ttfb_all": p90_all,
            "p50_threshold": th["p50"],
            "p90_threshold": th["p90"],
            "p50_pass": p50_pass,
            "p90_pass": p90_pass,
        })

    overall_pass = all(per_row_pass) and all(
        item["p50_pass"] and item["p90_pass"] for item in tier_summary
    )

    return {
        "user_tpm_quota": user_tpm_quota,
        "per_row_pass": per_row_pass,
        "per_row_violations": per_row_violations,
        "per_row_tier": per_row_tier,
        "tier_summary": tier_summary,
        "overall_pass": overall_pass,
    }


# ---------------------------------------------------------------------------
# evalscope 适配层（v3.x，对接 model_perf_evalscope/setup_and_run.sh run-all 产物）
# ---------------------------------------------------------------------------
def _build_aggregated_from_evalscope_group(group_dir: Path) -> Dict[str, Any]:
    """把 evalscope `_group_<RUN_GROUP>/` 下每档 `benchmark_summary.json` +
    `benchmark_percentile.json` 装配成与 matrix_aggregated.json 等价的内存结构，
    然后直接喂给现成的 `evaluate_sla()` 复用 6 档 TTFT/吞吐/TPM 评估器。

    evalscope 字段 → matrix_aggregated 映射：
      - Avg Input Tokens          → stats.avg_prompt_tokens
      - TTFT (ms)        / 1000   → stats.ttfb.avg          (秒)
      - p50 TTFT (ms)    / 1000   → stats.ttfb.p50          (秒)
      - p90 TTFT (ms)    / 1000   → stats.ttfb.p90          (秒)
      - Total Throughput (tok/s)  → stats.token_rates.avg   (t/s)
      - Total Throughput * 60     → stats.tpm
      - Success / Total Requests  → success_rate_f          (0~1)

    扫描规则：
      group_dir 下软链或子目录 `<provider>-<model>-<label>-YYYYMMDD-HHMM[_N]/`，
      其内部 `<model>/parallel_X_number_Y/{benchmark_summary,benchmark_percentile}.json`。
    """
    if not group_dir.is_dir():
        raise FileNotFoundError(f"evalscope group dir not found: {group_dir}")

    rows: List[Dict[str, Any]] = []
    # 直接 follow symlink，按运行时间排序保持稳定
    run_dirs = sorted(
        [p for p in group_dir.iterdir()
         if p.is_dir() or p.is_symlink()
         and not p.name.startswith("_")
         and p.name != "summary"],
        key=lambda p: p.name,
    )

    for run_dir in run_dirs:
        # 资源目录跳过
        if run_dir.name in {"summary", "group.log"} or run_dir.name.endswith(".log"):
            continue
        bench_summary = next(run_dir.rglob("benchmark_summary.json"), None)
        if bench_summary is None:
            continue
        try:
            summary = json.loads(bench_summary.read_text(encoding="utf-8"))
        except Exception:
            continue

        pct_path = bench_summary.parent / "benchmark_percentile.json"
        p50_ttft_s: Optional[float] = None
        p90_ttft_s: Optional[float] = None
        if pct_path.is_file():
            try:
                pct_list = json.loads(pct_path.read_text(encoding="utf-8")) or []
                for item in pct_list:
                    pct = str(item.get("Percentiles", "")).strip()
                    ttft_ms = item.get("TTFT (ms)")
                    if ttft_ms is None:
                        continue
                    if pct == "50%":
                        p50_ttft_s = float(ttft_ms) / 1000.0
                    elif pct == "90%":
                        p90_ttft_s = float(ttft_ms) / 1000.0
            except Exception:
                pass

        total_req = float(summary.get("Total Requests", 0) or 0)
        success_req = float(summary.get("Success Requests", 0) or 0)
        success_rate_f = (success_req / total_req) if total_req > 0 else 0.0

        avg_ttft_ms = summary.get("TTFT (ms)")
        avg_ttft_s = (float(avg_ttft_ms) / 1000.0) if avg_ttft_ms is not None else None

        # 优先取 Total Throughput（输入+输出合计吞吐），与旧 SLA "avg_token_rates ≥ 30" 语义对齐
        total_tps = summary.get("Total Throughput (tok/s)")
        if total_tps is None:
            total_tps = summary.get("Output Throughput (tok/s)")
        token_rate = float(total_tps) if total_tps is not None else None
        tpm_val = (token_rate * 60.0) if token_rate is not None else None

        rows.append({
            # 元数据：复用 run 目录名作为 dataset 标签（含 label / 时间戳）
            "dataset": run_dir.name,
            "concurrency": summary.get("Concurrency"),
            "total_requests": int(total_req) if total_req else 0,
            "success_requests": int(success_req) if success_req else 0,
            "failed_requests": int(summary.get("Failed Requests", 0) or 0),
            "success_rate_f": success_rate_f,
            "stats": {
                "avg_prompt_tokens": summary.get("Avg Input Tokens"),
                "avg_output_tokens": summary.get("Avg Output Tokens"),
                "ttfb": {
                    "avg": avg_ttft_s,
                    "p50": p50_ttft_s if p50_ttft_s is not None else avg_ttft_s,
                    "p90": p90_ttft_s if p90_ttft_s is not None else avg_ttft_s,
                },
                "token_rates": {"avg": token_rate},
                "tpm": tpm_val,
            },
            # 原始 evalscope 数据，便于上层报告引用
            "_evalscope_summary_path": str(bench_summary),
        })

    return {
        "schema": "matrix_aggregated/v1+evalscope_adapter",
        "source": "evalscope_group",
        "group_dir": str(group_dir),
        "rows": rows,
    }


def evaluate_sla_from_evalscope_group(
    group_dir: Path,
    user_tpm_quota: int = 5_000_000,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """一站式：扫描 evalscope group 目录 → 装配 aggregated → 跑 SLA。
    返回 (aggregated_dict, sla_result_dict)；调用方决定要不要落盘。
    """
    aggregated = _build_aggregated_from_evalscope_group(group_dir)

    # 借用 evaluate_sla()：把 dict 写到内存 path？为了零改动，直接把 dict 写临时文件再调用。
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tf:
        json.dump(aggregated, tf, ensure_ascii=False)
        tmp_path = Path(tf.name)
    try:
        sla = evaluate_sla(tmp_path, user_tpm_quota=user_tpm_quota)
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass
    return aggregated, sla


# ---------------------------------------------------------------------------
# 报告生成器适配层（Excel / HTML 共用；阈值与 evaluate_sla 完全同源）
# ---------------------------------------------------------------------------

# 违规项对应的报告字段名：Excel 的列名、HTML 表头都用这套 key，便于精确标红
FIELD_SUCCESS = "成功率"
FIELD_AVG_TTFT = "Avg TTFT(s)"
FIELD_P90_TTFT = "P90 TTFT(s)"
FIELD_AVG_RATE = "Avg Rate(t/s)"
FIELD_P50_TTFT = "P50 TTFT(s)"
FIELD_P95_TTFT = "P95 TTFT(s)"
FIELD_P50_RATE = "P50 Rate(t/s)"
FIELD_P5_RATE = "P5 Rate(t/s)"
FIELD_CACHE_HIT = "Avg Cache Hit Ratio"
SLA_FIELDS = (FIELD_SUCCESS, FIELD_AVG_TTFT, FIELD_P90_TTFT, FIELD_AVG_RATE)

# 上下文分桶准入：实测相对基准 ≥ 60%。
# TPS（越大越好）= 实测 / 基准；TTFT（越小越好）= 基准 / 实测。
# 分桶按压测档位标签，不按实测 prompt token：
#   1k–128k → ≤128K，200k → 128K–200K，512k/1m → 200K–1M。
# KV 命中不使用上表里的线上命中率。数据集公共前缀比例就是该档能达到的上限，
# 基准用设计命中率（_KV_DESIGN_PCT）；1m 仍是冷数据，不判定 KV。
ADMISSION_RATIO = 0.60
# (tps_p50, tps_p5, ttft_p50_s, ttft_p95_s)
_ADMISSION_TABLES: Dict[str, Dict[str, Tuple[float, float, float, float]]] = {
    "kimi-k3.1-pro": {
        "le_128k": (54.6, 28.2, 4.28, 9.85),
        "128_200k": (58.1, 30.4, 5.65, 12.02),
        "200k_1m": (60.3, 31.6, 6.52, 15.38),
    },
    # flashx 必须写在 flash 前面，避免被更短的名字截走
    "glm-5.3-flashx": {
        "le_128k": (160.0, 105.3, 2.08, 4.93),
        "128_200k": (166.9, 108.0, 2.52, 5.52),
        "200k_1m": (166.3, 107.2, 3.51, 7.63),
    },
    "glm-5.3-flash": {
        "le_128k": (61.8, 34.5, 2.61, 9.17),
        "128_200k": (58.5, 35.9, 3.28, 10.85),
        "200k_1m": (63.7, 36.4, 4.18, 15.77),
    },
}
# 数据集设计命中率（%）。与 build_hot_dataset.HIT_RATIO 一致。
_KV_DESIGN_PCT = {
    "1k": 33.3,
    "9k": 67.2,
    "16k": 67.2,
    "32k": 89.5,
    "64k": 89.5,
    "128k": 89.5,
    "200k": 96.0,
    "512k": 96.8,
}
_ADMISSION_BANDS = (
    ("le_128k", "≤128K"),
    ("128_200k", "128K–200K"),
    ("200k_1m", "200K–1M"),
)
_ADMISSION_KEYS = ("kimi-k3.1-pro", "glm-5.3-flashx", "glm-5.3-flash")


def _bucket_tokens(bucket: Optional[str]) -> int:
    import re
    m = re.fullmatch(r"(\d+)([km])", (bucket or "").strip().lower())
    if not m:
        return 1 << 60
    n = int(m.group(1))
    return n * (1024 if m.group(2) == "k" else 1024 * 1024)


def admission_model_key(model: Optional[str]) -> Optional[str]:
    """模型名命中准入表时返回表键；flashx 优先于 flash。"""
    low = (model or "").lower().replace("_", "-")
    if not low:
        return None
    for key in _ADMISSION_KEYS:
        if key in low:
            return key
    return None


def admission_band(bucket: Optional[str]) -> Optional[str]:
    n = _bucket_tokens(bucket)
    if n > 1024 * 1024:
        return None
    if n <= 128 * 1024:
        return "le_128k"
    if n <= 200 * 1024:
        return "128_200k"
    return "200k_1m"


def _band_label(band: Optional[str]) -> str:
    for key, label in _ADMISSION_BANDS:
        if key == band:
            return label
    return band or ""


def kv_design_pct(bucket: Optional[str]) -> Optional[float]:
    """该压测档位的 KV 设计命中率（百分数）。1m 和无前缀档位返回 None，不判定。"""
    return _KV_DESIGN_PCT.get((bucket or "").strip().lower())


def kv_design_rows() -> List[Dict[str, Any]]:
    """数据集各档 KV 设计命中率，供 SLA 判定标准 sheet。"""
    return [{"bucket": bucket, "kv_pct": pct} for bucket, pct in _KV_DESIGN_PCT.items()]


def admission_baseline(model: Optional[str], bucket: Optional[str]) -> Optional[Dict[str, Any]]:
    key = admission_model_key(model)
    band = admission_band(bucket)
    if key is None or band is None:
        return None
    tps_p50, tps_p5, ttft_p50, ttft_p95 = _ADMISSION_TABLES[key][band]
    design = kv_design_pct(bucket)
    return {
        "model_key": key,
        "band": band,
        "band_label": _band_label(band),
        "tps_p50": tps_p50,
        "tps_p5": tps_p5,
        "ttft_p50": ttft_p50,
        "ttft_p95": ttft_p95,
        "kv": None if design is None else design / 100.0,
        "kv_pct": design,
        "ratio_min": ADMISSION_RATIO,
    }


def threshold_rows() -> List[Dict[str, Any]]:
    """验收阈值表（供报告「SLA 判定标准」章节直接渲染）。"""
    return [
        {
            "tier": name,
            "lo": lo,
            "hi": hi,
            "p50": th["p50"],
            "p90": th["p90"],
            "rate_min": RATE_MIN_TOKENS_PER_SEC,
        }
        for name, lo, hi, th in TIERS
    ]


def _check_high(measured, baseline, field, label, unit, violations, failed_fields, band_label):
    """越大越好：实测 / 基准 ≥ 60%。"""
    if measured is None or baseline is None or baseline <= 0:
        violations.append(f"{label} 缺失（档位 {band_label}）")
        failed_fields.add(field)
        return
    ratio = measured / baseline
    if ratio + 1e-12 < ADMISSION_RATIO:
        violations.append(
            f"{label} {measured:.2f}{unit} / 基准 {baseline:.2f}{unit} = {ratio * 100:.2f}% < 60%（{band_label}）")
        failed_fields.add(field)


def _check_low(measured, baseline, field, label, violations, failed_fields, band_label):
    """越小越好：基准 / 实测 ≥ 60%。"""
    if measured is None or baseline is None:
        violations.append(f"{label} 缺失（档位 {band_label}）")
        failed_fields.add(field)
        return
    if measured <= 0:
        return
    ratio = baseline / measured
    if ratio + 1e-12 < ADMISSION_RATIO:
        violations.append(
            f"{label} 基准 {baseline:.2f}s / 实测 {measured:.2f}s = {ratio * 100:.2f}% < 60%（{band_label}）")
        failed_fields.add(field)


def evaluate_admission_row(
    model: Optional[str],
    bucket: Optional[str],
    p50_rate_tps: Optional[float],
    p5_rate_tps: Optional[float],
    p50_ttft_s: Optional[float],
    p95_ttft_s: Optional[float],
    cache_hit_ratio: Optional[float],
) -> Optional[Dict[str, Any]]:
    """命中准入模型时按分桶基准判定；未命中返回 None，调用方走原绝对阈值。"""
    if admission_model_key(model) is None:
        return None
    base = admission_baseline(model, bucket)
    violations: List[str] = []
    failed_fields: set = set()
    if base is None:
        violations.append(f"档位 {bucket or '-'} 超过 1M，无准入基准（不判定）")
        return {
            "mode": "admission",
            "tier": None,
            "pass": True,
            "violations": violations,
            "failed_fields": failed_fields,
            "thresholds": None,
        }
    label = base["band_label"]
    _check_high(p50_rate_tps, base["tps_p50"], FIELD_P50_RATE, "P50 Rate", " t/s",
                violations, failed_fields, label)
    _check_high(p5_rate_tps, base["tps_p5"], FIELD_P5_RATE, "P5 Rate", " t/s",
                violations, failed_fields, label)
    _check_low(p50_ttft_s, base["ttft_p50"], FIELD_P50_TTFT, "P50 TTFT",
               violations, failed_fields, label)
    _check_low(p95_ttft_s, base["ttft_p95"], FIELD_P95_TTFT, "P95 TTFT",
               violations, failed_fields, label)
    # 1m 等没有设计前缀的档位不判定 KV
    if base["kv"] is not None:
        kv_label = (bucket or "").strip().lower() or label
        _check_high(cache_hit_ratio, base["kv"], FIELD_CACHE_HIT, "KV 命中", "",
                    violations, failed_fields, kv_label)
        if FIELD_CACHE_HIT in failed_fields and cache_hit_ratio is not None:
            violations = [v for v in violations if not v.startswith("KV 命中")]
            ratio = cache_hit_ratio / base["kv"] if base["kv"] else 0
            violations.append(
                f"KV 命中 {cache_hit_ratio * 100:.2f}% / 基准 {base['kv_pct']:.2f}% = {ratio * 100:.2f}% < 60%（{kv_label}）")
    return {
        "mode": "admission",
        "tier": label,
        "pass": not failed_fields,
        "violations": violations,
        "failed_fields": failed_fields,
        "thresholds": base,
    }


def evaluate_report_row(
    avg_prompt_tokens: Optional[float],
    success_rate_f: Optional[float],
    avg_ttft_s: Optional[float],
    p90_ttft_s: Optional[float],
    avg_rate_tps: Optional[float],
    model: Optional[str] = None,
    bucket: Optional[str] = None,
    p50_ttft_s: Optional[float] = None,
    p95_ttft_s: Optional[float] = None,
    p50_rate_tps: Optional[float] = None,
    p5_rate_tps: Optional[float] = None,
    cache_hit_ratio: Optional[float] = None,
) -> Dict[str, Any]:
    """单行性能指标的验收判定，给 gen_report_from_db / gen_perf_dashboard 标红用。

    判定口径与 evaluate_sla() 一致：
      - 成功率必须 100%
      - Avg TTFT ≤ 档位 P50 阈值
      - P90 TTFT ≤ 档位 P90 阈值
      - Avg Rate(t/s) > RATE_MIN_TOKENS_PER_SEC（即 OTPS 下限 30 t/s）

    P5 Rate 只出现在报告列里，不参与达标判定。

    命中 kimi-k3.1-pro / glm-5.3-flashx / glm-5.3-flash 时改走分桶准入（相对基准 ≥ 60%）。

    返回：
      {"tier", "pass", "violations": [...], "failed_fields": set(...), "thresholds"}
    """
    admitted = evaluate_admission_row(
        model, bucket, p50_rate_tps, p5_rate_tps, p50_ttft_s, p95_ttft_s, cache_hit_ratio,
    )
    if admitted is not None:
        return admitted

    if not ABSOLUTE_SLA_ENABLED:
        return {
            "mode": "disabled",
            "tier": classify_tier(avg_prompt_tokens),
            "pass": True,
            "violations": [],
            "failed_fields": set(),
            "thresholds": None,
        }

    violations: List[str] = []
    failed_fields: set = set()

    def _num(v):
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    rate_f = _num(success_rate_f)
    avg_ttft = _num(avg_ttft_s)
    p90_ttft = _num(p90_ttft_s)
    avg_rate = _num(avg_rate_tps)
    tier = classify_tier(avg_prompt_tokens)

    # 前置门槛：成功率必须 100%
    if rate_f is not None and rate_f + 1e-9 < 1.0:
        violations.append(f"成功率 {rate_f * 100:.2f}% < 100%")
        failed_fields.add(FIELD_SUCCESS)

    th = get_tier_thresholds(tier)
    if tier is None:
        if avg_prompt_tokens is not None:
            violations.append(
                f"输入 {float(avg_prompt_tokens):.0f} tokens 未归入任何档位（仅记录，不判定）")
    else:
        if avg_ttft is not None and avg_ttft > th["p50"]:
            violations.append(
                f"Avg TTFT {avg_ttft:.2f}s > 阈值 {th['p50']}s（档位 {tier}）")
            failed_fields.add(FIELD_AVG_TTFT)
        if p90_ttft is not None and p90_ttft > th["p90"]:
            violations.append(
                f"P90 TTFT {p90_ttft:.2f}s > 阈值 {th['p90']}s（档位 {tier}）")
            failed_fields.add(FIELD_P90_TTFT)

    # OTPS 下限：越小越差。只卡 Avg Rate；P5 Rate 仅展示，不标红。
    if avg_rate is not None and avg_rate <= RATE_MIN_TOKENS_PER_SEC:
        violations.append(
            f"Avg Rate {avg_rate:.2f} t/s ≤ 下限 {RATE_MIN_TOKENS_PER_SEC:g} t/s")
        failed_fields.add(FIELD_AVG_RATE)

    # 只有「档位未归入」的提示不算不达标（与 evaluate_sla 一致）
    hard_fail = bool(failed_fields)
    return {
        "mode": "absolute",
        "tier": tier,
        "pass": not hard_fail,
        "violations": violations,
        "failed_fields": failed_fields,
        "thresholds": (dict(th) if tier is not None else None),
    }


def admission_threshold_rows(model: Optional[str]) -> List[Dict[str, Any]]:
    """准入模型的三档 TPS / TTFT 基准，供 SLA 判定标准 sheet。KV 另见 kv_design_rows。"""
    key = admission_model_key(model)
    if key is None:
        return []
    rows = []
    for band, label in _ADMISSION_BANDS:
        tps_p50, tps_p5, ttft_p50, ttft_p95 = _ADMISSION_TABLES[key][band]
        rows.append({
            "model": key,
            "band": label,
            "tps_p50": tps_p50,
            "tps_p5": tps_p5,
            "ttft_p50": ttft_p50,
            "ttft_p95": ttft_p95,
            "rate_floor": tps_p50 * ADMISSION_RATIO,
            "p5_floor": tps_p5 * ADMISSION_RATIO,
            "ttft_p50_ceil": ttft_p50 / ADMISSION_RATIO,
            "ttft_p95_ceil": ttft_p95 / ADMISSION_RATIO,
        })
    return rows


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Evaluate perf SLA. 兼容两种来源：\n"
            "  (1) v2.x 旧链路 matrix_aggregated.json（--aggregated-json）\n"
            "  (2) v3.x evalscope `_group_<RUN_GROUP>/` 目录（--evalscope-group-dir）\n"
            "二选一，互斥。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--aggregated-json", type=Path, default=None,
                     help="(v2.x) matrix_aggregated.json 路径")
    src.add_argument("--evalscope-group-dir", "--matrix-dir", type=Path, default=None,
                     dest="evalscope_group_dir",
                     help="(v3.x) evalscope `results/perf_evalscope/_group_<RUN_GROUP>/` 目录；"
                          "`--matrix-dir` 是兼容别名")
    ap.add_argument("--user-tpm-quota", type=int, default=5_000_000,
                    help="用户实际 TPM 配额（默认 5_000_000）")
    ap.add_argument("--out", "--output", type=Path, default=None, dest="out",
                    help="输出 sla_evaluation.json 路径；不传则写到来源目录")
    args = ap.parse_args()

    if args.evalscope_group_dir is not None:
        aggregated, result = evaluate_sla_from_evalscope_group(
            args.evalscope_group_dir, user_tpm_quota=args.user_tpm_quota,
        )
        # 也把装配出的 aggregated dump 出来便于排查
        out_path = args.out or (args.evalscope_group_dir / "sla_evaluation.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        agg_path = args.evalscope_group_dir / "matrix_aggregated_from_evalscope.json"
        agg_path.write_text(json.dumps(aggregated, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ SLA evaluation written: {out_path}")
        print(f"   aggregated (debug)   : {agg_path}")
    else:
        result = evaluate_sla(args.aggregated_json, user_tpm_quota=args.user_tpm_quota)
        out_path = args.out or (args.aggregated_json.parent / "sla_evaluation.json")
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ SLA evaluation written: {out_path}")

    print(f"   overall_pass = {result['overall_pass']}")
    for i, (ok, vios) in enumerate(zip(result["per_row_pass"], result["per_row_violations"])):
        if not ok:
            print(f"   ❌ row[{i}] violations: {vios}")


if __name__ == "__main__":
    _main()
