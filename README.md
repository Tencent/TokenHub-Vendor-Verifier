# THVV — TokenHub Vendor Verifier

对大模型供应商做**性能压测**与**效果评测**的一体化验证工具集。兼容任意 OpenAI / Anthropic 协议端点，跑完自动产出报告与结构化产物。

> 命名：THVV = TokenHub Vendor Verifier，用于供应商引入前的能力验证与产物归档。

---

## 能力总览

| 模块 | 说明 | 状态 |
|------|------|:---:|
| `perf` | 性能压测：1k~200k 输入长度档位 × 并发梯度全组合，含增速限流（429）应对、成功率早停，产出 **HTML（37 列全指标 + 失败请求明细）+ xlsx 双报告** | ✅ |
| `eval` | 效果评测：11 个主流数据集（AIME25/26、GPQA-Diamond、HLE、tau2-bench、MMLU-Pro、SimpleQA、LongBench v2、LiveCodeBench、SWE-Bench…），自动产出 **v2 六章效果评测报告** | ✅ |
| `cache` | 上下文缓存基准 | 预留 |
| `e2e` | 端到端协议验收 | 预留 |

---

## 目录结构

```
├── quickstart.sh              # 一键入口（check / install / perf / eval）
├── .gitignore                 # 产物、数据集缓存不入库
└── thvv/
    ├── cli.py                 # 统一 CLI（thvv perf ... / thvv eval ...）
    ├── quickstart.sh          # 子命令分发
    ├── configs/
    │   ├── env.example        # 配置模板（复制为 .env 使用）
    │   ├── env.demo           # .env 演示样例（OpenAI + Anthropic 双协议）
    │   └── .env               # 实际凭据（不入库）
    ├── perf/                  # 性能压测（run.sh + scripts/ + datasets/）
    │   ├── 性能验收标准.xlsx  # 性能验收标准（perf）
    │   └── results/           # 产物：性能测试报告.html + 性能测试报告.xlsx
    ├── eval/                  # 效果评测（run.sh + scripts/ + datasets/）
    │   ├── scripts/run_eval.py       # 评测引擎（预检查 + 限流重试 + 打包）
    │   ├── scripts/eval_report_v2.py # v2 报告生成器（六章模版）
    │   ├── 效果验收标准.xlsx  # 效果验收标准（eval）
    │   └── results/           # 产物：eval_report_v2.html / jsonl / csv
    ├── cache/                 # 预留
    └── e2e/                   # 预留
```

---

## 快速开始

### 1. 配置

```bash
cd thvv
cp configs/env.example configs/.env   # 填写 API_URL / API_KEY / MODEL_NAME / PROTOCOL / TOKENIZER
```

- `API_URL` 必须是**完整请求路径**（OpenAI: `.../v1/chat/completions`；Anthropic: `.../v1/messages`）
- `PROTOCOL` = `openai` | `anthropic`（perf 与 eval 均支持双协议）
- 配置优先级：CLI 参数 > 环境变量 > `configs/.env`

### 2. 环境检查 / 安装依赖

```bash
bash quickstart.sh check
bash quickstart.sh install
```

### 3. 性能压测

```bash
bash quickstart.sh perf bench 1k 20 200      # 单档：<bucket> [请求数] [并发]
bash quickstart.sh perf bench-all            # 全档位 × 并发梯度（可配 BUCKETS / CONCURRENCY_LADDER 等）
bash quickstart.sh perf report               # 从 results/ 重新生成报告
```

常用环境变量：`BUCKETS`、`CONCURRENCY_LADDER`、`BUCKET_COOLDOWN`、`N_<label>`（单档请求数）、`SUCCESS_RATE_MIN`（成功率早停）、`RATE_LIMIT_RAMP` / `RAMP_RATE` / `WARMUP_NUM`（增速限流应对）。详见 `thvv/perf/README.md`。

### 4. 效果评测

```bash
bash quickstart.sh eval list                 # 列出 11 个数据集
bash quickstart.sh eval bench aime26         # 单数据集
bash quickstart.sh eval bench aime26 --limit 30 --repeats 1 --eval_batch_size 10
bash quickstart.sh eval bench all            # 全部
```

需 LLM Judge 的数据集（hle / simple_qa）追加 `--judge_model / --judge_base_url / --judge_api_key`。限流自动等待 60s 并注入 `--use-cache` 续跑。详见 `thvv/eval/README.md`。

---

## 报告产物

### 效果评测：`eval_report_v2.html`（每数据集一份，对外交付物）

