#!/usr/bin/env python3
"""压测数据集敏感内容扫描与脱敏工具。

背景：datasets/perf_zh_*.jsonl 是真实中文网页/文档语料，夹带涉政/涉台/翻墙/赌博色情等
内容，压测时会被供应商内容安全策略拦截（如 HTTP 400 data_inspection_failed），
拉低成功率并污染压测结论。本工具用于量化风险面并在不改变语料长度分布的前提下脱敏。

三种模式：
  report  只统计不写文件：命中行数/占比、分档位与分类别命中次数、Top 命中词
  mask    把命中词等长替换为中性填充文本（保持 token 长度基本不变），输出到新文件
  drop    直接丢弃命中行，输出到新文件

用法：
  python3 scripts/sanitize_dataset.py report --limit 200
  python3 scripts/sanitize_dataset.py report --buckets 1k 9k
  python3 scripts/sanitize_dataset.py mask  --buckets 1k --out-dir datasets_clean
  python3 scripts/sanitize_dataset.py drop  --buckets 128k --out-dir datasets_clean

词表：scripts/sensitive_words.txt（每行 "<词>[,<类别>]"，可自行增删）
"""

import argparse
import json
import os
import random
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
PERF_DIR = os.path.dirname(HERE)
DEFAULT_DATASET_DIR = os.path.join(PERF_DIR, "datasets")
DEFAULT_WORDS = os.path.join(HERE, "sensitive_words.txt")

ALL_BUCKETS = ["1k", "9k", "16k", "32k", "64k", "128k", "200k"]
EXTRA_BUCKETS = ["9k_cold", "9k_hot", "9k_mix50"]

# 脱敏填充用的中性汉字（循环取用，避免长重复串被 tokenizer 过度合并）
FILLER = "本条目内容已做合规脱敏处理相关描述以供性能测试使用"

WS_RE = re.compile(r"\s+")


def load_words(path):
    words = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",", 1)
            word = parts[0].strip()
            if not word:
                continue
            cat = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "uncategorized"
            words.append((word, cat))
    # 长词优先，避免短词先替换导致长词匹配不到
    words.sort(key=lambda x: -len(x[0]))
    return words


def iter_texts(obj):
    """从一条样本（OpenAI messages 数组）里取出所有文本内容，返回 (path, text)。"""
    if isinstance(obj, list):
        for i, m in enumerate(obj):
            if isinstance(m, dict):
                c = m.get("content")
                if isinstance(c, str):
                    yield f"[{i}].content", c
                elif isinstance(c, list):  # 多模态 content parts
                    for j, p in enumerate(c):
                        if isinstance(p, dict) and isinstance(p.get("text"), str):
                            yield f"[{i}].content[{j}].text", p["text"]
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                yield k, v


def scan_line(texts, words):
    """返回 (命中词计数 Counter, 命中类别计数 Counter)。"""
    hits = Counter()
    cats = Counter()
    for _path, text in texts:
        for word, cat in words:
            n = text.count(word)
            if n:
                hits[word] += n
                cats[cat] += n
    return hits, cats


class SpanFinder:
    """敏感词定位器。

    语料中有大量「选 举」「中 华 民 国」这种字间插空格的排版（PDF/网页抓取产物），
    直接 count 会漏检，因此默认生成容忍词内空白的正则（spaced=True）。
    """

    def __init__(self, words, spaced=True):
        parts = []
        for w, _ in sorted(words, key=lambda x: -len(x[0])):
            if spaced:
                parts.append(r"\s*".join(re.escape(c) for c in w))
            else:
                parts.append(re.escape(w))
        self.pat = re.compile("|".join(parts)) if parts else None

    def spans(self, text):
        if self.pat is None or not text:
            return []
        return [(m.start(), m.end()) for m in self.pat.finditer(text)]


def apply_spans(text, spans, piece_repl):
    out, cur = [], 0
    for s, e in spans:
        out.append(text[cur:s])
        out.append(piece_repl(text[s:e]))
        cur = e
    out.append(text[cur:])
    return "".join(out)


def sanitize_obj(node, finder, piece_repl, passes=3):
    """递归处理样本中所有字符串字段，返回替换处数。

    替换后可能因拼接边界产生新命中，做多轮收敛。
    """
    n = 0
    seq = node.items() if isinstance(node, dict) else (
        enumerate(node) if isinstance(node, list) else [])
    for k, v in seq:
        if isinstance(v, str):
            spans = finder.spans(v)
            if not spans:
                continue
            new = apply_spans(v, spans, piece_repl)
            cnt = len(spans)
            for _ in range(passes - 1):
                sp = finder.spans(new)
                if not sp:
                    break
                new = apply_spans(new, sp, piece_repl)
                cnt += len(sp)
            node[k] = new
            n += cnt
        else:
            n += sanitize_obj(v, finder, piece_repl, passes)
    return n


