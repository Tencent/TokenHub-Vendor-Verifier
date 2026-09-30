# 大模型效果评测工具包

本工具包用于对大模型进行**精度/效果评测**，基于 [EvalScope](https://github.com/modelscope/evalscope) 评测框架，支持 11 个主流数据集。

**适用任意兼容 OpenAI 协议的模型 API**——只需在 `configs/.env` 中填写你的地址、密钥和模型名即可。

---

## 目录结构

```
├── run.sh                        # 一键入口（检查 / 评测 / 列出数据集）
├── requirements.txt              # Python 依赖
├── scripts/
│   ├── run_eval.py               # 通用评测引擎（预检查 + 限流重试 + 结果打包）
│   ├── eval_report_v2.py         # v2 效果评测报告生成器（六章模版）
│   ├── usage_hook.py             # evalscope 启动器：逐次记录每个 Chat 调用的原始 usage
│   ├── cache_stats.py            # 缓存命中率统计（严格口径，可单独运行）
│   └── perf_stats.py             # TPM / TPS 变化统计（按时间窗，可单独运行）
└── results/                      # 评测产物（自动创建，已由 .gitignore 排除）
```

---

## 支持的数据集

| 数据集 | 说明 | 默认 repeats | 需要 Judge | 需要 Docker |
|--------|------|:---:|:---:|:---:|
| `aime25` | AIME25 数学竞赛题 | 16 (pass@16) | ❌ | ❌ |
| `aime26` | AIME 2026 数学竞赛题 | 16 | ❌ | ❌ |
| `gpqa_diamond` | GPQA-Diamond 研究生级问答 | 3 | ❌ | ❌ |
| `hle` | Humanity's Last Exam 综合评测 | 1 | ✅ | ❌ |
| `tau2_bench` | tau2-bench Agent 对话评测 | 5 | ❌ | ❌ |
| `mmlu_pro` | MMLU-Pro 多学科多选题 | 1 | ❌ | ❌ |
| `simple_qa` | SimpleQA 事实准确性 | 1 | ✅ | ❌ |
| `longbench_v2` | LongBench v2 长上下文 | 1 | ❌ | ❌ |
| `live_code_bench` | LiveCodeBench 代码生成 | 1 | ❌ | ✅ |
| `swe_bench_verified_mini_agentic` | SWE-Bench Agentic | 1 | ❌ | ✅ |
| `swe_bench_pro` | SWE-Bench Pro | 1 | ❌ | ✅ |

---

## 快速开始

### 1. 配置

编辑项目根目录的 `configs/.env`：

```bash
API_URL=https://open.bigmodel.cn/api/paas/v4/chat/completions
API_KEY=your-api-key
MODEL_NAME=glm-5.2
PROVIDER=zhipu
```

### 2. 环境检查

```bash
bash quickstart.sh eval check
```

### 3. 列出支持的数据集

```bash
bash quickstart.sh eval list
```

### 4. 运行评测

```bash
# 单数据集
bash quickstart.sh eval bench aime25

# 多数据集
bash quickstart.sh eval bench aime25,gpqa_diamond

# 全部数据集
bash quickstart.sh eval bench all

# 带参数
bash quickstart.sh eval bench aime25 --repeats 8 --eval_batch_size 20
```

### 4.1 常用可选参数

以下参数由 `eval bench` 原样透传给评测引擎：

| 参数 | 说明 |
|------|------|
| `--limit N` | 每个数据集最多跑 N 条（冒烟验证） |
| `--repeats K` | 重复次数 / pass@k（默认按数据集，如 aime=16、gpqa=3、tau2=5） |
| `--eval_batch_size N` | 并发数（默认按数据集） |
| `--temperature` / `--top_p` / `--max_tokens` | 生成参数（缺省用服务端默认或引擎按模型推断） |
| `--thinking true/false` | 思考模式开关（默认开启） |
| `--thinking-type <t>` | thinking.type 取值（默认按供应商映射：minimax→adaptive，其余→enabled） |
| `--provider_override NAME` | 注入 `X-Provider-Override` 路由请求头 |
| `--subset_list airline,retail,telecom` | tau2_bench 领域子集 |
| `--mmlu_pro_subset` / `--longbench_v2_subset` / `--live_code_bench_subset` | 对应数据集子集 |

### 5. HLE / SimpleQA（需要 Judge 模型）

`hle` 与 `simple_qa` 使用 LLM-as-Judge 打分，**必须提供 Judge 配置**，否则预检查直接报错退出（`--datasets all` 因包含这两个数据集，同样强制要求）。judge 模型与地址有默认值（`deepseek-v4-pro` @ `https://api.deepseek.com/v1`），通常只需提供 `--judge_api_key`（或环境变量 `JUDGE_API_KEY`）：

```bash
bash quickstart.sh eval bench hle \
    --judge_model deepseek-v3-0324 \
    --judge_base_url https://api.example.com/v1 \
    --judge_api_key sk-xxx
```

也可写入 `configs/.env`，免每次传参（对应环境变量 `JUDGE_MODEL` / `JUDGE_BASE_URL` / `JUDGE_API_KEY`，优先级：CLI 参数 > 环境变量 > `.env`）：

```bash
JUDGE_MODEL=deepseek-v3-0324
JUDGE_BASE_URL=https://api.example.com/v1
JUDGE_API_KEY=sk-xxx
```

说明：

- `judge_base_url` 是 Judge 模型的 **OpenAI 兼容 base URL**（不含 `/chat/completions`），与被测模型的 `API_URL` 相互独立——即 Judge 可用其他供应商的模型
- 可选 `--judge_strategy`（`rule` / `llm` / `llm_recall` / `auto`），hle 与 simple_qa 默认已是 `llm`，一般无需指定
- 缺省时报错示例：`--judge_model（LLM Judge 需要）不能为空`

---

## 输出

评测结果保存在 `eval/results/<provider>-<model>-<timestamp>/` 下（run 结束自动清理冗余，只保留有效产物）：

```
results/zhipu-glm-5.2-20260702_180000/
├── all_eval_summary.json          # 总汇总（仅 run 根一份）
├── eval_results.tar.gz            # 打包归档（仅 run 根一份，含全部原始产物）
└── aime25/                        # 各数据集子目录
    ├── eval_report_v2.html        # v2 效果评测报告（六章模版，对外交付物）
    ├── eval_summary.json          # 单数据集摘要
    ├── per_sample_details.csv     # 逐题明细（sample_id / score / request / response / 逐题缓存命中）
    ├── usage_ledger.jsonl         # 逐次调用 usage 流水（缓存命中率的原始证据，不含正文）
    ├── configs/
    │   └── task_config.yaml       # 评测配置（实际生效参数）
    └── logs/                      # 运行日志
```

> 原始 `predictions/`、`reviews/`、`reports/`（evalscope 原生产物，通常占体积 90%+）
> 的信息已提炼进 `per_sample_details.csv` 与 `eval_report_v2.html`，
> 打包归档后会自动从散目录中清理；失败的数据集保留原始产物便于排障。

### v2 效果评测报告（eval_report_v2.html）包含

六章结构，对齐《效果测试报告模版》：
1. **核心结论**：正确率 / 题目数 / 跳过数 KPI 卡 + **缓存命中**（Token 命中率、命中调用占比、字段合规、被测模型调用命中率、单题命中率 P50/P10、零命中题数；按调用来源拆分：被测模型 / 用户模拟器；另按题内第 N 次调用给出预热曲线、按请求消息条数给出对话深度）——缓存相关数据全部集中在第一章，其他章节不再重复
2. **效果与稳定性**：多轮（repeats）得分对比
3. **用户体验与性能**：两块内容
   - **时延与速率分位**：TTFT（逐次调用）、生成速率（逐次调用 = 1/TPOT）、TPOT、输入/输出 Token、调用次数、题目耗时，均为 Avg / P50 / P90 / P95。TTFT 与生成速率统一只用逐次调用一个口径，不再重复给每题首次 / 每题整体；
   - **TPM / 吞吐变化**：按时间窗给出全程平均 TPM、窗口 TPM 的 Avg/P50/P90/P95、峰值与谷值、波动（变异系数），附 TPM 趋势折线图与逐窗口明细表。
4. **模型与评测配置**：实际生效参数（读自 task_config.yaml，凭证脱敏）
5. **逐题结果与评测证据**：每题完整思维链 / 答案 / 评分；逐题表新增「平均缓存命中率」列（多轮取各轮均值），展开后显示该题 cached / prompt tokens 与命中调用数，不合规调用标红
6. **异常跳过题目**：被 `ignore_errors` 静默丢弃的题及原因（来自 skipped_samples.jsonl，缺失时从运行日志兜底解析）

---

## 缓存命中率统计

每次评测都会记录整个过程中**每一次** Chat 调用的 usage，并统计缓存命中率：单轮题的每次调用、tau2 Agent 与用户模拟器的每一轮、SWE Agentic 的每一步都算在内。Judge 调用按被测模型名过滤掉，不计入。

### 推荐数据集：多轮 Agent 评测

单轮题（aime / gpqa 等）每题前缀都不一样，命中率天然很低，参考价值不大。**统计缓存命中率请跑多轮 Agent 评测**：同一会话里后一轮请求完整包含前一轮的上下文，能真实反映供应商的前缀缓存能力。

| 数据集 | 多轮形态 | 依赖 |
|--------|----------|------|
| `tau2_bench`（首选） | 航空 / 零售 / 电信客服，每题 10~40 轮，含工具定义与工具调用 | tau2（已随依赖安装） |
| `swe_bench_verified_mini_agentic` | 在代码仓内多步调用工具，上下文持续增长（走 evalscope AgentLoop，同一套调用链；尚未实跑验证） | Docker + ms_enclave |

```bash
# 冒烟：每个领域 1 题
bash quickstart.sh eval bench tau2_bench --subset_list airline,retail,telecom --limit 1 --repeats 1
# 完整
bash quickstart.sh eval bench tau2_bench --subset_list airline,retail,telecom
```

实测（智谱 glm-5.2，tau2 airline + retail 各 1 题，共 24 次调用）：命中率 84.23%（71,808 / 85,256 tokens），24/24 次调用字段合规。按对话深度看，2-4 条消息时命中率 70.44%，5-10 条时 89.30%。每个会话的首轮 cached=0，从第 2 轮起，Agent 轮次的 cached 达到 prompt 的 90% 以上。

### 统计口径（统一按 OpenAI Chat 协议）

| 项 | 定义 |
|----|------|
| 命中字段 | `usage.prompt_tokens_details.cached_tokens`（唯一认可的标准字段） |
| 分母 | `usage.prompt_tokens`（Chat 协议下为含缓存命中在内的全量输入） |
| Token 命中率 | Σcached_tokens / Σprompt_tokens，只在字段合规的调用上累加 |
| 命中调用占比 | cached_tokens > 0 的调用数 / 字段合规的调用数 |
| 分组 | 按请求消息条数（1 / 2-4 / 5-10 / 11-20 / 21-50 / >50）、是否带工具定义 |

### 对供应商的严格要求（验收项）

评测固定使用流式请求，并带上 `stream_options.include_usage=true`。**每一次** Chat 调用都必须满足以下要求：

1. 必须返回 `usage`。流式请求在末帧返回，不能省略。
2. `usage.prompt_tokens_details` 必须是对象，`cached_tokens` 必须是整数。**未命中时返回 `0`**，不能缺省、不能为 `null`。
3. 必须满足 `0 <= cached_tokens <= prompt_tokens`。
4. 命中数只认上述标准字段。`prompt_cache_hit_tokens`、`cache_read_input_tokens` 这类非标准字段只记录出现次数，**不计入命中**。

只要有一次调用不满足，该数据集的缓存字段就判为 **「不合规」**。判定结果写入报告和 `eval_summary.json.cache_stats.verdict`，并列出每类违规的次数。字段不合规时命中率无法可信计算，要求供应商整改后复测。

### 产物

- `<dataset>/usage_ledger.jsonl`：每行对应一次调用。记录的字段有：模型名、所属题目（`benchmark` / `subset` / `sample_id`）、调用来源（`role`：`subject` 为被测模型，`aux` 为用户模拟器等辅助调用）、消息条数、工具数，以及原始 `usage`。不记录请求和回复正文。该文件会随 `eval_results.tar.gz` 一起归档。
- `<dataset>/eval_summary.json` 的 `cache_stats` 字段：单个数据集的统计结果，包括总体、`by_role`、`by_turn`、`by_depth` 和 `per_sample_dist`（单题命中率分布）。
- `<dataset>/eval_summary.json` 的 `throughput_stats` 字段：TPM/TPS 变化统计，含窗口大小、各窗口明细与分位。
- `all_eval_summary.json` 的 `cache_stats` 字段：整次运行所有数据集合并后的统计结果。
- `<dataset>/per_sample_details.csv`：新增 `cache_calls / prompt_tokens / cached_tokens / cache_hit_rate / cache_hit_calls / cache_non_compliant_calls` 列，统计的是被测模型自身的调用。
- `eval_report_v2.html` 中的缓存指标集中在第一章「缓存命中」（KPI 卡片 + 按调用来源 / 按题内调用序号 / 按对话深度三张表），另在第五章逐题表给出「平均缓存命中率」列；第三章不再重复缓存指标。

单独重算：

```bash
python3 scripts/cache_stats.py results/<run>/tau2_bench --model <被测模型名>
python3 scripts/perf_stats.py  results/<run>/tau2_bench --model <被测模型名>   # TPM/TPS 变化
```

### TPM / 吞吐变化

`perf_stats.py` 用 ledger 里的调用时间戳按时间窗统计端点的 token 吞吐，输出全程平均 TPM、窗口 TPM 的 Avg/P50/P90/P95、峰值与谷值及其所在窗口、波动（变异系数），以及逐窗口明细（调用数 / 输入 tokens / 输出 tokens / TPM / 输出 TPM / TPS）。

- 时间窗默认**按时长自动选择**（目标 15 个窗口，候选 5s~15min），保证短跑也有足够分辨率；可用 `--window` 指定固定窗口。
- 分位与波动统计只取**完整且非空**的窗口：末尾不足一个窗口长度的、以及窗口内无调用的，会被剔除并单独计数，避免评测自身空档拉低统计。
- 口径为窗口内全部被测端点调用（含用户模拟器等辅助调用）的 `prompt_tokens + completion_tokens`，`TPM = tokens × 60 / 窗口秒数`。

实现方式：`run_eval.py` 通过 `scripts/usage_hook.py` 启动 evalscope，在进程内包装 `OpenAICompatibleAPI` 的 `generate` / `on_response`，**不修改 evalscope 源码**。如果当前解释器找不到 evalscope，会自动回退到 `evalscope` 命令行，此时报告显示「无数据」。

---

## 核心特性

### 限流重试

- 检测 429 / rate limit / too many requests 等限流特征
- 限流时等待 60s 后自动注入 `--use-cache` 续跑
- 非限流失败等待 5s 后重试
- 最多 3 次重试

### 预检查

运行前自动检查：
- evalscope 是否安装
- 必填参数是否完整
- 数据集是否已知
- 温度 / 并发 / repeats 参数合法性
- LLM Judge 数据集的 judge model 配置
- tau2_bench 子集合法性 + 自动安装 tau2 包
- Docker / swebench / ms_enclave 依赖检测

### 结果打包

评测完成后自动打包为 `eval_results.tar.gz`，包含：
- v2 效果评测报告（eval_report_v2.html）
- 总汇总 JSON / 各数据集 eval_summary.json
- 逐题明细 per_sample_details.csv
- 逐次调用 usage 流水 usage_ledger.jsonl（缓存命中率证据）
- evalscope 原始得分报告（reports/*.json）

---

## 常见问题

**Q: 首次运行提示 evalscope 未安装？**

```bash
bash quickstart.sh install
# 或
pip install evalscope==1.9.0   # requirements.txt 已固定 1.9.0，勿随意升级
```

**Q: tau2_bench 报 ImportError？**

脚本会自动安装 tau2-bench 包。如自动安装失败，手动执行：
```bash
pip install git+https://github.com/sierra-research/tau2-bench@v0.2.0
```

**Q: swe_bench 报缺少 swebench / ms_enclave？**

```bash
pip install 'evalscope[swe_bench]'  # 或 pip install swebench==4.1.0
pip install 'evalscope[sandbox]'    # 或 pip install ms-enclave
```

**Q: 如何只跑少量样本验证流程？**

```bash
bash quickstart.sh eval bench aime25 --limit 5
```

**Q: 中途中断后能续跑吗？**

能。脚本会在重试时自动探测缓存目录并注入 `--use-cache` 续跑。也可手动指定：
```bash
bash quickstart.sh eval bench aime25 --use_cache results/xxx/aime25/cache
```