六章模版结构，数据全部来自 evalscope 落盘产物（reviews / reports/*.json / task_config.yaml / 日志）：

1. **核心结论** — 正确率 / 题目数 / 异常跳过 KPI
2. **效果与稳定性** — 多轮（repeats）得分对比
3. **用户体验与性能** — TTFT / TPOT / 吞吐分位数
4. **模型与评测配置** — 实际生效参数（凭证脱敏）
5. **逐题结果与评测证据** — 每题完整思维链 / 答案 / 评分
6. **异常跳过题目** — 被 `ignore_errors` 静默丢弃的题及原因分类

典型结果目录：

```
thvv/eval/results/<provider>-<model>-<timestamp>/
├── all_eval_summary.json
├── eval_results.tar.gz
└── aime26/
    ├── eval_report_v2.html      # 六章报告（交付物）
    ├── eval_summary.json        # 分数摘要
    ├── per_sample_details.csv   # 逐题明细
    ├── configs/task_config.yaml # 实际生效配置
    ├── predictions/ reviews/    # 逐题原始输出与评分
    ├── logs/                    # 运行日志（跳过样本兜底来源）
    └── reports/*.json           # evalscope 原始得分报告
```

### 性能压测：`性能测试报告.html`（阅读版）+ `性能测试报告.xlsx`（指标版）

**HTML**（单文件离线可开，四章结构，指标与 xlsx 完全同口径）：

1. **总体结论** — 总请求 / 成功 / 失败 / 总成功率 + 处置建议
2. **并发梯度对比** — 37 列全指标矩阵：TTFT（Avg/Min/Max/P50/P90/P95/P99）、
   TTLT / Rate / ITL（Avg/P50/P90/P95/P99）、Tokens、Avg Total Time(ms)、
   Output TPM / Output TPS / Input TPM / TPM
3. **失败分析** — 失败原因聚合（429 限流自动识别）+ 占比 + 处置建议
4. **失败请求明细** — 逐条罗列所有失败请求（HTTP 状态码 / 请求 Body / 响应 Body / TTFT / TTLT）

**xlsx**：38 列「性能指标」sheet（口径与 HTML 一致）；失败明细只放 HTML，不在 xlsx 重复。

---

## 验收标准

### 性能验收标准

供应商性能验收判定标准见 [`性能验收标准.xlsx`](./thvv/perf/性能验收标准.xlsx)，要点如下：

**测试要求**

1. 低并发预热 5min，排除冷启动影响
2. 按并发梯度压测 10–15min，稳定后采集数据；TPM/RPM 未达承诺规格时，请求成功率须满足 SLA

**TTFT / 请求成功率**（按 InputTokens 增量，不含 cache）

| InputTokens | 腾讯侧标准 |
|------|------|
| <6K | P90<5s |
| 6～16K | P90<5s |
| 16～32K | P90<8s |
| 32～64K | P90<15s |
| 64～128K | P90<35s |
| 128～256K | P90<70s |

**OTPS**

| 模型激活参数 | 档位/Tier | 腾讯侧 OTPS 要求 |
|------|------|------|
| >10B 的模型 | L1 | ≥ 30 tokens/s |
| | L2 | ≥ 10 tokens/s |
| ≤10B 的模型 | / | ≥ 100 tokens/s |

> 以上均使用腾讯提供的 benchmark 验证，详细数据见 `thvv/perf/性能验收标准.xlsx`。

### 效果验收标准

效果验收基线见 [`效果验收标准.xlsx`](./thvv/eval/效果验收标准.xlsx)，各模型精度可在 **±2-4%** 上下浮动：

| 数据集 | kimi-k3 | HY3 | deepseek-v4-flash-0731 | hy4-preview |
|------|:---:|:---:|:---:|:---:|
| AIME2026 | 95 | 96.63 | 95.67 | 96 |
| HLE | 44 | 29.74 | 32.35 | 34.33 |
| MMLU_Pro | 89.52 | 87.36 | 87.25 | 85.96 |
| Simple_QA | 46.1 | 34.41 | 37.56 | 33.7 |
| GPQA-Diamond | 92.76 | 90.66 | 89.73 | 94.44 |
| LongBench V2 (Short) | 72.22 | 65.54 | 68.89 | 67.56 |
| τ²-Bench · 智慧零售 retail | 82.22 | 75.18 | 87.7 | 82.22 |
| τ²-Bench · 电力技术支持 telecom | 71.53 | 76.49 | 98.42 | 77.14 |
| τ²-Bench · 航空客服 airline | 66.52 | 63.45 | 68.05 | 74.7 |
| τ²-Bench · OVERALL | 75.02 | 73.61 | 88.7 | 77.27 |
| LIVE-CODE-BENCH | 93.18 | - | - | 86.92 |
| SWE-bench_Verified_Mini_Agentic | - | - | - | 85.42 |

> "-" 表示该模型未提供该数据集结果；详细数据见 `thvv/eval/效果验收标准.xlsx`。

---

## 凭据安全

- API Key 只放在 `configs/.env` 或环境变量，**不要写进命令行与代码**
- `.env` 与 `results/`、`datasets/` 缓存均已由 `.gitignore` 排除，不会进版本库
- 报告生成时对配置中的凭证字段自动脱敏

## 版本说明

- evalscope 固定 `1.9.0`（2026-07-20 已验证组合，勿随意升级）
