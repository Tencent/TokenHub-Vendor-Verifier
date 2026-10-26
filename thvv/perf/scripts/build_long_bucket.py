#!/usr/bin/env python3
"""生成超长档位压测数据集（512k / 1m）。

思路与现有档位保持一致：
  1) 素材只取已脱敏数据集（默认 datasets_clean/），块级再用与清洗一致的 finder 复查一遍；
  2) 以「句子块」为最小拼接单元（600~1500 字符），按 tokenizer 实际 token 数增量累加到目标；
     不能用字符数估算——现有语料 chars/token 在 1.04~2.0 之间波动，字符估算会偏 30%+；
  3) 末尾追加一个通用提问，结构与现有档位一致（system + user）；
  4) 整条生成后再复核一次（拼接边界可能拼出敏感词），命中则重生成。

用法：
  python3 scripts/build_long_bucket.py --buckets 512k 1m --count 200 100
  python3 scripts/build_long_bucket.py --buckets 512k --count 2 --out-dir /tmp/long_test
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
DEFAULT_POOL_DIR = os.path.join(PERF_DIR, "datasets_clean")
DEFAULT_OUT_DIR = os.path.join(PERF_DIR, "datasets_clean")
DEFAULT_WORDS = os.path.join(HERE, "sensitive_words.txt")

sys.path.insert(0, HERE)
from sanitize_dataset import SpanFinder, load_words  # noqa: E402

# 档位 -> 目标 token 数
TARGET_TOKENS = {"512k": 512_000, "1m": 1_000_000}
POOL_BUCKETS = ["1k", "9k", "16k", "32k", "64k", "128k", "200k"]

SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;])|\n")

QUESTIONS = [
    "请对以上内容做一份结构化摘要，列出核心要点、关键数据与结论。",
    "请从上述材料中抽取主要观点、涉及的关键主体与时间线，并按逻辑顺序整理。",
    "请阅读以上文档，总结其主要内容，并指出其中可能存在的矛盾或信息缺口。",
    "请基于以上材料回答：核心结论是什么？支撑该结论的关键论据有哪些？",
    "请将上述内容压缩为要点清单，保留关键结论与数字，省略重复表述。",
]


def to_blocks(text, min_len=600, max_len=1500):
    """把长文切成 600~1500 字符的块（按句边界，不破坏语义）。"""
    blocks, buf = [], ""
    for seg in SENT_SPLIT_RE.split(text):
        if not seg:
            continue
        if buf and len(buf) + len(seg) > max_len:
            if len(buf) >= min_len:
                blocks.append(buf)
                buf = seg
            else:
                buf += seg
        else:
            buf += seg
    if len(buf) >= min_len:
        blocks.append(buf)
    return blocks


def load_pool(pool_dir, buckets, max_chars, finder):
    """收集干净句块、system prompt 样本。"""
    blocks, systems = [], []
    total = 0
    for b in buckets:
        path = os.path.join(pool_dir, f"perf_zh_{b}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if total >= max_chars:
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for m in obj:
                    content = m.get("content") or ""
                    if m.get("role") == "system":
                        if 20 <= len(content) <= 300 and not finder.spans(content):
                            systems.append(content)
                        continue
                    if len(content) < 600:
                        continue
                    for blk in to_blocks(content):
                        if finder.spans(blk):
                            continue
                        blocks.append(blk)
                        total += len(blk)
                if total >= max_chars:
                    break
        if total >= max_chars:
            break
    return blocks, systems


def count_tokens(tk, text):
    return len(tk.encode(text, add_special_tokens=False))


def build_one(target, blocks, block_tokens, tk, rng, finder, systems, global_used, max_try=8):
    """拼一条达到 target token 的样本，返回 (messages, tokens)。

    global_used 记录已被任何一条样本用过的块，优先取未用块，压低跨样本重复
    （重复段落会让服务端按块做 KV cache 命中，虚抬长上下文吞吐）。
    """
    for _ in range(max_try):
        parts, total = [], 0
        while total < target:
            idx = rng.randrange(len(blocks))
            if idx in global_used:
                for _ in range(20):
                    cand = rng.randrange(len(blocks))
                    if cand not in global_used:
                        idx = cand
                        break
            global_used.add(idx)
            parts.append(blocks[idx])
            total += block_tokens[idx]
        doc = "".join(parts)
        system = systems[rng.randrange(len(systems))] if systems else (
            "你是一名专业的文本分析助手，擅长长文档摘要、信息抽取与要点整理。请用清晰的中文作答。"
        )
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": doc + "\n\n" + QUESTIONS[rng.randrange(len(QUESTIONS))]},
        ]
        joined = msgs[0]["content"] + msgs[1]["content"]
        if finder.spans(joined):
            continue  # 拼接边界拼出了敏感词，重来
        return msgs, total
    raise RuntimeError("多次生成仍无法得到干净样本，请扩大 --pool-chars")


def main():
    ap = argparse.ArgumentParser(description="生成 512k / 1m 超长档位数据集")
    ap.add_argument("--buckets", nargs="+", default=["512k", "1m"])
    ap.add_argument("--count", nargs="+", type=int, default=[200, 100], help="各档条数")
    ap.add_argument("--pool-dir", default=DEFAULT_POOL_DIR)
    ap.add_argument("--pool-buckets", nargs="+", default=POOL_BUCKETS)
    ap.add_argument("--pool-chars", type=int, default=300_000_000, help="素材池最大字符数")
    ap.add_argument(
        "--pool-file",
        default="",
        help="外部语料池 jsonl（每行含 text 字段，由 fetch_wiki_pool.py 生成）；指定后忽略 --pool-dir",
    )
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--words", default=DEFAULT_WORDS)
    ap.add_argument("--tokenizer", default="zai-org/GLM-4.6")
    ap.add_argument("--seed", type=int, default=20261026)
    args = ap.parse_args()

    if len(args.count) != len(args.buckets):
        sys.exit("--count 数量必须与 --buckets 一致")
    for b in args.buckets:
        if b not in TARGET_TOKENS:
            sys.exit(f"不支持的档位 {b}，可选：{list(TARGET_TOKENS)}")

    words = load_words(args.words)
    finder = SpanFinder(words)
    if args.pool_file:
        print(f"词表 {len(words)} 条；从外部池加载 {args.pool_file} ...", flush=True)
        blocks, systems, chars = [], [], 0
        with open(args.pool_file, encoding="utf-8") as fh:
            for line in fh:
                try:
                    blk = json.loads(line).get("text") or ""
                except json.JSONDecodeError:
                    continue
                if len(blk) < 600:
                    continue
                blocks.append(blk)
                chars += len(blk)
                if chars >= args.pool_chars:
                    break
    else:
        print(f"词表 {len(words)} 条；加载素材池（上限 {args.pool_chars // 1_000_000}M 字符）...", flush=True)
        blocks, systems = load_pool(args.pool_dir, args.pool_buckets, args.pool_chars, finder)
        chars = sum(map(len, blocks))
    print(f"素材池：{len(blocks)} 块 / {chars} 字符；system 样本 {len(systems)} 条", flush=True)
    if not blocks:
        sys.exit("素材池为空，检查 --pool-file / --pool-dir / --pool-buckets")

    from transformers import AutoTokenizer
    tk = AutoTokenizer.from_pretrained(args.tokenizer)
    print(f"tokenizer：{args.tokenizer}", flush=True)

    print("预计算块 token 数...", flush=True)
    block_tokens = [count_tokens(tk, b) for b in blocks]
    print(f"块 token 数：均值 {sum(block_tokens) / len(block_tokens):.0f}", flush=True)

    os.makedirs(args.out_dir, exist_ok=True)
    rng = random.Random(args.seed)
    global_used = set()
    for bucket, count in zip(args.buckets, args.count):
        target = TARGET_TOKENS[bucket]
        out_path = os.path.join(args.out_dir, f"perf_zh_{bucket}.jsonl")
        stats = Counter()
        with open(out_path, "w", encoding="utf-8") as out:
            for i in range(count):
                msgs, tokens = build_one(
                    target, blocks, block_tokens, tk, rng, finder, systems, global_used
                )
                out.write(json.dumps(msgs, ensure_ascii=False) + "\n")
                stats["min"] = tokens if not stats["min"] else min(stats["min"], tokens)
                stats["max"] = max(stats["max"], tokens)
                stats["sum"] += tokens
                if (i + 1) % 20 == 0 or i + 1 == count:
                    print(
                        f"  [{bucket}] {i + 1}/{count} 条，token "
                        f"min={stats['min']} max={stats['max']} avg={stats['sum'] // (i + 1)}",
                        flush=True,
                    )
        print(
            f"[{bucket}] 完成 {count} 条 -> {out_path}"
            f"（目标 {target}，实测 {stats['min']}~{stats['max']}，均值 {stats['sum'] // count}）",
            flush=True,
        )


if __name__ == "__main__":
    main()
