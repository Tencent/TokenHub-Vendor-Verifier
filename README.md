# THVV — TokenHub Vendor Verifier

对大模型供应商做**性能压测**与**效果评测**的一体化验证工具集。性能压测支持 OpenAI / Anthropic 双协议端点，效果评测支持任意 OpenAI 兼容端点，跑完自动产出报告与结构化产物。

> 命名：THVV = TokenHub Vendor Verifier，用于供应商引入前的能力验证与产物归档。

---

## 能力总览

| 模块 | 说明 | 状态 |
|------|------|:---:|
| `perf` | 性能压测：1k~200k 输入长度档位 × 并发梯度全组合，含成功率早停、温度 / tokenizer 自适应，产出 **HTML（37 列全指标 + 失败请求明细）+ xlsx 双报告** | ✅ |
| `eval` | 效果评测：11 个主流数据集（AIME25/26、GPQA-Diamond、HLE、tau2-bench、MMLU-Pro、SimpleQA、LongBench v2、LiveCodeBench、SWE-Bench…），自动产出**效果评测报告** | ✅ |

---

## 目录结构

```
├── README.md / README_EN.md   # 本说明（中文 / 英文）
├── .gitignore                 # 产物、数据集缓存、密钥不入库（数据集走 Git LFS）
└── thvv/                      # 所有入口均在 thvv/ 下（先 cd thvv 再操作）
    ├── quickstart.sh          # 一键入口（check / install / perf / eval）
    ├── cli.py                 # 统一 CLI：python3 thvv/cli.py perf|eval|check|install ...
    ├── lib/params.sh          # 公共参数解析（--env / --url 等端点直传）
    ├── configs/
    │   ├── env.example        # 配置模板（复制为 .env 使用）
    │   ├── env.demo           # .env 演示样例（OpenAI + Anthropic 双协议）
    │   ├── env.d/             # 命名 env 配置（<name>.env，--env <name> 选择，不入库）
    │   ├── README.md          # 配置项说明
    │   └── .env               # 实际凭据（不入库）
    ├── perf/                  # 性能压测
    │   ├── run.sh             # 子命令：check / bench / bench-all / sla-tune / sla-tune-all / report
    │   ├── requirements.txt   # perf 依赖（evalscope[perf]>=0.13.0）
    │   ├── scripts/           # gen_perf_dashboard.py（HTML 唯一出口）/ gen_report_from_db.py（xlsx）/
    │   │                      # export_failure_details.py（失败 CSV）/ setup_tokenizer.py / sla_eval.py（SLA 判定）
    │   ├── datasets/          # 10 个数据集（1k~200k 共 7 主档 + 3 个前缀缓存场景）
    │   ├── references/        # 性能测试报告模板.xlsx
    │   ├── 性能验收标准.xlsx  # 性能验收标准（perf）
    │   └── results/           # 产物：性能测试报告.html + 性能测试报告.xlsx
    └── eval/                  # 效果评测
        ├── run.sh             # 子命令：check / bench / list
        ├── requirements.txt   # eval 依赖（evalscope 固定 1.9.0）
        ├── scripts/run_eval.py       # 评测引擎（预检查 + 限流重试 + 打包）
        ├── scripts/eval_report_v2.py # 报告生成器（含验收标准对比章节）
        ├── scripts/acceptance_data.json # 验收基线（由 效果验收标准.xlsx 同步维护）
        ├── 效果验收标准.xlsx  # 效果验收标准（eval）
        └── results/           # 产物：eval_report_v2.html / eval_summary.json / per_sample_details.csv
```

---

## 快速开始

### 1. 配置

```bash
cd thvv
cp configs/env.example configs/.env   # 填写 API_URL / API_KEY / MODEL_NAME / PROTOCOL / TOKENIZER
```

- `API_URL` 必须是**完整请求路径**（OpenAI: `.../v1/chat/completions`；Anthropic: `.../v1/messages`）
- `PROTOCOL` = `openai` | `anthropic`（perf 支持双协议；eval 走 OpenAI 兼容接口，一般无需设置）

