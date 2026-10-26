#!/usr/bin/env python3
"""从 HuggingFace 中文维基抽取干净文本块，构建超长档位语料池。

来源：wikimedia/wikipedia（默认 20231101.zh，CC BY-SA 4.0）
产出：jsonl 池文件，每行 {"id","title","text"}，text 为 600~1500 字符的完整句块。
过滤：过短条目、重复块、命中敏感词表的块（词表与清洗/复核同一份，含空白容忍）。

用法：
  python3 scripts/fetch_wiki_pool.py --pool-chars 400000000
  python3 scripts/fetch_wiki_pool.py --pool-chars 2000000 --out /tmp/pool_test.jsonl   # 试跑
"""

import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PERF_DIR = os.path.dirname(HERE)
DEFAULT_WORDS = os.path.join(HERE, "sensitive_words.txt")
DEFAULT_OUT = os.path.join(PERF_DIR, ".cache", "wiki_zh_pool.jsonl")

sys.path.insert(0, HERE)
from sanitize_dataset import SpanFinder, load_words  # noqa: E402
from build_long_bucket import to_blocks  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="构建维基中文干净文本块池")
    ap.add_argument("--dataset", default="wikimedia/wikipedia")
    ap.add_argument("--config", default="20231101.zh")
    ap.add_argument("--split", default="train")
    ap.add_argument("--pool-chars", type=int, default=400_000_000)
    ap.add_argument("--min-article-chars", type=int, default=600)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--words", default=DEFAULT_WORDS)
    ap.add_argument("--progress-every", type=int, default=2000)
    args = ap.parse_args()

    words = load_words(args.words)
    finder = SpanFinder(words)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    from datasets import load_dataset

    ds = load_dataset(args.dataset, args.config, split=args.split, streaming=True)
    seen = set()
    stat = {"articles": 0, "blocks": 0, "chars": 0, "short": 0, "dup": 0, "sensitive": 0}

    try:
        with open(args.out, "w", encoding="utf-8") as out:
            for ex in ds:
                stat["articles"] += 1
                text = ex.get("text") or ""
                if len(text) < args.min_article_chars:
                    stat["short"] += 1
                    continue
                for blk in to_blocks(text):
                    h = hashlib.blake2b(blk.encode("utf-8"), digest_size=8).digest()
                    if h in seen:
                        stat["dup"] += 1
                        continue
                    seen.add(h)
                    if finder.spans(blk):
                        stat["sensitive"] += 1
                        continue
                    out.write(
                        json.dumps(
                            {"id": ex.get("id"), "title": ex.get("title"), "text": blk},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    stat["blocks"] += 1
                    stat["chars"] += len(blk)

                if stat["articles"] % args.progress_every == 0:
                    print(
                        f"  条目 {stat['articles']}，块 {stat['blocks']}，"
                        f"{stat['chars'] / 1e6:.1f}M 字符，丢弃(重复/敏感) {stat['dup']}/{stat['sensitive']}",
                        flush=True,
                    )
                if stat["chars"] >= args.pool_chars:
                    break
    except KeyboardInterrupt:
        print("中断，已写入部分池文件")

    print(
        f"完成：条目 {stat['articles']}，块 {stat['blocks']}，{stat['chars']} 字符，"
        f"过短条目 {stat['short']}，重复块 {stat['dup']}，敏感块 {stat['sensitive']}\n"
        f"输出：{args.out}"
    )
    sys.stdout.flush()
    # datasets streaming 的后台线程在解释器收尾时会触发 PyGILState_Release 崩溃，强制退出规避
    os._exit(0)


if __name__ == "__main__":
    main()
