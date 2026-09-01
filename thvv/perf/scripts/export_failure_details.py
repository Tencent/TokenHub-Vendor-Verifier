#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 benchmark_data.db 导出所有失败请求的完整清单（request + response）为 CSV。

用法：
  python3 scripts/export_failure_details.py                      # 扫描 results/ 下所有 run
  python3 scripts/export_failure_details.py --filter perf-1k-20260715  # 只匹配含该串的 run
  python3 scripts/export_failure_details.py --out <路径>/failure_details.csv

输出：failure_details.csv
  columns: no, run, bucket, parallel, http_status,
           request, response, latency, ttft, error

数据来源：
  - request          完整请求体 JSON
  - response_messages base64(pickle(list))，解码后为模型响应/错误
  - error            错误列（HTTP 错误体 JSON，含错误码/消息）
  - success=0 的行即为失败请求

不提取 Request ID（与 eval 报告口径一致）：需要追踪单请求时
以 response / error 列中的原始错误体为准。
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import pickle
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _log(msg: str) -> None:
    print(f"[export_failure_details] {msg}", file=sys.stderr, flush=True)


def _decode_response_messages(blob: Any) -> list:
    """response_messages 是 base64(pickle(list))；解出 list，失败返回 []。"""
    if not blob:
        return []
    try:
        data = base64.b64decode(blob) if isinstance(blob, str) else blob
        msgs = pickle.loads(data)
        return msgs if isinstance(msgs, list) else []
    except Exception:
        return []


def _response_to_text(resp_msgs: list) -> str:
    """把解码后的响应列表转成可读文本。"""
    if not resp_msgs:
        return ""
    try:
        # 末条可能是错误 dict
        last = resp_msgs[-1]
        if isinstance(last, dict):
            if last.get("content"):
                c = last["content"]
                return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str)
            if last.get("error"):
                return json.dumps(last["error"], ensure_ascii=False, default=str)
            if last.get("message"):
                return json.dumps(last["message"], ensure_ascii=False, default=str)
        return json.dumps(resp_msgs, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps(resp_msgs, ensure_ascii=False, default=str)


def _http_status(row: dict, resp_msgs: list, err_text: str) -> str:
    """提取 HTTP 状态码。"""
    sc = row.get("status_code") or row.get("http_status")
    if sc:
        return str(sc)
    if resp_msgs and isinstance(resp_msgs[-1], dict):
        sc = resp_msgs[-1].get("status_code") or resp_msgs[-1].get("http_status")
        if sc:
            return str(sc)
    # 从错误文本提取 HTTP 状态码
    m = re.search(r"\b(\d{3})\b", err_text)
    if m:
        return m.group(1)
    return ""


def find_run_dirs(results_root: Path) -> list[tuple[str, Path, str, str, int]]:
    """扫描 results/ 下含 benchmark_data.db 的子目录，返回 (run_tag, db, vendor, bucket, parallel)。"""
    runs = []
    for db in results_root.rglob("benchmark_data.db"):
        try:
            parts = db.relative_to(results_root).parts
            run_tag = parts[0]
            parallel_dir = parts[-2]
            m = re.match(r"parallel_(\d+)_number_(\d+)", parallel_dir)
            parallel = int(m.group(1)) if m else 0
            tag_parts = run_tag.split("-")
            vendor = tag_parts[0]
            bucket = next((p for p in tag_parts if re.fullmatch(r"\d+k", p)), "?")
            runs.append((run_tag, db, vendor, bucket, parallel))
        except Exception:
            continue
    runs.sort(key=lambda r: r[0])
    return runs


def load_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA table_info(result)")
        cols = [r[1] for r in cur.fetchall()]
        cur.execute("SELECT * FROM result")
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as e:
        _log(f"读取 db 失败: {db_path} ({e})")
        rows = []
    finally:
        conn.close()
    return rows


def export(results_dir: Path, run_filter: str, out_path: Path) -> int:
    runs = find_run_dirs(results_dir)
    if run_filter:
        runs = [r for r in runs if run_filter in r[0]]
    if not runs:
        _log(f"{results_dir} 下未找到任何 benchmark_data.db")
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    total_fail = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "no", "run", "bucket", "parallel", "http_status",
            "request", "response", "latency", "ttft", "error",
        ])
        no = 0
        for run_tag, db, vendor, bucket, parallel in runs:
            rows = load_rows(db)
            for row in rows:
                if int(row.get("success") or 0) == 1:
                    continue
                no += 1
                total_fail += 1
                resp_msgs = _decode_response_messages(row.get("response_messages"))
                resp_text = _response_to_text(resp_msgs)
                err_text = str(row.get("error") or "") if row.get("error") else resp_text
                http_status = _http_status(row, resp_msgs, err_text)
                request = str(row.get("request") or "")
                response = resp_text or err_text
                writer.writerow([
                    no, run_tag, bucket, parallel, http_status,
                    request, response,
                    row.get("latency"), row.get("first_chunk_latency"), err_text,
                ])
            if total_fail:
                _log(f"[{run_tag}] 失败请求 {sum(1 for r in rows if int(r.get('success') or 0) != 1)} 条")

    _log(f"完成: 共 {total_fail} 条失败请求 → {out_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 perf 所有失败请求的完整清单 (request+response)")
    ap.add_argument("--results-dir", default="results", help="results 目录（默认 ./results）")
    ap.add_argument("--filter", default="", help="只匹配 run-tag 包含该字符串的实验")
    ap.add_argument("--out", default="", help="输出 CSV 路径，默认 results/failure_details_<ts>.csv")
    args = ap.parse_args()

    root = Path(args.results_dir)
    out_path = Path(args.out) if args.out else (
        root / f"failure_details_{__import__('datetime').datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    )
    return export(root, args.filter, out_path)


if __name__ == "__main__":
    raise SystemExit(main())