def mask_piece(piece):
    return "".join(FILLER[i % len(FILLER)] for i in range(len(piece)))


def mask_obj(node, finder):
    """等长打码。"""
    return sanitize_obj(node, finder, mask_piece, passes=2)


# ---------------- replace 模式：用干净语料整体替换命中片段 ----------------

SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])|\n")
ASCII_CHUNK_RE = re.compile(r"[A-Za-z][A-Za-z0-9 ._\-]{2,15}")


def build_pool(paths, words, max_chars, finder, limit=0):
    """从干净（未命中）样本中收集替换素材：中文句段池 + ASCII 片段池。

    干净判定必须用与替换一致的 finder（含空白容忍），否则「国 会」这类句段
    会被当成干净素材吸进池里，替换后反而引入新命中。
    """
    cn_pool, ascii_pool = [], []
    cn_chars = 0
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for li, line in enumerate(fh):
                if limit and li >= limit:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for _p, text in iter_texts(obj):
                    for seg in SENT_SPLIT_RE.split(text):
                        if len(seg) < 6:
                            continue
                        if finder.spans(seg):
                            continue
                        cn_pool.append(seg)
                        cn_chars += len(seg)
                        for m in ASCII_CHUNK_RE.findall(seg):
                            m = m.strip()
                            if len(m) >= 3 and not m.isdigit():
                                ascii_pool.append(m)
                if cn_chars >= max_chars:
                    break
        if cn_chars >= max_chars:
            break
    return cn_pool, ascii_pool


class Replacer:
    """从语料池里取自然文本片段，取满 n 个字符。

    优先取池中长度正好为 n 的完整片段（ASCII 词形替换后仍是完整单词）；
    没有等长片段时退化为跨句流式截取（中文场景，仍是连续自然文本）。
    """

    def __init__(self, pool, seed=20261026):
        self.pool = pool or []
        self.rng = random.Random(seed)
        self.by_len = {}
        for s in self.pool:
            self.by_len.setdefault(len(s), []).append(s)
        self.cur = {}
        for length, items in self.by_len.items():
            self.cur[length] = self.rng.randrange(len(items))
        self.i = self.rng.randrange(len(self.pool)) if self.pool else 0
        self.off = 0

    def take(self, n):
        if not self.pool:
            return "".join(FILLER[i % len(FILLER)] for i in range(n))
        bucket = self.by_len.get(n)
        if bucket:
            idx = self.cur.get(n, 0)
            # 随机步长，避免同一条样本里连续取到同值片段
            self.cur[n] = idx + self.rng.randint(1, 7)
            return bucket[idx % len(bucket)]
        out, got = [], 0
        while got < n:
            seg = self.pool[self.i % len(self.pool)]
            self.i += 1
            avail = len(seg) - self.off
            if avail <= 0:
                self.off = 0
                continue
            if avail >= n - got:
                out.append(seg[self.off:self.off + (n - got)])
                self.off += n - got
                got = n
            else:
                out.append(seg[self.off:])
                got += avail
                self.off = 0
        return "".join(out)


def replace_obj(node, words, rc, ra, finder):
    """把命中片段整体替换为等长的干净语料，返回替换处数。"""

    def piece_repl(piece):
        replacer = ra if piece.isascii() else rc
        return replacer.take(len(piece))

    return sanitize_obj(node, finder, piece_repl, passes=3)


def dataset_path(bucket, in_dir):
    return os.path.join(in_dir, f"perf_zh_{bucket}.jsonl")


def run_report(args, words, finder, cat_of):
    print(f"{'档位':<10}{'扫描行':>8}{'命中行':>8}{'命中率':>9}{'命中次数':>10}")
    print("-" * 48)
    total_lines = total_hit = 0
    global_hits = Counter()
    global_cats = Counter()
    for bucket in args.buckets:
        path = dataset_path(bucket, args.in_dir)
        if not os.path.exists(path):
            print(f"{bucket:<10}{'（缺失）':>8}")
            continue
        lines = hit_lines = 0
        hits = Counter()
        cats = Counter()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if args.limit and lines >= args.limit:
                    break
                lines += 1
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                texts = list(iter_texts(obj))
                n_line = 0
                for _p, text in texts:
                    for m in finder.pat.findall(text):
                        w = WS_RE.sub("", m)
                        hits[w] += 1
                        cats[cat_of.get(w, "uncategorized")] += 1
                        n_line += 1
                if n_line:
                    hit_lines += 1
        total_lines += lines
        total_hit += hit_lines
        global_hits.update(hits)
        global_cats.update(cats)
        rate = f"{hit_lines / lines * 100:.1f}%" if lines else "-"
        print(f"{bucket:<10}{lines:>8}{hit_lines:>8}{rate:>9}{sum(hits.values()):>10}")
    print("-" * 48)
    rate = f"{total_hit / total_lines * 100:.1f}%" if total_lines else "-"
    print(f"{'合计':<10}{total_lines:>8}{total_hit:>8}{rate:>9}{sum(global_hits.values()):>10}")
    print("\n按类别命中次数：")
    for cat, n in global_cats.most_common():
        print(f"  {cat:<16}{n:>8}")
    print("\nTop 命中词：")
    for w, n in global_hits.most_common(args.top):
        print(f"  {w:<16}{n:>8}")


