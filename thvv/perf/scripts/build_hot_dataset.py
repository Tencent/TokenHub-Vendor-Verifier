#!/usr/bin/env python3
"""生成「带前缀缓存命中」的压测数据集（长度固定，不引入 session）。

背景
----
原数据集每条内容互不相同，服务端 prefix cache 永不命中（KV Cache Hit Rate 恒为 0），
TTFT 测的是全量冷 prefill。线上实测命中率：
<8k 33.3%、8k-32k 67.2%、32k-128k 89.5%、128k-200k 96.0%、200k-512k 96.8%。

做法
----
  一条请求 = [system] + 公共前缀(h×L) + 唯一尾部((1-h)×L) + 问题

  - 总长恒等于 L，落在原档位区间内，run.sh 的 --min/--max-prompt-length 无需改动
  - 前 h×L 个 token 在所有请求间相同 → 服务端 prefix cache 稳定命中
  - 尾部每条不同 → 命中率不会虚高到 100%
  - 相比 session 方案，长度与命中率都是预设值，不受模型回复长度影响

语义连贯性（按业务分组）
------------------------
素材若跨业务拼接（前缀来自「邮件分类」、尾部来自「客户流失分析」），性能测量不受影响，
但内容不合常理，供应商做安全审核或后续复用做效果评测时会有问题。

因此小档位（≤32k，单业务素材够用）按 system 指纹分组：每档取 4 个最大的业务组，
每组独立构造一条前缀 + 尾部，system/前缀/尾部同属一个业务，读起来像同一业务的不同样本。

大档位（≥64k）单个业务没有那么长的连续文本（素材单条长度有限），无法避免跨业务拼接，
此时退化为全池拼接，但仍保持 4 个独立前缀。

块粒度按档位自适应
------------------
1k 档用 50~200 字符的小块（约 30~130 token），避免「一块就占满整档」导致长度偏差 ±20%；
9k 用 300~800 字符；16k 以上用 600~1500 字符。

用法
----
  python3 scripts/build_hot_dataset.py --buckets 1k --count 20 --out-dir /tmp/hot_test
  python3 scripts/build_hot_dataset.py --buckets 1k 9k --count 200 200 --pool-jsonl .cache/cls_clean.jsonl
"""

import argparse
import hashlib
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PERF_DIR = os.path.dirname(HERE)
DEFAULT_DATASET_DIR = os.path.join(PERF_DIR, "datasets_clean")
DEFAULT_OUT_DIR = os.path.join(PERF_DIR, "datasets_hot")
DEFAULT_WORDS = os.path.join(HERE, "sensitive_words.txt")

sys.path.insert(0, HERE)
from build_long_bucket import to_blocks  # noqa: E402
from sanitize_dataset import SpanFinder, load_words  # noqa: E402

# 档位 -> 目标输入 token（取 run.sh bucket_range 的中值）
BUCKET_TARGET = {
    "1k": 1024, "9k": 9216, "16k": 16384, "32k": 32768,
    "64k": 65536, "128k": 131072, "200k": 204800, "512k": 512000,
}

# 档位 -> 目标缓存命中率（按压测口径设定，2026-11-16）
HIT_RATIO = {
    "1k": 0.333, "9k": 0.672, "16k": 0.672,
    "32k": 0.895, "64k": 0.895, "128k": 0.895,
    "200k": 0.960, "512k": 0.968,
}

# 档位 -> token 区间，必须与 run.sh 的 bucket_range 完全一致（否则会被过滤掉）
BUCKET_RANGE = {
    "1k": (870, 1228), "9k": (8000, 11000), "16k": (14000, 18000),
    "32k": (28000, 36000), "64k": (56000, 72000), "128k": (112000, 144000),
    "200k": (180000, 220000), "512k": (500000, 540000),
}

# 素材切块粒度（token）。必须与厂商缓存块对齐：实测 DeepSeek 为 128 token / 块，
# 块级独立 hash，任意位置相同块都计入 cached_tokens。切得比它大，
# 块内会残留相同子片段，命中率会虚高（1k 档曾从 33% 虚高到 65%）。
CACHE_BLOCK_TOKENS = 128

