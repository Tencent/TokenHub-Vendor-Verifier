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
│   └── sla_eval.py               # （手动工具）SLA 验收评估：TTFT P50/P90 分档阈值 + 吞吐下限判定
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

> `datasets/` 是真实目录，已内置 10 个数据集（7 档主数据 + 3 个缓存场景）共 3.2GB，开箱即用。如需更换数据源，直接覆盖 `datasets/*.jsonl` 即可。

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