def run_write(args, words, mode, finder, replacers=None):
    os.makedirs(args.out_dir, exist_ok=True)
    for bucket in args.buckets:
        path = dataset_path(bucket, args.in_dir)
        if not os.path.exists(path):
            print(f"[skip] {bucket} 数据集不存在")
            continue
        out_path = os.path.join(args.out_dir, f"perf_zh_{bucket}.jsonl")
        lines = hit_lines = masked = dropped = 0
        with open(path, encoding="utf-8") as fh, open(out_path, "w", encoding="utf-8") as out:
            for line in fh:
                if args.limit and lines >= args.limit:
                    break
                lines += 1
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if mode == "drop":
                    n = sum(len(finder.spans(t)) for t in iter_texts(obj))
                elif mode == "mask":
                    n = mask_obj(obj, finder)
                else:  # replace：整体换成干净语料片段
                    n = replace_obj(obj, words, *replacers, finder)
                if n:
                    hit_lines += 1
                    if mode == "drop":
                        dropped += 1
                        continue
                    masked += n
                out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        kept = lines - dropped
        print(
            f"[{bucket}] 扫描 {lines} 行，命中 {hit_lines} 行，"
            f"{'丢弃 ' + str(dropped) if mode == 'drop' else '脱敏 ' + str(masked)} 处，"
            f"输出 {kept} 行 -> {out_path}"
        )


def main():
    ap = argparse.ArgumentParser(description="压测数据集敏感内容扫描与脱敏")
    ap.add_argument(
        "mode",
        choices=["report", "mask", "replace", "drop"],
        help="report=只统计；mask=等长打码；replace=整段替换为干净语料；drop=丢弃命中行",
    )
    ap.add_argument(
        "--pool-buckets",
        nargs="+",
        default=["1k", "9k", "16k"],
        help="replace 模式的干净语料来源档位（默认 1k 9k 16k）",
    )
    ap.add_argument("--pool-chars", type=int, default=20_000_000, help="语料池最大字符数")
    ap.add_argument("--buckets", nargs="+", default=ALL_BUCKETS, help="要处理的档位，默认全部主档位")
    ap.add_argument("--in-dir", default=DEFAULT_DATASET_DIR)
    ap.add_argument("--out-dir", default=os.path.join(PERF_DIR, "datasets_clean"))
    ap.add_argument("--words", default=DEFAULT_WORDS)
    ap.add_argument("--limit", type=int, default=0, help="每档只处理前 N 行（0=全量）")
    ap.add_argument("--top", type=int, default=30, help="report 模式打印的 Top 词数量")
    ap.add_argument(
        "--no-space-tolerant",
        action="store_true",
        help="关闭词内空白容忍（默认开启，用于匹配「选 举」这类字间空格写法）",
    )
    args = ap.parse_args()

    if args.buckets == ["all"]:
        args.buckets = ALL_BUCKETS + EXTRA_BUCKETS
    if not os.path.exists(args.words):
        sys.exit(f"词表不存在：{args.words}")
    words = load_words(args.words)
    finder = SpanFinder(words, spaced=not args.no_space_tolerant)
    cat_of = {}
    for w, c in words:
        cat_of.setdefault(w, c)
    print(f"词表：{args.words}（{len(words)} 条，空白容忍={'开' if not args.no_space_tolerant else '关'}）\n")

    if args.mode == "report":
        run_report(args, words, finder, cat_of)
        return

    replacers = None
    if args.mode == "replace":
        pool_paths = [dataset_path(b, args.in_dir) for b in args.pool_buckets]
        cn_pool, ascii_pool = build_pool(pool_paths, words, args.pool_chars, finder)
        print(
            f"语料池：中文句段 {len(cn_pool)} 条 / {sum(map(len, cn_pool))} 字符，"
            f"ASCII 片段 {len(ascii_pool)} 条"
        )
        replacers = (Replacer(cn_pool), Replacer(ascii_pool, seed=20261027))

    run_write(args, words, args.mode, finder, replacers)


if __name__ == "__main__":
    main()