# 档位 -> 切块粒度（字符），仅 --dataset-dir 模式（不按 token 切块时）使用
BUCKET_BLOCK = {
    "1k": (50, 200), "9k": (300, 800), "16k": (600, 1500), "32k": (600, 1500),
    "64k": (600, 1500), "128k": (600, 1500), "200k": (600, 1500), "512k": (600, 1500),
}

# 允许按业务分组的档位：只有 1k 能同时满足「单业务素材足够」和「尾部块全局唯一」。
# 9k 以上单个业务组的可用块不足以覆盖 200 条的尾部需求，块必然重复，
# 而 DeepSeek 等厂商的缓存按 128 token 块独立 hash（任意位置相同块都命中），
# 重复块会把实测命中率从设计值抬高几十个百分点。因此 9k+ 改为全池拼接，
# 用跨业务换取块唯一，保证命中率可控（语义混杂是可接受的代价）。
GROUPED_BUCKETS = {"1k"}

QUESTIONS = [
    "请对以上内容做一份结构化摘要，列出核心要点、关键数据与结论。",
    "请从上述材料中抽取主要观点、涉及的关键主体与时间线，并按逻辑顺序整理。",
    "请阅读以上文档，总结其主要内容，并指出其中可能存在的矛盾或信息缺口。",
    "请基于以上材料回答：核心结论是什么？支撑该结论的关键论据有哪些？",
    "请将上述内容压缩为要点清单，保留关键结论与数字，省略重复表述。",
]

DEFAULT_SYSTEM = "你是一名专业的文本分析助手，擅长长文档摘要、信息抽取与要点整理。请用清晰的中文作答。"


def count_tokens(tk, text):
    return len(tk.encode(text, add_special_tokens=False))


def to_token_blocks(text, tk, size=128):
    """把文本切成固定 size token 的块。

    必须和厂商的缓存块对齐（实测 DeepSeek 为 128 token 块、按块内容独立 hash）。
    如果块比它大，块内仍会包含与其他块相同的 128-token 子片段，
    服务端照样算命中 —— 这是「整块去重后命中率仍虚高」的原因。
    """
    ids = tk.encode(text, add_special_tokens=False)
    out = []
    for i in range(0, len(ids) - size + 1, size):
        out.append(tk.decode(ids[i:i + size], skip_special_tokens=True))
    return out


def _dedup_blocks(blocks, toks):
    """按整块内容去重（保持原顺序）。

    为什么需要：实测 DeepSeek 的上下文缓存按 128 token 块做**独立 hash**（非链式），
    任意位置出现相同块都会被算作 cached_tokens。素材里模板化文本很多
    （同一业务日志常以「当前日期时间…」等相同文字开头），若尾部复用块，
    实测命中率会从设计的 33% 虚高到 85%。整块去重后按顺序游标取用，
    即可让每条尾部的内容全局唯一。
    """
    seen, ub, ut = set(), [], []
    for b, t in zip(blocks, toks):
        h = hashlib.md5(b.encode("utf-8")).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        ub.append(b)
        ut.append(t)
    return ub, ut


def take_tail(src, tk, target_tokens, cursor):
    """从顺序游标处取连续的块拼出尾部，返回 (text, tokens, 消耗块数)。

    用游标顺序取（而非随机起点）是为了让 200 条请求的尾部内容互不重叠，
    服务端不会把它们计入缓存命中。
    """
    blocks, toks = src["pool"], src["pool_tok"]
    n = len(blocks)
    if n == 0:
        return "", 0, 0
    i = cursor % n
    parts, total, used = [], 0, 0
    for _ in range(n):
        if total >= target_tokens:
            break
        bt = toks[i]
        if total + bt > target_tokens * 1.05:
            blk = blocks[i]
            need = max(0, target_tokens - total)
            lo, hi = 0, len(blk)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if count_tokens(tk, blk[:mid]) <= need:
                    lo = mid
                else:
                    hi = mid - 1
            if lo > 20:
                seg = blk[:lo]
                parts.append(seg)
                total += count_tokens(tk, seg)
                used += 1
            break
        parts.append(blocks[i])
        total += bt
        i = (i + 1) % n
        used += 1
    return "".join(parts), total, used


