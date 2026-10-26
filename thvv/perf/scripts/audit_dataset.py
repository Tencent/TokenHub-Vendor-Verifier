#!/usr/bin/env python3
"""数据集独立复核（不依赖 sanitize 的替换逻辑）。

三级匹配，逐级放大召回：
  L1 原样匹配             —— 与清洗时口径一致
  L2 去掉全部空白后匹配   —— 捕获「中 華 民 國」「V P N」这类字间插空格的写法
  L3 去掉全部非文字符号后匹配 —— 捕获「中·华·民·国」这类插标点的写法（误报偏高，仅作风险提示）

同时做语料质量检查：JSON 解析失败、空内容、控制字符、超长同字重复（替换痕迹）、
字符长度分布（用于确认替换没有改变压测输入规模）。

用法：
  python3 scripts/audit_dataset.py --in-dir datasets_clean
  python3 scripts/audit_dataset.py --in-dir datasets_clean --buckets 128k --limit 200
"""

import argparse
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
PERF_DIR = os.path.dirname(HERE)
DEFAULT_DATASET_DIR = os.path.join(PERF_DIR, "datasets")
DEFAULT_WORDS = os.path.join(HERE, "sensitive_words.txt")
ALL_BUCKETS = ["1k", "9k", "16k", "32k", "64k", "128k", "200k"]

WS_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]+")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
REPEAT_RE = re.compile(r"(.)\1{49,}")
MASK_TRACE = "本条目内容已做合规脱敏处理"


def load_words(path):
    words = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            w = line.split(",", 1)[0].strip()
            if w:
                words.append(w)
    return sorted(set(words))


def build_re(words):
    # 长词优先，避免短词抢占
    return re.compile("|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)))


def iter_texts(obj):
    if isinstance(obj, list):
        for m in obj:
            if isinstance(m, dict) and isinstance(m.get("content"), str):
                yield m["content"]
    elif isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, str):
                yield v


def audit_file(path, pat, limit=0):
    st = {
        "lines": 0,
        "json_bad": 0,
        "empty": 0,
        "ctrl": 0,
        "repeat": 0,
        "mask_trace": 0,
        "chars_total": 0,
        "line_max": 0,
        "l1_lines": 0,
        "l1_hits": 0,
        "l2_lines": 0,
        "l2_hits": 0,
        "l3_lines": 0,
        "l3_hits": 0,
    }
    l2_words = Counter()
    l3_words = Counter()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if limit and st["lines"] >= limit:
                break
            st["lines"] += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                st["json_bad"] += 1
                continue
            texts = list(iter_texts(obj))
            joined = "".join(texts)
            n = len(joined)
            st["chars_total"] += n
            st["line_max"] = max(st["line_max"], n)
            if n == 0:
                st["empty"] += 1
            if CTRL_RE.search(joined):
                st["ctrl"] += 1
            if REPEAT_RE.search(joined):
                st["repeat"] += 1
            if MASK_TRACE in joined:
                st["mask_trace"] += 1

            c1 = len(pat.findall(joined))
            if c1:
                st["l1_lines"] += 1
                st["l1_hits"] += c1

            l2 = WS_RE.sub("", joined)
            m2 = pat.findall(l2)
            if m2:
                st["l2_lines"] += 1
                st["l2_hits"] += len(m2)
                l2_words.update(m2)

            l3 = NON_WORD_RE.sub("", joined)
            m3 = pat.findall(l3)
            if m3:
                st["l3_lines"] += 1
                st["l3_hits"] += len(m3)
                l3_words.update(m3)
    return st, l2_words, l3_words


def main():
    ap = argparse.ArgumentParser(description="数据集独立复核（三级归一化匹配 + 质量检查）")
    ap.add_argument("--in-dir", default=DEFAULT_DATASET_DIR)
    ap.add_argument("--buckets", nargs="+", default=ALL_BUCKETS)
    ap.add_argument("--words", default=DEFAULT_WORDS)
    ap.add_argument("--limit", type=int, default=0, help="每档只复核前 N 行（0=全量）")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    if not os.path.exists(args.words):
        sys.exit(f"词表不存在：{args.words}")
    words = load_words(args.words)
    pat = build_re(words)
    print(f"目录：{args.in_dir}")
    print(f"词表：{args.words}（{len(words)} 条）\n")

    print(f"{'档位':<8}{'行数':>7}{'L1命中行':>9}{'L2命中行':>9}{'L3命中行':>9}"
          f"{'均字符':>9}{'JSON坏':>7}{'空':>4}{'控制符':>7}{'超长重复':>9}")
    print("-" * 92)
    g2, g3 = Counter(), Counter()
    totals = Counter()
    for b in args.buckets:
        path = os.path.join(args.in_dir, f"perf_zh_{b}.jsonl")
        if not os.path.exists(path):
            print(f"{b:<8}{'（缺失）':>7}")
            continue
        st, w2, w3 = audit_file(path, pat, args.limit)
        g2.update(w2)
        g3.update(w3)
        totals.update(st)
        avg = st["chars_total"] // st["lines"] if st["lines"] else 0
        print(f"{b:<8}{st['lines']:>7}{st['l1_lines']:>9}{st['l2_lines']:>9}{st['l3_lines']:>9}"
              f"{avg:>9}{st['json_bad']:>7}{st['empty']:>4}{st['ctrl']:>7}{st['repeat']:>9}")

    print("-" * 92)
    avg = totals["chars_total"] // totals["lines"] if totals["lines"] else 0
    print(f"{'合计':<8}{totals['lines']:>7}{totals['l1_lines']:>9}{totals['l2_lines']:>9}"
          f"{totals['l3_lines']:>9}{avg:>9}{totals['json_bad']:>7}{totals['empty']:>4}"
          f"{totals['ctrl']:>7}{totals['repeat']:>9}")

    if g2:
        print("\nL2（去空白）命中词 Top：")
        for w, n in g2.most_common(args.top):
            print(f"  {w:<14}{n:>7}")
    if g3:
        print("\nL3（去符号）命中词 Top（含拼接误报，仅作风险提示）：")
        for w, n in g3.most_common(args.top):
            print(f"  {w:<14}{n:>7}")
    print(f"\n打码痕迹（{MASK_TRACE}）出现行数：{totals['mask_trace']}")


if __name__ == "__main__":
    main()
