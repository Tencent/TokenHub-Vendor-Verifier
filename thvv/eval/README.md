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
| `swe_bench_pro` | SWE-Bench Pro（实例镜像过多，酌情测试） | 1 | ❌ | ✅ |

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

### 6. 代码类评测（live_code_bench / swe_bench_verified_mini_agentic）

#### 前置依赖

```bash
pip install 'evalscope[swe_bench]'   # = swebench==4.1.0
pip install 'evalscope[sandbox]'     # = ms-enclave（SWE Agent 容器沙箱）

# Docker daemon 可用；非 root 用户需加入 docker 组（否则预检查报"未检测到可用的 Docker daemon"）
sudo usermod -aG docker $USER && newgrp docker
```

swe_bench_verified_mini_agentic 需要预置 50 个实例镜像（每题一个，约 22GB，层共享后）：
镜像名规则 `swebench/sweb.eval.x86_64.<instance_id>.latest`（`instance_id` 中 `__` 替换为 `_1776_`，如 `django__django-11790` → `swebench/sweb.eval.x86_64.django_1776_django-11790:latest`）。批量预拉：

```bash
python3 - <<'EOF' > pull_sweb.sh
from modelscope import MsDataset
ds = MsDataset.load('evalscope/swe-bench-verified-mini', split='test')
for iid in ds['instance_id']:
    print('docker pull swebench/sweb.eval.x86_64.%s:latest' % iid.replace('__', '_1776_'))
EOF
bash pull_sweb.sh
```

> 国内拉取：Docker Hub 直连不通，`/etc/docker/daemon.json` 配 `registry-mirrors`（实测可用 `https://docker.1ms.run`，覆盖 swebench 冷门镜像；阿里云专属加速器仅限 ECS 内且只放行白名单镜像）。也可经 GitHub Action 中转到阿里云 ACR 再拉取（注意 ACR 镜像名会丢掉 `swebench/` 前缀，拉完必须 `docker tag` 回原名，否则评测引擎识别不到本地镜像）。

#### 实测命令（glm-5.3-flash @ 内网端点）

```bash
# LIVE-CODE-BENCH（release_latest 共 1055 题，16 并发，实测约 4h15m）
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

长跑建议后台执行：`nohup bash quickstart.sh eval bench ... > /tmp/xxx.log 2>&1 &`

#### 关键注意

- **`--live_code_bench_subset release_latest` 必须指定**：否则 evalscope 会遍历全部 29 个 subset（release_v1~v6 及各种组合，题目大量重叠），耗时成倍且口径错误
- **并发建议**：live_code_bench 单轮生成可开 16+（看端点吞吐）；swe_bench_verified_mini_agentic 每个并发独占一个 Docker 容器跑多轮 Agent（250 步上限），建议 4（激进可 8，注意 `docker ps` 容器数与 CPU）
- **SWE 长尾题**：单题 Agent 可磨 30-60 分钟（模型反复试错到步数上限），进度条长时间不动属正常，看 `docker ps` 容器仍在轮转即未卡死
- **live_code_bench 为本地进程执行模型生成的代码**（未启用沙箱），评测机会执行模型产出的任意 Python 代码，注意运行环境隔离
- 冒烟：`--limit 5`（LCB）/ `--limit 2`（SWE），链路通了再去掉全量
- 全量基准参考（glm-5.3-flash）：LiveCodeBench 89.29 / SWE-bench Verified Mini Agentic 88.00

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