多模型 / 多供应商：为每个模型建命名 env（`configs/env.d/<name>.env`），运行时 `--env <name>` 切换；也可在 perf/eval 命令上直接传端点参数（无需任何 env 文件）：

```bash
bash quickstart.sh perf bench 16k 500 10 --env glm53      # 用 configs/env.d/glm53.env
bash quickstart.sh eval bench aime26 --env kimi           # 用 configs/env.d/kimi.env
bash quickstart.sh perf bench 1k 20 1 \
    --url http://10.24.8.100:30809/v1/chat/completions \
    --api-key <API_KEY> --model glm-5.3-flash --provider zhipu   # 端点直传
```

- 配置优先级：CLI 参数（`--url` 等）> 环境变量 > `--env` 选中的命名 env 文件 > `configs/.env`
- perf 结果目录含模型名（`results/perf-<bucket>-<model>-<ts>`），多模型并行压测互不干扰
- 可直传参数：`--url / --api-key / --model / --protocol / --tokenizer / --provider / --judge-api-key`（均支持 `--opt=value` 形式）

> 除 quickstart.sh 外，也可用统一 CLI：`python3 thvv/cli.py perf bench 1k 20 1`（等价透传，启动时自动加载 `configs/.env`）

### 2. 环境检查 / 安装依赖

```bash
bash quickstart.sh check
bash quickstart.sh install
```

### 3. 性能压测

```bash
bash quickstart.sh perf bench 1k 200 20      # 单档：<bucket> [请求数] [并发]
bash quickstart.sh perf bench-all            # 全档位 × 并发梯度（可配 BUCKETS / CONCURRENCY_LADDER 等）
bash quickstart.sh perf sla-tune 32k         # SLA 并发上限探索（单档，二分搜索满足 SLA 的最大并发）
bash quickstart.sh perf sla-tune-all         # SLA 并发上限探索（全档位）
bash quickstart.sh perf report               # 从 results/ 重新生成报告
```

常用环境变量：`BUCKETS`、`CONCURRENCY_LADDER`、`BUCKET_COOLDOWN`、`N_<label>`（单档请求数）、`SUCCESS_RATE_MIN`（成功率早停）、`TEMPERATURE`（默认按模型名自适应：kimi/moonshot=1.0，其余=0.0）、`CLIENT`（xlsx 报告署名）、`NONCE=1`（随机前缀防 prefix cache 命中）。SLA 探索：`SLA_LOWER_BOUND`/`SLA_UPPER_BOUND`（并发搜索范围，默认 1~256）、`SLA_NUM_RUNS`（每点重复取平均）、`SLA_NUMBER_MULTIPLIER`（每点请求数=并发×N）、`SLA_PARAMS`（自定义 SLA 约束 JSON，覆盖按 bucket 的 TIERS 双阈值默认映射）。`MODEL_NAME` 命中常见模型族时 tokenizer 自动识别，`TOKENIZER` 显式设置优先。详见 `thvv/perf/README.md`。

### 4. 效果评测

```bash
bash quickstart.sh eval list                 # 列出 11 个数据集
bash quickstart.sh eval bench aime26         # 单数据集
bash quickstart.sh eval bench aime26 --limit 30 --repeats 1 --eval_batch_size 10
bash quickstart.sh eval bench all            # 全部
```

需 LLM Judge 的数据集（hle / simple_qa，`all` 同样强制）需提供 `--judge_api_key`；judge 模型与地址有默认值（`deepseek-v4-pro` @ `https://api.deepseek.com/v1`），可用 `--judge_model / --judge_base_url` 覆盖。限流自动等待 60s 并注入 `--use-cache` 续跑。详见 `thvv/eval/README.md`。

---

## 命令速查（完整口径）

### 效果评测（精度测试）

命令链路：`quickstart.sh eval bench` → `eval/run.sh bench` → `python3 scripts/run_eval.py`（EvalScope 引擎，预检查 + 限流重试 + 结果打包）。

