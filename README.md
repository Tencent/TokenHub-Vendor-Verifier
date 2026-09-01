# THVV — TokenHub Vendor Verifier

对大模型供应商做**性能压测**与**效果评测**的一体化验证工具集。兼容任意 OpenAI / Anthropic 协议端点，跑完自动产出报告与结构化产物。

> 命名：THVV = TokenHub Vendor Verifier，用于供应商引入前的能力验证与产物归档。

---

## 能力总览

| 模块 | 说明 | 状态 |
|------|------|:---:|
| `perf` | 性能压测：1k~200k 输入长度档位 × 并发梯度全组合，含增速限流（429）应对、成功率早停、TPM 趋势图与**性能仪表盘** | ✅ |
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
    │   └── .env               # 实际凭据（不入库）
    ├── perf/                  # 性能压测（run.sh + scripts/ + datasets/）
    │   └── results/           # 产物：xlsx / TPM 图 / 性能仪表盘.html
    ├── eval/                  # 效果评测（run.sh + scripts/ + datasets/）
    │   ├── scripts/run_eval.py       # 评测引擎（预检查 + 限流重试 + 打包）
    │   ├── scripts/eval_report_v2.py # v2 报告生成器（六章模版）
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

### 性能压测：`性能仪表盘.html` + xlsx + TPM 趋势图

总体结论卡 + 并发梯度矩阵（13 项指标）+ 失败原因聚合（429 限流自动识别与处置建议）。

---

## 凭据安全

- API Key 只放在 `configs/.env` 或环境变量，**不要写进命令行与代码**
- `.env` 与 `results/`、`datasets/` 缓存均已由 `.gitignore` 排除，不会进版本库
- 报告生成时对配置中的凭证字段自动脱敏

## 版本说明

- evalscope 固定 `1.9.0`（2026-07-20 已验证组合，勿随意升级）
- `eval_report_v2.py` 移植自 maas-test-management（origin/master），仅保留本地生成路径；上游同步用 diff 对比同名文件
