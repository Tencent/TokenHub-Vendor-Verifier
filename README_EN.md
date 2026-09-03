# THVV — TokenHub Vendor Verifier

An all-in-one verification toolkit for running **performance load tests** and **quality evaluations** against large-model vendors. Performance load testing supports OpenAI / Anthropic dual-protocol endpoints, while quality evaluation works with any OpenAI-compatible endpoint. Reports and structured artifacts are produced automatically once a run completes.

> Naming: THVV = TokenHub Vendor Verifier, used for capability verification and artifact archival before a vendor is onboarded.

---

## Capability Overview

| Module | Description | Status |
|------|------|:---:|
| `perf` | Performance load testing: full combination of input-length buckets (1k–200k) × concurrency ladders, with success-rate early stopping and adaptive temperature / tokenizer, producing dual reports — **HTML (37-column full metrics + failed-request details) + xlsx** | ✅ |
| `eval` | Quality evaluation: 11 mainstream datasets (AIME25/26, GPQA-Diamond, HLE, tau2-bench, MMLU-Pro, SimpleQA, LongBench v2, LiveCodeBench, SWE-Bench…), automatically producing the **evaluation report** | ✅ |

---

## Directory Structure

```
├── README.md / README_EN.md   # This guide (Chinese / English)
├── .gitignore                 # Artifacts, dataset caches and secrets are not committed (datasets via Git LFS)
└── thvv/                      # All entrypoints live under thvv/ (cd thvv first)
    ├── quickstart.sh          # One-shot entrypoint (check / install / perf / eval)
    ├── cli.py                 # Unified CLI: python3 thvv/cli.py perf|eval|check|install ...
    ├── configs/
    │   ├── env.example        # Config template (copy to .env to use)
    │   ├── env.demo           # .env demo (OpenAI + Anthropic protocols)
    │   ├── README.md          # Configuration reference
    │   └── .env               # Actual credentials (not committed)
    ├── perf/                  # Performance load testing
    │   ├── run.sh             # Subcommands: check / bench / bench-all / report
    │   ├── requirements.txt   # perf dependencies (evalscope[perf]>=0.13.0)
    │   ├── scripts/           # gen_perf_dashboard.py (single HTML exit) / gen_report_from_db.py (xlsx) /
    │   │                      # export_failure_details.py (failure CSV) / setup_tokenizer.py / sla_eval.py (SLA verdicts)
    │   ├── datasets/          # 10 datasets (1k~200k, 7 main buckets + 3 prefix-cache scenarios)
    │   ├── references/        # 性能测试报告模板.xlsx (report template)
    │   ├── 性能验收标准.xlsx  # Performance acceptance criteria (perf)
    │   └── results/           # Artifacts: 性能测试报告.html + 性能测试报告.xlsx
    └── eval/                  # Quality evaluation
        ├── run.sh             # Subcommands: check / bench / list
        ├── requirements.txt   # eval dependencies (evalscope pinned to 1.9.0)
        ├── scripts/run_eval.py       # Evaluation engine (pre-checks + rate-limit retries + packaging)
        ├── scripts/eval_report_v2.py # Report generator
        ├── 效果验收标准.xlsx  # Quality acceptance criteria (eval)
        └── results/           # Artifacts: eval_report_v2.html / eval_summary.json / per_sample_details.csv
```

---

## Quick Start

### 1. Configuration

```bash
cd thvv
cp configs/env.example configs/.env   # Fill in API_URL / API_KEY / MODEL_NAME / PROTOCOL / TOKENIZER
```

- `API_URL` must be the **full request path** (OpenAI: `.../v1/chat/completions`; Anthropic: `.../v1/messages`)
- `PROTOCOL` = `openai` | `anthropic` (perf supports both; eval speaks the OpenAI-compatible interface and usually needs no change)
- Config priority: CLI args > environment variables > `configs/.env`

> Besides quickstart.sh, a unified CLI is available: `python3 thvv/cli.py perf bench 1k 20 1` (equivalent passthrough, auto-loads `configs/.env` on startup)

### 2. Environment Check / Install Dependencies

```bash
bash quickstart.sh check
bash quickstart.sh install
```

### 3. Performance Load Testing

```bash
bash quickstart.sh perf bench 1k 20 200      # Single bucket: <bucket> [requests] [concurrency]
bash quickstart.sh perf bench-all            # All buckets × concurrency ladders (configurable via BUCKETS / CONCURRENCY_LADDER etc.)
bash quickstart.sh perf report               # Regenerate report from results/
```

Common environment variables: `BUCKETS`, `CONCURRENCY_LADDER`, `BUCKET_COOLDOWN`, `N_<label>` (requests per bucket), `SUCCESS_RATE_MIN` (success-rate early stop), `TEMPERATURE` (adaptive by model name by default: kimi/moonshot=1.0, others=0.0), `CLIENT` (client name stamped into the xlsx report). When `MODEL_NAME` matches a known model family the tokenizer is auto-detected; an explicit `TOKENIZER` takes precedence. See `thvv/perf/README.md` for details.

### 4. Quality Evaluation

