# 压测数据集来源与许可

本目录 `datasets/` 与 `datasets_clean/` 下的 `perf_zh_*.jsonl` 按档位来源分为两类。

## 一、1k / 9k / 16k / 32k / 64k / 128k / 200k

真实中文网页与文档语料（法规条文、百科条目、产品手册、企业公开资料、学校通知等），
由项目早期从公开网页抓取汇总而成，逐条来源未做留存。

原始语料夹带涉政/涉台、翻墙、赌博、色情等内容，会触发供应商内容安全策略
（如 HTTP 400 `data_inspection_failed`），因此 `datasets_clean/` 版本对命中片段做了
等长替换（`scripts/sanitize_dataset.py`，详见 README「敏感内容脱敏」）。

**请勿将 `datasets/` 原始语料对外分发**；`datasets_clean/` 仅用于模型性能压测，
不用于训练。

## 二、512k / 1m（超长上下文档位）

- **来源**：HuggingFace 数据集 [`wikimedia/wikipedia`](https://huggingface.co/datasets/wikimedia/wikipedia)，
  config `20231101.zh`，split `train`，仅取 `text` 字段。
- **许可**：中文维基百科文本采用
  [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.zh) 许可。
  `perf_zh_512k.jsonl` / `perf_zh_1m.jsonl` 是该文本的衍生作品（切块、过滤、重排拼接），
  **同样以 CC BY-SA 4.0 提供**，署名：Wikipedia contributors, CC BY-SA 4.0。
- **处理流程**：
  1. `scripts/fetch_wiki_pool.py`：流式读取条目 → 按句切成 600~1500 字符块 →
     块级去重 → 丢弃命中敏感词表的块（词表与清洗同一份，含词内空白容忍）→ 写出语料池；
  2. `scripts/build_long_bucket.py`：按 tokenizer（`zai-org/GLM-4.6`）实测 token 数增量拼接，
     累积到目标（512,000 / 1,000,000 token），末尾追加通用提问，输出 `messages` 结构；
  3. `scripts/audit_dataset.py` 复核（L1 原样 / L2 去空白 / L3 去标点三级）。
- **复现**：

  ```bash
  python3 scripts/fetch_wiki_pool.py --pool-chars 400000000
  python3 scripts/build_long_bucket.py --pool-file .cache/wiki_zh_pool.jsonl \
      --buckets 512k 1m --count 200 100
  ```

- **说明**：拼接后不再保留条目边界，dump 的具体版本变更会影响可复现性；
  语料池缓存 `.cache/` 不入库，可随时按上述命令重建。

## 三、使用注意

- 两档超长数据集为压测用途，内容来自百科条目拼接，语义上不构成连贯文档，
  不适合作为效果评测数据。
- 数据集文件以 Git LFS 管理（见仓库根 `.gitattributes`），clone 后需
  `git lfs install && git lfs pull`。
