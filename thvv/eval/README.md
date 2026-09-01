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
│   └── eval_report_v2.py         # v2 效果评测报告生成器（六章模版）
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

### 5. HLE / SimpleQA（需要 Judge 模型）

```bash
bash quickstart.sh eval bench hle \
    --judge_model deepseek-v3-0324 \
    --judge_base_url https://api.example.com/v1 \
    --judge_api_key sk-xxx
```

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
    ├── per_sample_details.csv     # 逐题明细（sample_id / score / request / response）
    ├── configs/
    │   └── task_config.yaml       # 评测配置（实际生效参数）
    └── logs/                      # 运行日志
```

> 原始 `predictions/`、`reviews/`、`reports/`（evalscope 原生产物，通常占体积 90%+）
> 的信息已提炼进 `per_sample_details.csv` 与 `eval_report_v2.html`，
> 打包归档后会自动从散目录中清理；失败的数据集保留原始产物便于排障。

### v2 效果评测报告（eval_report_v2.html）包含

六章结构，对齐《效果测试报告模版》：
1. **核心结论**：正确率 / 题目数 / 跳过数 KPI 卡
2. **效果与稳定性**：多轮（repeats）得分对比
3. **用户体验与性能**：TTFT / TPOT / 吞吐分位数
4. **模型与评测配置**：实际生效参数（读自 task_config.yaml，凭证脱敏）
5. **逐题结果与评测证据**：每题完整思维链 / 答案 / 评分
6. **异常跳过题目**：被 `ignore_errors` 静默丢弃的题及原因（来自 skipped_samples.jsonl，缺失时从运行日志兜底解析）

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
- evalscope 原始得分报告（reports/*.json）

---

## 常见问题

**Q: 首次运行提示 evalscope 未安装？**

```bash
bash quickstart.sh install
# 或
pip install evalscope==1.8.0
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