```bash
bash quickstart.sh eval list                 # List the 11 datasets
bash quickstart.sh eval bench aime26         # Single dataset
bash quickstart.sh eval bench aime26 --limit 30 --repeats 1 --eval_batch_size 10
bash quickstart.sh eval bench all            # All datasets
```

For datasets requiring an LLM Judge (`hle` / `simple_qa`; `all` enforces this too), provide `--judge_api_key`. The judge model and base URL have defaults (`deepseek-v4-pro` @ `https://api.deepseek.com/v1`) and can be overridden via `--judge_model / --judge_base_url`. Rate limits automatically wait 60s and resume with `--use-cache`. See `thvv/eval/README.md` for details.

---

## Report Artifacts

### Quality Evaluation: `eval_report_v2.html` (one per dataset; the delivery artifact)

Six-chapter template, with all data sourced from evalscope on-disk outputs (reviews / reports/*.json / task_config.yaml / logs):

1. **Key Conclusions** — accuracy / question count / skipped-exception KPI
2. **Quality & Stability** — multi-round (repeats) score comparison
3. **User Experience & Performance** — TTFT / TPOT / throughput percentiles
4. **Model & Evaluation Config** — actually applied parameters (credentials redacted)
5. **Per-Question Results & Evaluation Evidence** — full chain-of-thought / answer / score for each question
6. **Skipped & Exception Questions** — questions silently dropped by `ignore_errors` and their cause classification

Typical results directory:

```
thvv/eval/results/<provider>-<model>-<timestamp>/
├── all_eval_summary.json        # Overall summary (one per run root)
├── eval_results.tar.gz          # Packaged archive (contains all raw artifacts)
└── aime26/
    ├── eval_report_v2.html      # Evaluation report (delivery artifact)
    ├── eval_summary.json        # Score summary
    ├── per_sample_details.csv   # Per-question details
    ├── configs/task_config.yaml # Actually applied config
    └── logs/                    # Run logs (fallback source for skipped samples)
```

> The raw `predictions/`, `reviews/`, and `reports/` (native evalscope outputs, 90%+ of the volume) are
> distilled into the per-question details and the report, then automatically removed from the loose
> directories after packaging; failed datasets keep their raw artifacts for troubleshooting.

### Performance Load Testing: `性能测试报告.html` (readable) + `性能测试报告.xlsx` (metrics)

**HTML** (single self-contained file, opens offline, four chapters, metrics fully consistent with xlsx):

1. **Overall Conclusion** — total requests / success / failure / overall success rate + recommendations
2. **Concurrency Ladder Comparison** — 37-column full metrics matrix: TTFT (Avg/Min/Max/P50/P90/P95/P99),
   TTLT / Rate / ITL (Avg/P50/P90/P95/P99), Tokens, Avg Total Time(ms),
   Output TPM / Output TPS / Input TPM / TPM
3. **Failure Analysis** — failure-reason aggregation (429 rate-limit auto-detected) + share + recommendations
4. **Failed-Request Details** — every failed request listed (HTTP status code / request body / response body / TTFT / TTLT)

**xlsx**: a 38-column "metrics" sheet (same metrics as HTML); failure details are only in the HTML, not duplicated in xlsx.

---

## Acceptance Criteria

### Performance Acceptance Criteria

Vendor performance acceptance criteria are defined in [`性能验收标准.xlsx`](./thvv/perf/性能验收标准.xlsx). Key points:

**Test Requirements**

1. Low-concurrency warm-up for 5min to eliminate cold-start effects
2. Concurrency-ladder load testing for 10–15min, collect data after stabilization; when TPM/RPM falls short of the committed spec, request success rate must meet the SLA

**TTFT / Request Success Rate** (by incremental InputTokens, cache excluded)

| InputTokens | Tencent-side standard |
|------|------|
| <6K | P90<5s |
| 6～16K | P90<5s |
| 16～32K | P90<8s |
| 32～64K | P90<15s |
| 64～128K | P90<35s |
| 128～256K | P90<70s |

**OTPS**

| Model active params | Tier | Tencent-side OTPS requirement |
|------|------|------|
| >10B models | L1 | ≥ 30 tokens/s |
| | L2 | ≥ 10 tokens/s |
| ≤10B models | / | ≥ 100 tokens/s |

> All of the above are verified using the Tencent-provided benchmark; see `thvv/perf/性能验收标准.xlsx` for details.

### Quality Acceptance Criteria

Quality acceptance baselines are defined in [`效果验收标准.xlsx`](./thvv/eval/效果验收标准.xlsx), with per-model accuracy allowed to float within **±2-4%**:

| Dataset | kimi-k3 | HY3 | deepseek-v4-flash-0731 | hy4-preview |
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

> "-" means the model did not provide a result for that dataset; see `thvv/eval/效果验收标准.xlsx` for details.

---

## Credential Security

- API keys only live in `configs/.env` or environment variables — **never write them into CLI args or code**
- `.env`, `results/`, and `datasets/` caches are all excluded by `.gitignore` and never enter the repository
- Credential fields in configs are automatically redacted during report generation

## Version Notes

- eval side: evalscope is pinned to `1.9.0` (a verified combination as of 2026-07-20; do not upgrade casually); perf side: `evalscope[perf]>=0.13.0`