```bash
# 环境检查 / 数据集列表
bash quickstart.sh check
bash quickstart.sh eval list                 # 11 个数据集

# 单 / 多数据集 / 全部
bash quickstart.sh eval bench aime26
bash quickstart.sh eval bench aime25,gpqa_diamond
bash quickstart.sh eval bench all

# 不含 swe_bench_pro 的 10 个数据集全量
# （swe_bench_pro 实例镜像数量多、体积大，酌情单独测试；hle/simple_qa 需先在 .env 配好 JUDGE_API_KEY）
bash quickstart.sh eval bench aime25,aime26,gpqa_diamond,hle,tau2_bench,mmlu_pro,simple_qa,longbench_v2,live_code_bench,swe_bench_verified_mini_agentic

# 冒烟 + 指定轮次与并发
bash quickstart.sh eval bench aime26 --limit 30 --repeats 1 --eval_batch_size 10
```

常用参数：

| 参数 | 说明 |
|------|------|
| `--limit N` | 冒烟：每数据集最多跑 N 条（LCB 用 5、SWE 用 2） |
| `--repeats K` | 重复轮次 / pass@k（默认按数据集：aime=16、gpqa=3、tau2=5） |
| `--eval_batch_size N` | 并发数（默认按数据集） |
| `--subset_list airline,retail,telecom` | tau2_bench 域筛选 |
| `--live_code_bench_subset release_latest` | LCB 子集，**必须指定**（否则遍历全部 29 个 subset） |
| `--thinking true/false` | 思考模式开关（默认开启） |
| `--judge_api_key` / `--judge_model` / `--judge_base_url` | Judge 模型（hle / simple_qa / all 必需） |

各数据集默认 repeats 与默认并发（未显式传参时生效，源自 `eval/scripts/run_eval.py` 注册表）：

| 数据集 | 默认 repeats | 默认并发（eval_batch_size） |
|--------|:---:|:---:|
| aime25 | 16（pass@16） | 30 |
| aime26 | 16 | 30 |
| gpqa_diamond | 3 | 20 |
| hle | 1 | 30 |
| tau2_bench | 5 | 30 |
| mmlu_pro | 1 | 30 |
| simple_qa | 1 | 30 |
| longbench_v2 | 1 | 10 |
| live_code_bench | 1 | 16 |
| swe_bench_verified_mini_agentic | 1 | 4 |
| swe_bench_pro（镜像过多，酌情测试） | 1 | 1 |

> 代码类数据集并发受 Docker 限制：swe_bench_verified_mini_agentic 每个并发独占一个容器（默认 4，激进可 8）；swe_bench_pro 默认 1；live_code_bench 单轮生成可开 16+（看端点吞吐）。

代码类数据集实测命令示例（glm-5.3-flash @ 内网端点）：

```bash
# LiveCodeBench（release_latest 共 1055 题，16 并发，实测约 4h15m）
bash quickstart.sh eval bench live_code_bench \
  --model_name glm-5.3-flash \
  --base_url http://10.24.8.100:30809/v1 \
  --api_key <API_KEY> \
  --eval_batch_size 16 \
  --live_code_bench_subset release_latest \
  --output_dir glm53flash-100-lcb-full

# SWE-bench Verified Mini Agentic（50 题，4 并发 Agent，实测约 3h）
bash quickstart.sh eval bench swe_bench_verified_mini_agentic \
  --model_name glm-5.3-flash \
  --base_url http://10.24.8.100:30809/v1 \
  --api_key <API_KEY> \
  --eval_batch_size 4 \
  --output_dir glm53flash-100-swe-full
```

要点：swe_bench 每个并发独占一个 Docker 容器（250 步上限），建议 4 并发；LCB 为本地进程执行模型生成代码（未启用沙箱）注意环境隔离；长跑建议 `nohup bash quickstart.sh eval bench ... > /tmp/xxx.log 2>&1 &`。详见 `thvv/eval/README.md`。

### 性能压测（性能测试）

命令链路：`quickstart.sh perf` → `perf/run.sh` → `evalscope perf`。

