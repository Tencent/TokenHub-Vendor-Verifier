# 大模型性能压测工具包

本工具包用于对大模型推理 API 进行标准的性能基准测试（吞吐、延迟、并发能力），基于
[EvalScope](https://github.com/modelscope/evalscope) 性能测试引擎。

**适用任意兼容 OpenAI Chat Completions 或 Anthropic Messages 协议的模型**。工具本身不绑定任何特定模型族或 API 端点——只需在项目级 `thvv/configs/.env` 中填写你的地址、密钥和模型名即可。

> 配置文件中的 GLM-5.2 和 GLM-4.6 tokenizer **仅为示例**，使用前请替换为你的实际模型和对应的 tokenizer。

---

## 目录结构

```
├── run.sh                        # 一键入口（check / bench / bench-all / report）
├── requirements.txt              # Python 依赖（evalscope[perf]>=0.13.0）
├── datasets/                     # 内置压测数据集（10 个）
│   ├── perf_zh_1k.jsonl          # ~1k  tokens 中文对话
│   ├── perf_zh_9k.jsonl          # ~9k  tokens
│   ├── perf_zh_16k.jsonl         # ~16k tokens
│   ├── perf_zh_32k.jsonl         # ~32k tokens
│   ├── perf_zh_64k.jsonl         # ~64k tokens
│   ├── perf_zh_128k.jsonl        # ~128k tokens
│   ├── perf_zh_200k.jsonl        # ~200k tokens
│   ├── perf_zh_9k_cold.jsonl     # 前缀缓存冷启动
│   ├── perf_zh_9k_hot.jsonl      # 前缀缓存热命中
│   └── perf_zh_9k_mix50.jsonl    # 50% 前缀缓存混合
├── references/                   # 性能测试报告模板.xlsx
├── scripts/
│   ├── setup_tokenizer.py        # 下载/校验 tokenizer（bench / bench-all / check 启动前自动调用）
│   ├── gen_perf_dashboard.py     # HTML 报告唯一出口：性能测试报告.html
│   ├── gen_report_from_db.py     # 38 列 Excel 指标报告：性能测试报告.xlsx
│   ├── export_failure_details.py # （手动工具）失败请求 CSV 导出
│   ├── sla_eval.py               # （手动工具）SLA 验收评估：TTFT P50/P90 分档阈值 + 吞吐下限判定
│   ├── sanitize_dataset.py       # 敏感内容扫描/脱敏/整段替换（report / mask / replace / drop）
│   ├── audit_dataset.py          # 数据集独立复核（L1 原样 / L2 去空白 / L3 去标点 + 质量检查）
│   ├── fetch_wiki_pool.py        # 从中文维基抽取干净文本块，构建超长档位语料池
│   └── build_long_bucket.py      # 生成 512k / 1m 超长档位数据集（按 tokenizer 实测 token 拼接）
└── results/                      # 压测产物（自动创建，已由 .gitignore 排除）
```

> 运行配置统一放在项目级 `thvv/configs/`（模板 `thvv/configs/env.example`，复制为 `.env` 使用），不在本目录。

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

```bash
cp ../configs/env.example ../configs/.env   # 即项目级 thvv/configs/
```

编辑 `configs/.env`，填写必填项：

```bash
# ===== OpenAI 协议示例 =====
PROTOCOL=openai
API_URL=https://你的服务地址/v1/chat/completions
API_KEY=你的密钥
MODEL_NAME=你的模型名
TOKENIZER=你的tokenizer仓库名

# ===== Anthropic 协议示例（Claude 系列）=====
# PROTOCOL=anthropic
# API_URL=https://api.anthropic.com/v1/messages
# API_KEY=sk-ant-xxx
# MODEL_NAME=claude-3-5-sonnet-20241022
# TOKENIZER=Xenova/gpt-4o
```

| 模型族 | TOKENIZER 示例 |
|---|---|
| GLM-4 / GLM-5 | `zai-org/GLM-4.6` |
| DeepSeek V3 / V4 | `deepseek-ai/DeepSeek-V3` |
| MiniMax M2 / M2.7 | `MiniMaxAI/MiniMax-M2` |
| Kimi K2.5 / K2.6 / K2.7 | `moonshotai/Kimi-K2-Thinking` |
| Claude 系列（anthropic 协议） | `Xenova/gpt-4o`（近似） |

> `TOKENIZER` 可留空：`MODEL_NAME` 命中常见模型族（GLM / DeepSeek / MiniMax / Kimi / Qwen / Llama / Claude / GPT 等）时自动推断，显式设置优先。
> 采样温度无需手动设置：默认按模型自适应（kimi/moonshot 系列 = 1.0，其余 = 0.0），可用环境变量 `TEMPERATURE` 显式覆盖（优先级最高）。

### 3. 环境检查

```bash
bash run.sh check
```

### 4. 小跑验证

```bash
bash run.sh bench 1k 20 1
```

参数含义：`bucket=1k, 请求数=20, 并发=1`

### 5. 正式压测

```bash
# 16k 输入，250 请求，32 并发
bash run.sh bench 16k 250 32

# 64k 输入，400 请求，64 并发
bash run.sh bench 64k 400 64

# 128k 输入，500 请求，128 并发
bash run.sh bench 128k 500 128
```

支持的所有档位：

```text
1k  9k  16k  32k  64k  128k  200k
```

> 启动后进程在后台运行，会打印 PID 和日志路径。
> 使用 `tail -f results/perf-*-*/run.log` 跟踪进度。

### 6. 报告

`bench` / `bench-all` 跑完会**自动生成**两份报告（HTML 阅读版 + xlsx 指标版），无需手动操作；路径见下方常见问题。

中途中断 / 异常失败后手动补报告：

```bash
# HTML 报告（参数透传 gen_perf_dashboard.py）
bash run.sh report --run-dir results/perf-<bucket>-<ts> --model "模型名"

# Excel 指标报告（--client 用于报告署名，也可用环境变量 CLIENT）
python3 scripts/gen_report_from_db.py --results-dir results --client "供应商名称" \
    --out results/性能测试报告.xlsx
```

---

## 输出指标说明

压测产出两份报告（同目录、同名不同扩展名）：

**`性能测试报告.html`**（阅读版，`gen_perf_dashboard.py` 生成，单文件离线可开）：

```
一、总体结论      总请求数 / 成功 / 失败 / 总成功率 + 处置建议
二、并发梯度对比  按档位分表，指标与 xlsx 性能指标 sheet 完全同口径：
                  总请求 / 成功 / 失败 / 成功率，TTFT（Avg/Min/Max/P50/P90/P95/P99）、
                  TTLT 与 Rate 与 ITL（Avg/P50/P90/P95/P99）、Tokens 三项、
                  Avg Total Time(ms)、Output TPM / Output TPS / Input TPM / TPM
三、失败分析      失败原因聚合统计（限流 / 超时 / 服务端错误 ...）+ 占比
四、失败请求明细  逐条罗列所有失败请求：HTTP 状态码 / 请求 Body / 响应 Body /
                  TTFT / TTLT（不提取 Request ID，与效果评测口径一致）
```

**`性能测试报告.xlsx`**（指标交付版，`gen_report_from_db.py` 生成）：

```
性能指标 sheet：客户端 / 数据集 / 并发数 / 总请求数 / 成功数 / 失败数 / 成功率
  TTFT：Avg / Min / Max / P50 / P90 / P95 / P99（首 token 时延，秒）
  TTLT：Avg / P50 / P90 / P95 / P99（总时延，秒）
  Rate：Avg / P50 / P90 / P95 / P99（生成速率，tokens/s）
  ITL：Avg / P50 / P90 / P95 / P99（token 间隔，秒）
  Token：Avg Prompt / Avg Completion / Total Tokens
  吞吐：Output TPM / Output TPS / Input TPM / TPM
```

> 失败请求的逐条明细只在 HTML 报告第四章「失败请求明细」中呈现，xlsx 不再包含该 sheet。

---

## 数据集说明

数据集为中文长文档对话，每行一组 OpenAI Messages 格式的 JSON 数组：

```json
[
  {"role": "system", "content": "你是一名财经分析助理..."},
  {"role": "user", "content": "<中文长文档 + 问题>"}
]
```

| 档位 | 文件大小 | 输入长度范围 |
|---|---|---|
| 1k   | 8 MB   | 870–1,228 tokens |
| 9k   | 69 MB  | 8,000–11,000 tokens |
| 16k  | 125 MB | 14,000–18,000 tokens |
| 32k  | 251 MB | 28,000–36,000 tokens |
| 64k  | 507 MB | 56,000–72,000 tokens |
| 128k | 1.0 GB | 112,000–144,000 tokens |
| 200k | 567 MB | 180,000–220,000 tokens |
| 512k | 325 MB | 512,010–513,546 tokens（200 条） |
| 1m   | 317 MB | 1,000,007–1,001,367 tokens（100 条） |

> `datasets/` 是真实目录，已内置各档主数据共 3.2GB，开箱即用。如需更换数据源，直接覆盖 `datasets/*.jsonl` 即可。

### 超长档位（512k / 1m）

两档不来自原有语料，改为从中文维基百科重新构建，避免长文本反复拼接同一批段落：
`generated from wikimedia/wikipedia 20231101.zh` → 按句切块、去重、丢弃敏感块 → 按 tokenizer
实测 token 数累加到目标长度（字符数估算会偏 30%+，现有语料 chars/token 在 1.04~2.0 之间波动）。

```bash
# 1) 抽取干净语料池（400M 字符，约 5 分钟，产物 .cache/ 不入库）
python3 scripts/fetch_wiki_pool.py --pool-chars 400000000
# 2) 生成两档（token 精度 ±0.1%，跨样本块重复率 0%）
python3 scripts/build_long_bucket.py --pool-file .cache/wiki_zh_pool.jsonl \
    --buckets 512k 1m --count 200 100
```

实测：池 318,820 块 / 400M 字符（丢弃敏感块 80,284）；512k 均值 512,505 token，1m 均值 1,000,565 token；
复核 L1/L2 均为 0 命中。语料来源与许可（CC BY-SA 4.0）见 `SOURCE.md`。

> 1M token 远超常见模型上下文（GLM-4.6 为 128k），用于测长上下文承载与截断行为，模型侧拒绝属预期。

### 敏感内容脱敏（重要）

原始语料来自真实中文网页/文档（法规、百科、产品手册、新闻等），夹带涉政/涉台、翻墙、赌博、色情等内容，
压测时会被供应商内容安全策略拦截（如 HTTP 400 `data_inspection_failed`），**把内容审核失败算成压测失败，
虚降成功率并污染结论**。实测命中率随输入长度陡增：

| 档位 | 1k | 9k | 16k | 32k | 64k | 128k | 200k |
|---|---|---|---|---|---|---|---|
| 命中行占比 | 6.8% | 22.9% | 45.0% | 66.1% | 87.4% | 98.0% | 99.6% |
| 替换处数 | 452 | 2,627 | 5,076 | 9,592 | 20,065 | 39,667 | 21,429 |

> 注意：语料是 PDF/网页抓取产物，存在大量「选 举」「中 华 民 国」这种**字间插空格**的排版，
> 直接字符串匹配会大面积漏检。清洗与复核都开词内空白容忍（`--no-space-tolerant` 可关）。

工具：`scripts/sanitize_dataset.py` + 词表 `scripts/sensitive_words.txt`（可自行增删词）。

当前 `datasets_clean/` 由 `replace` 模式生成：命中片段整段替换为**从干净语料池（1k/9k/16k 中未命中的句段）取出的等长自然文本**，
不是打码；行数、字符数、token 长度分布完全不变，读起来仍是正常中文/英文词。

```bash
# 仅统计风险面（不写文件）
python3 scripts/sanitize_dataset.py report --buckets 1k 9k

# 整段替换为干净语料（推荐，当前 datasets_clean/ 即此模式产出）
python3 scripts/sanitize_dataset.py replace --out-dir datasets_clean

# 等长打码（命中词 -> 中性填充文本）
python3 scripts/sanitize_dataset.py mask --out-dir datasets_masked

# 丢弃命中行（长档位命中率过高，一般不推荐）
python3 scripts/sanitize_dataset.py drop --buckets 1k
```

> 词表同时收录简体与繁体变体（语料里有台湾法规等繁体条目），新增词直接追加到 `scripts/sensitive_words.txt` 后重跑 `replace` 即可。

复核用独立脚本（不与清洗逻辑共用实现）：

```bash
python3 scripts/audit_dataset.py --in-dir datasets_clean        # 全量
python3 scripts/audit_dataset.py --in-dir datasets_clean --buckets 128k --limit 100
```

三级口径：L1 原样匹配 / L2 去空白后匹配（查「选 举」这类排版）/ L3 去标点后匹配（查「国、会」这类，跨词拼接误报偏高）。
另附语料质量检查：JSON 坏行、空内容、控制字符、超长同字重复（用于确认替换没有引入新问题）。

当前 `datasets_clean/` 复核结果（12788 行全量）：L1 = 0，L2 = 0，L3 = 435 行（3.4%，均为跨标点拼接，
如「全国、会议」被判成 `国会`，模型审核不会命中）；控制字符与超长重复行数与原始语料完全一致（284 / 993 @128k），
说明替换未引入新的质量问题。

`run.sh` 默认优先使用 `datasets_clean/`，缺失时回落 `datasets/`；也可显式指定：

```bash
DATASET_DIR=datasets bash run.sh bench 128k 500 128   # 用原始语料
DATASET_DIR=datasets_clean bash run.sh bench 128k 500 128
```

> 压测只关心 token 长度与吞吐，脱敏不改变语义也不改变长度分布，对压测指标无影响。

---

## 常见问题

**Q: 首次压测时 tokenizer 会自动下载吗？**

会。`run.sh bench` / `bench-all` / `check` 启动前会自动检测 tokenizer 本地缓存，未缓存时自动调用 `scripts/setup_tokenizer.py` 从 HuggingFace / ModelScope 下载，无需手动执行。海外环境如遇下载慢，可设置 HF 镜像：

```bash
export HF_ENDPOINT=https://huggingface.co
```

**Q: 提示 `configs/.env 不存在`？**

```bash
cp ../configs/env.example ../configs/.env
```

**Q: 压测成功后会自动出报告吗？中途中断呢？**

跑完（正常结束）会自动生成两份报告：
- **单档压测** (`bench`)：`results/perf-<bucket>-<ts>/性能测试报告.html` + `性能测试报告.xlsx`
- **全档位压测** (`bench-all`)：`results/_group_<ts>/性能测试报告.html` + `性能测试报告.xlsx`（跨档位汇总）

报告路径会在结束时打印。若中途 Ctrl+C 中断或异常失败，不会自动出报告，已跑完的 `benchmark_data.db` 不会丢失，手动运行即可补出报告：
```bash
bash run.sh report --run-dir results/perf-<bucket>-<ts> --model "模型名"
# 全档位组目录：
bash run.sh report --run-dir results/_group_<ts> --model "模型名"
```

**Q: 如何只保留部分数据集以减小包体积？**

删除 `datasets/` 下不需要的 `perf_zh_*.jsonl` 即可，脚本会自动跳过缺失的档位。

**Q: 失败请求详情包含哪些信息？**

报告逐条罗列所有失败请求（success=0），每条包含：请求 Body、HTTP 状态码、
响应 Body（db 的 error 列文本，即原始 HTTP 错误体，含错误码/错误消息）、TTFT、TTLT。

不提取 Request ID（与 eval 报告口径一致）：需要追踪单个请求时，以响应 Body
中的原始错误体为准；完整失败清单也可用 `scripts/export_failure_details.py`
导出为 CSV（request + response 全量字段）。