def truncate_tokens(tk, text, max_tokens):
    """按 token 数二分截断文本（字符比例估算在中文上误差可达 30%）。"""
    if count_tokens(tk, text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens(tk, text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def take_text(blocks, block_tokens, tk, target_tokens, rng, start=None):
    """按 token 数拼接近 target 的文本，返回 (text, tokens)。"""
    n = len(blocks)
    if n == 0:
        return "", 0
    idx = rng.randrange(n) if start is None else start % n
    parts, total = [], 0
    for _ in range(n * 3):
        if total >= target_tokens:
            break
        bt = block_tokens[idx % n]
        if total + bt > target_tokens * 1.05:
            # 最后一块按 token 二分截断（字符比例估算在中文上误差可达 30%）
            blk = blocks[idx % n]
            need = max(0, target_tokens - total)
            lo_c, hi_c = 0, len(blk)
            while lo_c < hi_c:
                mid = (lo_c + hi_c + 1) // 2
                if count_tokens(tk, blk[:mid]) <= need:
                    lo_c = mid
                else:
                    hi_c = mid - 1
            if lo_c > 20:
                seg = blk[:lo_c]
                parts.append(seg)
                total += count_tokens(tk, seg)
            break
        parts.append(blocks[idx % n])
        total += bt
        idx += 1
    return "".join(parts), total


# ---------------- 素材加载 ----------------

def load_groups_from_jsonl(path, finder, max_chars=0):
    """从脱敏后的真实素材池按 system 指纹分组：{key: {"system":..., "texts":[...]}}。"""
    groups = {}
    total = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if max_chars and total >= max_chars:
                break
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            msgs = row.get("messages") or []
            sys_text = ""
            for m in msgs:
                if m.get("role") == "system" and isinstance(m.get("content"), str):
                    sys_text = m["content"]
                    break
            key = hashlib.md5(sys_text.encode()).hexdigest()[:12] if sys_text else "__nosys__"
            g = groups.setdefault(key, {"system": sys_text, "texts": []})
            for m in msgs:
                if m.get("role") != "user":
                    continue
                c = m.get("content")
                if isinstance(c, str) and len(c) >= 30:
                    g["texts"].append(c)
                    total += len(c)
    return groups


def load_groups_from_dataset(dataset_dir, bucket, finder):
    """从现有语料目录读一篇档位数据，作为单一素材组。"""
    path = os.path.join(dataset_dir, f"perf_zh_{bucket}.jsonl")
    if not os.path.exists(path):
        return {}
    texts, sys_text = [], ""
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msgs, list):
                continue
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                c = m.get("content")
                if not isinstance(c, str):
                    continue
                if m.get("role") == "system":
                    if not sys_text:
                        sys_text = c
                elif m.get("role") == "user":
                    texts.append(c)
    if not texts:
        return {}
    key = hashlib.md5(sys_text.encode()).hexdigest()[:12] if sys_text else "__nosys__"
    return {key: {"system": sys_text, "texts": texts}}


# ---------------- 构造 ----------------

def build_sources(groups, bucket, tk, finder, want_sources):
    """把素材组转成 1~N 个可用源（含切好的块）。

    小档位：取字符量最大的 want_sources 个业务组，每组独立成一个源（语义连贯）。
    大档位：合并全部素材成 1 个源（单业务长度不够，只能跨业务拼）。
    """
    lo, hi = BUCKET_BLOCK[bucket]
    use_groups = bucket in GROUPED_BUCKETS

    if not use_groups:
        groups = {"__merged__": {
            "system": next(iter(groups.values()))["system"] if groups else "",
            "texts": [t for g in groups.values() for t in g["texts"]],
        }}

    # 优先使用带 system 的业务组：__nosys__ 是「无 system 的多种业务」混杂，语义并不连贯
    if use_groups and len(groups) > 1:
        named = {k: v for k, v in groups.items() if k != "__nosys__" and v.get("system")}
        if named:
            groups = named

    ranked = sorted(groups.items(), key=lambda kv: -sum(len(t) for t in kv[1]["texts"]))
    if use_groups:
        ranked = ranked[:want_sources]
    else:
        ranked = ranked[:1]

    sources = []
    global_seen = set()   # 跨源全局去重：厂商按块内容独立 hash，块在哪个源出现都算命中
    for key, g in ranked:
        blocks, toks = [], []
        for t in g["texts"]:
            # 按厂商缓存块大小（128 token）切块，块内不再包含可与其他块重复的子片段
            for b in to_token_blocks(t, tk, CACHE_BLOCK_TOKENS):
                if finder.spans(b):
                    continue
                h = hashlib.md5(b.encode("utf-8")).hexdigest()
                if h in global_seen:
                    continue
                global_seen.add(h)
                blocks.append(b)
                toks.append(CACHE_BLOCK_TOKENS)
        # 整块去重：尾部按顺序游标取块，保证 200 条请求的尾部内容互不重叠
        blocks, toks = _dedup_blocks(blocks, toks)
        if len(blocks) < 4:
            continue

        # system 最多占目标长度的 25%，否则会把总长顶出档位区间
        sys_text = g["system"] or DEFAULT_SYSTEM
        max_sys = int(BUCKET_TARGET[bucket] * 0.25)
        if count_tokens(tk, sys_text) > max_sys:
            sys_text = truncate_tokens(tk, sys_text, max_sys)
        # 再向下对齐到缓存块整数倍：system 不是整块数会让后续块边界错位，
        # 实际可命中的块数比设计少半块（命中率偏低 5~10pp）
        cur_sys = count_tokens(tk, sys_text)
        aligned = (cur_sys // CACHE_BLOCK_TOKENS) * CACHE_BLOCK_TOKENS
        if aligned > 0 and aligned < cur_sys:
            sys_text = truncate_tokens(tk, sys_text, aligned)

        # 前缀池与尾部池分离：两者共用一池时，前缀随机取到的块会和尾部游标撞上，
        # 服务端把这些块算作缓存命中，实测命中率会高于设计值
        cut = max(CACHE_BLOCK_TOKENS, len(blocks) // 10)
        sources.append({
            "key": key,
            "system": sys_text,
            "has_system": bool(g["system"]),
            "prefix_pool": blocks[:cut],
            "prefix_tok": toks[:cut],
            "pool": blocks[cut:],
            "pool_tok": toks[cut:],
            "chars": sum(len(b) for b in blocks),
        })
    return sources


def build_bucket(tk, bucket, count, prefixes, sources, finder, rng):
    """按源构造数据集。prefixes 为总前缀数；每个源至少分到 1 个前缀。"""
    target = BUCKET_TARGET[bucket]
    hit = HIT_RATIO[bucket]
    lo, hi = BUCKET_RANGE[bucket]
    # 命中率 = cached_tokens / prompt_tokens，cached 部分包含 system，
    # 所以公共前缀 = h×L − system − 问题，否则总长会超出档位区间。
    # 向上对齐到缓存块整数倍：服务端只按整块命中，不对齐会少半块（1k 档 341 vs 384）
    base_prefix = ((int(target * hit) + CACHE_BLOCK_TOKENS - 1)
                   // CACHE_BLOCK_TOKENS) * CACHE_BLOCK_TOKENS

    if not sources:
        raise RuntimeError(f"[{bucket}] 无可用素材源")

    # 分配前缀：每个源至少 1 个前缀，多出来的按轮次补
    per_source = max(1, prefixes // len(sources))
    prefix_specs = []  # (source_index, prefix_text, prefix_tokens)
    for si, src in enumerate(sources):
        fixed = count_tokens(tk, src["system"]) if src["has_system"] else 0
        pre_tok = max(64, base_prefix - fixed)
        for k in range(per_source):
            start = (si * 7919 + k * 104729) % len(src["prefix_pool"])
            txt, _ = take_text(src["prefix_pool"], src["prefix_tok"], tk, pre_tok, rng, start)
            if finder.spans(txt):
                for t in range(1, 15):
                    alt, _ = take_text(src["prefix_pool"], src["prefix_tok"], tk, pre_tok,
                                       rng, start + t * 131)
                    if not finder.spans(alt):
                        txt = alt
                        break
            prefix_specs.append((si, txt, pre_tok))

    rows, stats = [], []
    skip_len = skip_sensitive = 0
    tail_cursor = {}   # 每个源独立的块游标：尾部顺序取块，保证 200 条互不重叠
    tail_wraps = 0     # 池用尽后回绕的次数，回绕意味着块开始重复
    for i in range(count):
        si, pre, pre_tok = prefix_specs[i % len(prefix_specs)]
        src = sources[si]
        q = QUESTIONS[i % len(QUESTIONS)]
        fixed = count_tokens(tk, src["system"]) if src["has_system"] else 0
        tail_target = max(40, target - pre_tok - fixed - count_tokens(tk, q))

        # 只消耗一段尾部：先多取 15%，再按 token 精确截断到目标长度。
        # （早期版本按区间中值反复重取，每次重试都推进游标，25 条就把块池耗尽回绕，
        #   导致尾部重复、服务端把整段尾部计入缓存命中，实测命中率从 33% 虚高到 66%）
        pos = tail_cursor.get(si, 0)
        tail, _, used = take_tail(src, tk, int(tail_target * 1.15), pos)
        new_pos = pos + used
        if new_pos > len(src["pool"]):
            tail_wraps += 1
        tail_cursor[si] = new_pos % max(1, len(src["pool"]))

        user = pre + "\n\n" + tail + "\n\n" + q
        total = count_tokens(tk, user) + fixed
        # 对齐到目标中值而非「只要落在区间内」：长度偏大会把命中率分母撑大，
        # 实测命中率会比设计值低一截（1k 档曾因此从 33% 掉到 19%）
        if total > int(target * 1.02):
            over = total - target
            tail = truncate_tokens(tk, tail, max(40, count_tokens(tk, tail) - over))
            user = pre + "\n\n" + tail + "\n\n" + q
            total = count_tokens(tk, user) + fixed
        elif total < int(target * 0.98):
            # 偏短时补一段（游标继续推进，仍是未使用过的块）
            extra, _, used2 = take_tail(src, tk, target - total + 80, tail_cursor[si])
            tail = tail + extra
            tail_cursor[si] = (tail_cursor[si] + used2) % max(1, len(src["pool"]))
            user = pre + "\n\n" + tail + "\n\n" + q
            total = count_tokens(tk, user) + fixed
            if total > int(target * 1.02):
                over = total - target
                tail = truncate_tokens(tk, tail, max(40, count_tokens(tk, tail) - over))
                user = pre + "\n\n" + tail + "\n\n" + q
                total = count_tokens(tk, user) + fixed
        if not (lo <= total <= hi):
            skip_len += 1
            continue
        if finder.spans(user):
            skip_sensitive += 1
            continue

        msgs = ([{"role": "system", "content": src["system"]}] if src["has_system"] else [])
        msgs.append({"role": "user", "content": user})
        rows.append(msgs)
        stats.append(total)

    if tail_wraps:
        print(f"      [warn] 素材块用尽后回绕 {tail_wraps} 次，尾部会有重复块，"
              f"实测命中率将高于设计值", flush=True)

    if skip_len or skip_sensitive:
        print(f"      [skip] 长度越界 {skip_len}，敏感词 {skip_sensitive}", flush=True)

    # 记录每个前缀对应的业务指纹，便于核验语义
    prefix_meta = [{"source": sources[si]["key"], "prefix_chars": len(txt),
                    "prefix_tokens": ptok}
                   for si, txt, ptok in prefix_specs]
    return rows, stats, prefix_meta


def default_count(bucket):
    return {"1k": 200, "9k": 200, "16k": 200, "32k": 200,
            "64k": 200, "128k": 200, "200k": 150, "512k": 100}[bucket]


def main():
    ap = argparse.ArgumentParser(description="生成带前缀缓存命中的固定长度压测数据集")
    ap.add_argument("--buckets", nargs="+", default=["1k"])
    ap.add_argument("--count", nargs="+", type=int, help="各档条数；缺省按档给默认值")
    ap.add_argument("--prefixes", type=int, default=4, help="每档公共前缀数（默认 4）")
    ap.add_argument("--dataset-dir", default=DEFAULT_DATASET_DIR,
                    help="素材来源一：现有语料目录")
    ap.add_argument("--pool-jsonl", default="",
                    help="素材来源二：脱敏语料池 JSONL（优先）")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    ap.add_argument("--words", default=DEFAULT_WORDS)
    ap.add_argument("--tokenizer", default="zai-org/GLM-4.6")
    ap.add_argument("--seed", type=int, default=20261116)
    args = ap.parse_args()

    bad = [b for b in args.buckets if b not in BUCKET_TARGET]
    if bad:
        sys.exit(f"不支持档位 {bad}，可选 {list(BUCKET_TARGET)}")
    if args.count and len(args.count) != len(args.buckets):
        sys.exit("--count 数量必须与 --buckets 一致")
    counts = dict(zip(args.buckets, args.count)) if args.count else \
        {b: default_count(b) for b in args.buckets}

    from transformers import AutoTokenizer
    print(f"[1/4] tokenizer {args.tokenizer} ...", flush=True)
    tk = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)

    finder = SpanFinder(load_words(args.words))
    os.makedirs(args.out_dir, exist_ok=True)

    shared_groups = None
    if args.pool_jsonl:
        print(f"[2/4] 加载真实素材池 {args.pool_jsonl} 并按 system 分组 ...", flush=True)
        shared_groups = load_groups_from_jsonl(args.pool_jsonl, finder)
        sizes = sorted((sum(len(t) for t in g["texts"]) for g in shared_groups.values()),
                       reverse=True)
        print(f"      业务组 {len(shared_groups)} 个，字符量 top5: "
              f"{[f'{s // 1000}k' for s in sizes[:5]]}", flush=True)
    else:
        print(f"[2/4] 素材来源 {args.dataset_dir}（现有语料）", flush=True)

    rng = random.Random(args.seed)
    meta = {
        "generator": "build_hot_dataset.py",
        "tokenizer": args.tokenizer,
        "seed": args.seed,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "design": "system + 公共前缀(h*L) + 唯一尾部((1-h)*L) + 问题；总长=L",
        "source": args.pool_jsonl or args.dataset_dir,
        "grouped_buckets": sorted(GROUPED_BUCKETS),
        "buckets": {},
    }

    for bucket in args.buckets:
        n = counts[bucket]
        grouped = bucket in GROUPED_BUCKETS
        print(f"[3/4] {bucket}: 目标 {BUCKET_TARGET[bucket]} token，命中率 "
              f"{HIT_RATIO[bucket]:.1%}，{n} 条 / {args.prefixes} 前缀"
              f"（{'按业务分组' if grouped else '全池拼接'}）...", flush=True)
        t0 = time.time()

        if shared_groups is not None:
            groups = shared_groups
        else:
            groups = load_groups_from_dataset(args.dataset_dir, bucket, finder)
        if not groups:
            print(f"      [skip] 无素材", flush=True)
            continue

        sources = build_sources(groups, bucket, tk, finder,
                                want_sources=args.prefixes)
        if not sources:
            print(f"      [skip] 素材不足（切块后少于 4 块）", flush=True)
            continue
        desc = ", ".join(f"{s['key']}({s['chars'] // 1000}k)" for s in sources)
        print(f"      源 {len(sources)} 个: {desc}", flush=True)

        rows, stats, prefix_meta = build_bucket(
            tk, bucket, n, args.prefixes, sources, finder, rng)

        path = os.path.join(args.out_dir, f"perf_zh_{bucket}.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        lo, hi = BUCKET_RANGE[bucket]
        ok = sum(1 for s in stats if lo <= s <= hi)
        meta["buckets"][bucket] = {
            "count": len(rows),
            "target_tokens": BUCKET_TARGET[bucket],
            "target_hit_ratio": HIT_RATIO[bucket],
            "prefix_tokens": int(BUCKET_TARGET[bucket] * HIT_RATIO[bucket]),
            "prefixes": prefix_meta,
            "sources": [{"key": s["key"], "chars": s["chars"],
                         "system_head": s["system"][:60]} for s in sources],
            "tokens": {
                "min": min(stats) if stats else 0,
                "max": max(stats) if stats else 0,
                "mean": round(sum(stats) / len(stats), 1) if stats else 0,
                "in_range": ok,
                "range": [lo, hi],
            },
            "elapsed_sec": round(time.time() - t0, 1),
        }
        print(f"      → {path}  {len(rows)} 条，长度 "
              f"{min(stats) if stats else 0}~{max(stats) if stats else 0}，落区间 "
              f"{ok}/{len(stats)}，{time.time() - t0:.0f}s", flush=True)

    meta_path = os.path.join(args.out_dir, "META.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[4/4] 完成：{args.out_dir}\n      元信息：{meta_path}", flush=True)


if __name__ == "__main__":
    main()