```bash
# 单档：<bucket> = 1k|9k|16k|32k|64k|128k|200k（默认 500 请求 / 10 并发）
bash quickstart.sh perf bench 16k 500 10

# 全档位 × 并发梯度（梯度默认 1 8 32 64 128，档间冷却 120s，成功率早停可配）
NONCE=1 BUCKETS="1k 9k 16k 32k 64k 128k 200k" \
CONCURRENCY_LADDER="1 8 32 64 128" SUCCESS_RATE_MIN=99.9 \
bash quickstart.sh perf bench-all

# SLA 并发上限探索（二分搜索满足 SLA 的最大并发，单档 / 全档位）
bash quickstart.sh perf sla-tune 16k
bash quickstart.sh perf sla-tune-all
#   可配：SLA_LOWER_BOUND=1 SLA_UPPER_BOUND=256 SLA_NUM_RUNS=2
#         SLA_NUMBER_MULTIPLIER=8（每点请求数 = 并发×N）SLA_PARAMS='<自定义 SLA JSON>'

# 补生成报告
bash quickstart.sh perf report --run-dir results/perf-1k-20260831-2143
```

底层命令展开（`perf/run.sh` 实际执行）：

```bash
evalscope perf \
    --model "$MODEL_NAME" --api "$PROTOCOL" \
    --tokenizer-path "$TOKENIZER" \
    --url "$API_URL" --api-key "$API_KEY" \
    --temperature "$TEMPERATURE" \
    --parallel "$p" --number "$n" \
    --dataset custom_multi_turn --dataset-path "datasets/perf_zh_${bucket}.jsonl" \
    --min-prompt-length "$minlen" --max-prompt-length "$maxlen" \
    --max-tokens 4096 --multi-turn --max-turns 1 \
    --connect-timeout 60 --read-timeout 600 --db-commit-interval 5 \
    --outputs-dir "$out" --no-timestamp --no-test-connection
# sla-tune 追加：--sla-auto-tune --sla-variable parallel \
#   --sla-lower-bound/--sla-upper-bound/--sla-num-runs/--sla-number-multiplier/--sla-params
```

档位（bucket → 输入 token 区间）与 SLA 口径（`sla_params_for_bucket`，单位 ms）：

| bucket | 输入区间 (token) | 对应验收档位 | avg_ttft | p90_ttft | avg_tpot |
|---|---|---|---|---|---|
| 1k | 870–1228 | <6K | ≤2000 | ≤5000 | ≤33（≈单请求 OTPS ≥30） |
| 9k | 8000–11000 | 6～16K | ≤4000 | ≤8000 | ≤33 |
| 16k | 14000–18000 | 16～32K | ≤4000 | ≤8000 | ≤33 |
| 32k | 28000–36000 | 32～64K | ≤8000 | ≤15000 | ≤33 |
| 64k | 56000–72000 | 64～128K | ≤15000 | ≤35000 | ≤33 |
| 128k | 112000–144000 | 128～256K | ≤30000 | ≤70000 | ≤33 |
| 200k | 180000–220000 | 128～256K | ≤30000 | ≤70000 | ≤33 |

> SLA 口径源自 `perf/scripts/sla_eval.py` TIERS 双阈值（avg_ttft 均值 + p90_ttft 分位双门槛），与 `性能验收标准.xlsx` 的 P90 标准一致；请求成功率 100% 为 auto-tune 内置硬门槛。重复压测必开 `NONCE=1`（注入随机前缀防 prefix cache 命中：不注入二次跑命中 97.75%，注入后 0.00%）。详见 `thvv/perf/README.md`。

---

## 报告产物

### 效果评测：`eval_report_v2.html`（每数据集一份，对外交付物）

报告结构如下，数据全部来自 evalscope 落盘产物（reviews / reports/*.json / task_config.yaml / 日志）：

1. **核心结论** — 正确率 / 题目数 / 异常跳过 KPI
2. **验收标准对比** — 实测得分 vs `效果验收标准.xlsx` 基线（模型名无视大小写匹配，±2~4 百分点浮动判定：≤2pp PASS / 2~4pp 边界 / >4pp 超标或 FAIL）
3. **效果与稳定性** — 多轮（repeats）得分对比
4. **用户体验与性能** — TTFT / TPOT / 吞吐分位数
5. **模型与评测配置** — 实际生效参数（凭证脱敏）
6. **逐题结果与评测证据** — 每题完整思维链 / 答案 / 评分
7. **异常跳过题目** — 被 `ignore_errors` 静默丢弃的题及原因分类

典型结果目录：

```
thvv/eval/results/<provider>-<model>-<timestamp>/
├── all_eval_summary.json        # 总汇总（仅 run 根一份）
├── eval_results.tar.gz          # 打包归档（含全部原始产物）
└── aime26/
    ├── eval_report_v2.html      # 效果评测报告（交付物）
    ├── eval_summary.json        # 分数摘要
    ├── per_sample_details.csv   # 逐题明细
    ├── configs/task_config.yaml # 实际生效配置
    └── logs/                    # 运行日志（跳过样本兜底来源）
```

> 原始 `predictions/`、`reviews/`、`reports/`（evalscope 原生产物，占体积 90%+）的信息已提炼进
> 逐题明细与报告，打包归档后自动从散目录清理；失败的数据集保留原始产物便于排障。

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

| 数据集 | kimi-k3 | HY3 | deepseek-v4-flash-0731 | hy4-preview | glm-5.3 | glm-5.3-flash | deepseek-v4.1-flash |
|------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| AIME2026 | 95 | 96.63 | 95.67 | 96 | - | 93.75 | 97.92 |
| HLE | 44 | 29.74 | 32.35 | 34.33 | 42.3 | 29.24 | 33.37 |
| MMLU_Pro | 89.52 | 87.36 | 87.25 | 85.96 | - | 87.58 | 87.86 |
| Simple_QA | 46.1 | 34.41 | 37.56 | 33.7 | - | 34.56 | 41.33 |
| GPQA-Diamond | 92.76 | 90.66 | 89.73 | 94.44 | 91.7 | 90.4 | 90.91 |
| LongBench V2 (Short) | 72.22 | 65.54 | 68.89 | 67.56 | - | 67.6 | 64.44 |
| τ²-Bench · 智慧零售 retail | 82.22 | 75.18 | 87.7 | 82.22 | - | 88.6 | 89.47 |
| τ²-Bench · 电力技术支持 telecom | 71.53 | 76.49 | 98.42 | 77.14 | - | 98.25 | 93.4 |
| τ²-Bench · 航空客服 airline | 66.52 | 63.45 | 68.05 | 74.7 | - | 86 | 90 |
| τ²-Bench · OVERALL | 75.02 | 73.61 | 88.7 | 77.27 | - | 92.09 | 91.11 |
| LIVE-CODE-BENCH | 93.18 | - | - | 86.92 | - | 87.46 | 93.18 |
| SWE-bench_Verified_Mini_Agentic | - | - | - | 85.42 | - | 81.63 | 90 |

> "-" 表示该模型未提供该数据集结果；详细数据见 `thvv/eval/效果验收标准.xlsx`。
> glm-5.3 列为官方榜单口径（HLE 42.3 = 无工具口径，GPQA-Diamond 91.7，DataLearner/z.ai 2026-09，其余数据集官方未发布）；
> glm-5.3-flash / deepseek-v4.1-flash 列为 THVV 内部实测口径。HLE 均为无工具口径（非 HLE w/ tools）。

---

## 凭据安全

- API Key 只放在 `configs/.env` 或环境变量，**不要写进命令行与代码**
- `.env` 与 `results/`、`datasets/` 缓存均已由 `.gitignore` 排除，不会进版本库
- 报告生成时对配置中的凭证字段自动脱敏

## 版本说明

- eval 侧 evalscope 固定 `1.9.0`（2026-07-20 已验证组合，勿随意升级）；perf 侧 `evalscope[perf]>=0.13.0`
