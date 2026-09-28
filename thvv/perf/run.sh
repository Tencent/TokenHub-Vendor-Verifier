#!/usr/bin/env bash
# ============================================================
# 大模型性能压测（OpenAI 协议通用）一键启动脚本
# ============================================================
# 用法：
#   bash run.sh check                        # 环境检查
#   bash run.sh bench <bucket> [N] [P]       # 跑单档压测
#   bash run.sh bench-all                    # 全档位 × 并发梯度压测
#   bash run.sh sla-tune <bucket>            # SLA 并发上限探索（单档，二分搜索）
#   bash run.sh sla-tune-all                 # SLA 并发上限探索（全档位）
#   bash run.sh report                       # 从 results/ 生成报告
#
# <bucket> = 1k | 9k | 16k | 32k | 64k | 128k | 200k
# N        = 请求总数，默认 500
# P        = 并发数，默认 10
#
# bench-all 环境变量：
#   BUCKETS             档位列表，默认 "1k 9k 16k 32k 64k 128k 200k"
#   CONCURRENCY_LADDER  并发梯度，默认 "1 8 32 64 128"
#   BUCKET_COOLDOWN     档间冷却秒数，默认 120
#   N_<label>           覆盖特定档位的请求数（如 N_1k=20 N_9k=100）
#   NONCE=0             关闭随机前缀注入（默认已开启：每条 prompt 注入随机 nonce 防前缀命中）
#   SUCCESS_RATE_MIN    成功率早停阈值（百分比，默认 0=关闭）；
#                       某档并发跑完后成功率低于此值时，跳过该 bucket 剩余更高并发，
#                       直接切到下一个 bucket。设为 0 可关闭早停。
#
# sla-tune 环境变量（evalscope perf --sla-auto-tune 二分搜索满足 SLA 的最大并发）：
#   SLA_LOWER_BOUND       并发搜索下界，默认 1
#   SLA_UPPER_BOUND       并发搜索上界，默认 256
#   SLA_NUM_RUNS          每个并发点重复次数（取平均消抖），默认 2
#   SLA_NUMBER_MULTIPLIER 每点请求数 = 并发 × N，默认 8
#   SLA_PARAMS            直接指定 --sla-params JSON（覆盖 bucket 默认映射）
#   SLA_COOLDOWN          sla-tune-all 档间冷却秒数，默认 120
#   DRY_RUN=1             只打印 evalscope 命令不执行（验证参数拼装）
#
# 配置：cp configs/env.example configs/.env 后填写（支持任意 OpenAI 兼容端点）
# 多模型：--env <name> 选择命名配置（configs/env.d/<name>.env），或直接
#   --url/--api-key/--model/--protocol/--tokenizer/--provider/--judge-api-key 覆盖；
#   结果目录含模型名 slug（results/perf-<bucket>-<model>-<ts>），多模型并行互不干扰
# ============================================================
set -eu

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$KIT_DIR"

# .env 在项目根目录 configs/ 下（perf 的上一级）
ROOT_DIR="$(cd "$KIT_DIR/.." && pwd)"

# ---- THVV 专属参数解析（--env / --url / --model 等，其余透传给子命令）----
source "$ROOT_DIR/lib/params.sh"
thvv_parse_env_args "$ROOT_DIR/configs" "$@"
set -- ${THVV_REST_ARGS[@]+"${THVV_REST_ARGS[@]}"}

# ---- 加载 .env ----
# 外部环境变量（命令行前缀，如 API_URL=... bash run.sh ...，或 quickstart 注入）优先于 .env：
# 先备份外部已设置的值，source .env 后再恢复，便于临时切换端点/模型跑单次任务。
# --env <name> 选中的命名 env 文件（THVV_ENV_FILE）优先于默认 configs/.env。
load_env() {
    local envfile="${THVV_ENV_FILE:-}"
    if [[ -z "$envfile" ]]; then
        envfile="$ROOT_DIR/configs/.env"
    fi
    if [[ ! -f "$envfile" && -f "configs/.env" ]]; then
        envfile="configs/.env"
    fi
    if [[ ! -f "$envfile" ]]; then
        echo "[warn] configs/.env 不存在，使用环境变量或默认值" >&2
        return 0
    fi
    thvv_load_env_file "$envfile"
}

load_env

# CLI 直传覆盖（--url / --model 等）在 env 文件加载后生效（优先级最高）
thvv_apply_overrides

# 统一变量名：API_URL / API_KEY / MODEL_NAME
API_URL="${API_URL:-${TOKENHUB_URL:-}}"
API_KEY="${API_KEY:-${TOKENHUB_KEY:-}}"
MODEL_NAME="${MODEL_NAME:-${TOKENHUB_MODEL:-}}"

# 协议：openai（OpenAI Chat Completions）或 anthropic（Anthropic Messages，Claude 系列）
# 两者均基于 evalscope perf 引擎，通过 --api 选择对应 plugin
PROTOCOL="${PROTOCOL:-openai}"
if [[ "$PROTOCOL" != "openai" && "$PROTOCOL" != "anthropic" ]]; then
    echo "[fail] PROTOCOL 仅支持 openai 或 anthropic，当前 PROTOCOL=$PROTOCOL" >&2
    exit 1
fi

# 模型名 slug（结果目录命名用，多模型并行/切换测试时隔离产物）
MODEL_SLUG="$(printf '%s' "${MODEL_NAME:-}" | sed 's/[^A-Za-z0-9._-][^A-Za-z0-9._-]*/_/g')"
MODEL_SLUG="${MODEL_SLUG:-model}"

# 采样温度：按 MODEL_NAME 自动决定，无需 setup_patch.sh 改 evalscope 包
#   - Kimi / moonshot 系列（thinking 模型）在 temperature=0 时输出异常，需 1.0
#   - 其他模型（deepseek/glm/minimax 等）保持 0.0，性能更稳定
#   - 用户可通过环境变量 TEMPERATURE 显式覆盖（优先级最高）
if [[ -z "${TEMPERATURE:-}" ]]; then
    case "$(echo "${MODEL_NAME:-}" | tr '[:upper:]' '[:lower:]')" in
        *kimi*|*moonshot*)  TEMPERATURE="1.0" ;;
        *)                  TEMPERATURE="0.0" ;;
    esac
fi
echo "[info] MODEL_NAME=$MODEL_NAME → temperature=$TEMPERATURE" >&2

# 保存用户在 .env 中显式设置的 TOKENIZER（如果没设则为空）
_TOKENIZER_USER="${TOKENIZER:-}"

# Tokenizer 默认值仅作示例
TOKENIZER="${TOKENIZER:-zai-org/GLM-4.6}"

# 根据 MODEL_NAME 关键词自动推断 tokenizer；
# 仅当用户未在 configs/.env 中显式设置 TOKENIZER 时才生效
auto_detect_tokenizer() {
    [[ -n "$_TOKENIZER_USER" ]] && return 0  # 用户已显式设置，不覆盖
    local lower
    lower=$(echo "${MODEL_NAME:-}" | tr '[:upper:]' '[:lower:]')
    local detected=""
    case "$lower" in
        *glm*|*chatglm*)          detected="zai-org/GLM-4.6" ;;
        *deepseek*|*deep-seek*)   detected="deepseek-ai/DeepSeek-V3" ;;
        *minimax*|*mini-max*)     detected="MiniMaxAI/MiniMax-M2" ;;
        *kimi*|*moonshot*)        detected="moonshotai/Kimi-K2-Thinking" ;;
        *qwen*|*tongyi*)          detected="Qwen/Qwen3-235B-A22B" ;;
        *llama*)                  detected="meta-llama/Llama-3.1-8B-Instruct" ;;
        *mistral*)                detected="mistralai/Mistral-7B-Instruct-v0.3" ;;
        *baichuan*)               detected="baichuan-inc/Baichuan2-13B-Chat" ;;
        *yi-*|*yi_*|*yi-l*)       detected="01-ai/Yi-1.5-34B-Chat" ;;
        *gemma*|*gemini*)         detected="google/gemma-2-2b" ;;
        *claude*|*anthropic*)     detected="Xenova/gpt-4o" ;;  # Claude 无公开 tokenizer，用 tiktoken 近似
        *gpt*|*openai*)           detected="Xenova/gpt-4o" ;;
        *phi-*|*phi_*|*phi3*)     detected="microsoft/Phi-3-mini-4k-instruct" ;;
    esac
    if [[ -n "$detected" ]]; then
        TOKENIZER="$detected"
        echo "[info] 根据 MODEL_NAME ($MODEL_NAME) 自动选择 tokenizer: $TOKENIZER" >&2
    fi
}

# ---- 检测并安装缺失依赖 ----
ensure_perf_deps() {
    local missing=()
    # evalscope（CLI 命令检测）
    if ! evalscope --version &>/dev/null; then
        missing+=("evalscope[perf]")
    fi
    # Python 包检测
    for pkg in openpyxl transformers modelscope; do
        python3 -c "import $pkg" 2>/dev/null || missing+=("$pkg")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "[deps] 缺少依赖: ${missing[*]}，正在安装..."
        python3 -m pip install -r requirements.txt -q || {
            python3 -m pip install -r requirements.txt -q --force-reinstall || {
                echo "[fail] 依赖安装失败，请手动执行: pip install -r requirements.txt"
                exit 1
            }
        }
        echo "[deps] 依赖安装完成"
    fi
}

# ---- 检测 tokenizer 本地缓存，未缓存则自动下载 ----
# 用户无需手动执行 setup_tokenizer.py；首次压测时自动从 HuggingFace / ModelScope 下载
ensure_tokenizer_cached() {
    [[ -z "$TOKENIZER" ]] && return 0
    # 用 local_files_only=True 探测；命中则直接返回，未命中则触发下载
    python3 - "$TOKENIZER" "$KIT_DIR/scripts/setup_tokenizer.py" <<'PY'
import sys, os
tok_name = sys.argv[1]
setup_script = sys.argv[2]
try:
    from transformers import AutoTokenizer
    AutoTokenizer.from_pretrained(tok_name, trust_remote_code=True, local_files_only=True)
    print(f"  [tokenizer] 已缓存: {tok_name}")
except Exception:
    print(f"  [tokenizer] 未缓存，开始下载 {tok_name} ...")
    # 调用 setup_tokenizer.py 完成下载 + 注入 chat_template
    ret = os.system(f"python3 '{setup_script}'")
    if ret != 0:
        print(f"  [tokenizer] 下载失败，请检查网络或手动执行: python3 {setup_script}", file=sys.stderr)
        sys.exit(1)
    print(f"  [tokenizer] 下载完成: {tok_name}")
PY
}

# ---- nonce 注入：每条 prompt 加随机前缀（防 prefix cache 命中） ----
# 用法：NONCE=1 bash run.sh bench 16k 100 20
# 原理：每条数据第一条 system/user 消息前拼 [nonce:<uuid>]（+~9 token，逐行独立 nonce），
#       token 序列在消息内容第一个 token 就分叉（模板前缀 <1 个 cache block），
#       同轮不同行、跨轮重复压测 prefix cache 均零命中。
#       实测：不注入二次跑 97.75% 命中，注入后 0.00%。
#       注意：nonce 按数据集行注入；请求数 > 行数时同一行被复用仍是相同 prompt，
#       严格零命中口径仍需保证 行数 ≥ 最大请求数。
inject_nonce() {
    local ds="$1"
    local out="/tmp/thvv_nonce/$(basename "${ds%.jsonl}").nonce.$$.jsonl"
    mkdir -p "$(dirname "$out")"
    python3 - "$ds" "$out" <<'PY'
import json, sys, uuid
src, dst = sys.argv[1], sys.argv[2]
count = 0
with open(src) as f, open(dst, 'w') as g:
    for line in f:
        line = line.strip()
        if not line:
            continue
        msgs = json.loads(line)
        nonce = f"[nonce:{uuid.uuid4().hex[:8]}]"
        for m in msgs:
            if m.get('role') in ('system', 'user') and isinstance(m.get('content'), str):
                m['content'] = f"{nonce}\n{m['content']}"
                break
        g.write(json.dumps(msgs, ensure_ascii=False) + '\n')
        count += 1
print(f"[nonce] 已为 {count} 行逐行注入独立随机 nonce（每条 +~9 token）→ {dst}", file=sys.stderr)
PY
    echo "$out"
}


# ---- rank 预热: 并发发 N 个短请求, 覆盖全部 prefill/decode rank 的 chat 冷路径 ----
# 背景: bench-all 顺序跑 c=1..64 只覆盖部分 rank, 更高并发首波会有 rank 首次收请求,
#       ApiServer 进程的 chat 冷路径 (template 编译/parser lazy import/kernel 首载
#       ~2-5s) -> 首波 TTFT 长尾 (实测 1k@c128: 首波 128 请求 avg 4.45s / 稳态 avg
#       0.7s, p90 5.55s 全部来自首波)。预热后 p90 回到稳态水平。
# WARMUP=0 可关闭 (默认开)
warmup_ranks() {
    local conc="${1:-128}"
    local t0=$(date +%s)
    echo "[warmup] 并发 $conc 个短请求预热 (覆盖全部 P/D rank chat 冷路径)..."
    for i in $(seq 1 "$conc"); do
        curl -s -o /dev/null -m 180 \
            -H "Authorization: Bearer ${API_KEY}" \
            -H "Content-Type: application/json" \
            -X POST "$API_URL" \
            -d "{\"model\":\"${MODEL_NAME}\",\"messages\":[{\"role\":\"user\",\"content\":\"warmup ${RANDOM}-${RANDOM}\"}],\"max_tokens\":4}" &
    done
    wait
    echo "[warmup] 完成 ($(( $(date +%s) - t0 ))s)"
}

# ---- bucket -> token 区间 ----
bucket_range() {
    case "$1" in
        1k)   echo "870 1228" ;;
        9k)   echo "8000 11000" ;;
        16k)  echo "14000 18000" ;;
        32k)  echo "28000 36000" ;;
        64k)  echo "56000 72000" ;;
        128k) echo "112000 144000" ;;
        200k) echo "180000 220000" ;;
        *) echo "0 999999" ;;
    esac
}

cmd_check() {
    auto_detect_tokenizer
    echo "[check] python3..."
    python3 --version
    echo "[check] evalscope..."
    evalscope --version || { echo "[fail] evalscope 未安装：pip install evalscope[perf]"; exit 1; }
    echo "[check] python deps..."
    python3 -c "import openpyxl, sqlite3" \
        && echo "  OK: openpyxl/sqlite3" \
        || { echo "[fail] 缺少依赖：pip install openpyxl"; exit 1; }
    echo "[check] tokenizer..."
    if [[ -n "$TOKENIZER" ]]; then
        ensure_tokenizer_cached
    else
        echo "  [warn] TOKENIZER 未设置，请在 configs/.env 中指定 tokenizer 仓库名"
    fi
    echo "[check] datasets..."
    for f in 1k 9k 16k 32k 64k 128k 200k; do
        if [[ -f "datasets/perf_zh_${f}.jsonl" ]]; then
            sz=$(du -h "datasets/perf_zh_${f}.jsonl" | cut -f1)
            echo "  OK: perf_zh_${f}.jsonl ($sz)"
        else
            echo "  [warn] 缺失: datasets/perf_zh_${f}.jsonl"
        fi
    done
    echo "[check] configs/.env..."
    [[ -f "$ROOT_DIR/configs/.env" ]] && echo "  OK" || echo "  [warn] 不存在，请 cp configs/env.example configs/.env"
    echo "[check] API_URL / API_KEY / MODEL_NAME..."
    [[ -n "$API_URL" && -n "$API_KEY" && -n "$MODEL_NAME" ]] \
        && echo "  OK (API端点、Key、模型名均已设置)" \
        || echo "  [warn] 缺少 API_URL/API_KEY/MODEL_NAME 中的一项或多项，请检查 configs/.env"
    echo "[check] PROTOCOL..."
    case "$PROTOCOL" in
        openai)    echo "  OK: $PROTOCOL（perf 支持）" ;;
        anthropic) echo "  OK: $PROTOCOL（perf 支持，Claude 系列）" ;;
        *) { echo "  [fail] perf 仅支持 openai / anthropic 协议，当前 PROTOCOL=$PROTOCOL"; exit 1; } ;;
    esac
}

cmd_bench() {
    ensure_perf_deps
    auto_detect_tokenizer
    ensure_tokenizer_cached
    local bucket="$1"
    local n="${2:-500}"
    local p="${3:-10}"
    local ds="datasets/perf_zh_${bucket}.jsonl"
    [[ ! -f "$ds" ]] && { echo "数据集不存在: $ds"; exit 1; }
    if [[ "${NONCE:-1}" == "1" ]]; then
        ds="$(inject_nonce "$ds")"
    fi
    [[ -z "$API_KEY" ]] && { echo "[fail] API_KEY 未设置，请编辑 configs/.env"; exit 1; }
    [[ -z "$API_URL" ]] && { echo "[fail] API_URL 未设置，请编辑 configs/.env"; exit 1; }
    [[ -z "$MODEL_NAME" ]] && { echo "[fail] MODEL_NAME 未设置，请编辑 configs/.env"; exit 1; }

    local range=($(bucket_range "$bucket"))
    local minlen=${range[0]} maxlen=${range[1]}
    local ts=$(date +%Y%m%d-%H%M%S)
    local out="results/perf-${bucket}-${MODEL_SLUG}-${ts}"
    mkdir -p "$out"

    echo "[bench] model=$MODEL_NAME bucket=$bucket parallel=$p number=$n"
    echo "        url=$API_URL"
    echo "        tokenizer=$TOKENIZER"
    echo "        out=$out"
    echo ""

    if [[ "${WARMUP:-1}" == "1" ]]; then
        warmup_ranks $(( p > 128 ? p : 128 ))
    fi

    evalscope perf \
        --model "$MODEL_NAME" --api "$PROTOCOL" \
        --tokenizer-path "$TOKENIZER" \
        --url "$API_URL" --api-key "$API_KEY" \
        --temperature "$TEMPERATURE" \
        --parallel "$p" --number "$n" \
        --dataset custom_multi_turn \
        --dataset-path "$ds" \
        --min-prompt-length "$minlen" --max-prompt-length "$maxlen" \
        --max-tokens "${MAX_TOKENS:-4096}" --multi-turn --max-turns 1 \
        --connect-timeout 60 --read-timeout 600 --db-commit-interval 5 \
        --outputs-dir "$out" --no-timestamp --no-test-connection \
        2>&1 | tee "$out/run.log"

    echo ""
    echo "[done] 结果目录: $KIT_DIR/$out"

    # 报告出口（跑完才会到这里；Ctrl+C 中断会直接终止脚本，不生成报告）：
    #   性能测试报告.html = 阅读版：总体结论 + 并发梯度矩阵 + 失败原因聚合 + 失败请求逐条明细
    #   性能测试报告.xlsx = 指标交付版：38 列聚合指标 + 失败请求详情 sheet
    echo "[report] 生成性能测试报告 ..."
    python3 scripts/gen_perf_dashboard.py \
        --run-dir "$out" \
        --title "${MODEL_NAME} 性能压测报告 (${bucket})" \
        --model "$MODEL_NAME" \
        --out "$out/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_${ts}.html" \
        2>&1 | tail -3 || echo "[warn] 报告生成失败，可手动运行: python3 scripts/gen_perf_dashboard.py --run-dir $out"
    echo "[report] 生成 Excel 指标报告 ..."
    python3 scripts/gen_report_from_db.py --results-dir results \
        --filter "$(basename "$out")" \
        --vendor "${PROVIDER:-unknown}" --model "$MODEL_NAME" \
        --no-tpm-html \
        --out "$out/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_${ts}.xlsx" \
        2>&1 | tail -3 || echo "[warn] Excel 报告生成失败，可手动运行: python3 scripts/gen_report_from_db.py --results-dir results --filter $(basename "$out")"
    echo "[report] 📄 报告(HTML): $out/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_${ts}.html"
    echo "[report] 📊 报告(Excel): $out/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_${ts}.xlsx"
}

cmd_report() {
    # 手动补报告：参数直接透传给 gen_perf_dashboard.py
    # 例: bash run.sh report --run-dir results/perf-1k-20260831-2143 --model glm-5.3-flash
    python3 scripts/gen_perf_dashboard.py "$@"
}

# ---- 并发梯度 → 默认请求数 ----
# 上游 8695981：降低并发档位默认请求数（默认梯度单 bucket 1270→800）
parallel_number() {
    case "$1" in
        1)   echo 20  ;;
        4)   echo 40  ;;
        8)   echo 60  ;;
        16)  echo 80  ;;
        32)  echo 120 ;;
        64)  echo 200 ;;
        128) echo 400 ;;
        *)   echo 100 ;;
    esac
}


# ---- SLA 阈值表（THVV sla_eval 口径，200k 归 128k+ 档）----
sla_thresholds() {
    case "$1" in
        1k)   echo "2.0 5.0" ;;
        9k)   echo "4.0 8.0" ;;
        16k)  echo "4.0 8.0" ;;
        32k)  echo "8.0 15.0" ;;
        64k)  echo "15.0 35.0" ;;
        128k) echo "30.0 70.0" ;;
        200k) echo "30.0 70.0" ;;
        *)    echo "" ;;
    esac
}

# ---- SLA 判定: TTFT avg ≤ P50 阈值 且 P90 ≤ P90 阈值 且 成功率 100% 且 OTPS ≥ 30 ----
sla_judge() {
    local bucket="$1" perf_summary="$2" sr="$3"
    local th=($(sla_thresholds "$bucket"))
    [[ ${#th[@]} -ne 2 ]] && return
    local avg_s p90_s otps
    avg_s=$(grep -m1 "│ TTFT (ms)" "$perf_summary" | awk -F'│' '{gsub(/ /,"",$5); printf "%.3f", $5/1000}')
    p90_s=$(grep -m1 "First-Turn TTFT (s)" "$perf_summary" | awk -F'│' '{gsub(/ /,"",$7); print $7}')
    otps=$(grep -m1 "Decode toks/s" "$perf_summary" | awk -F'│' '{gsub(/ /,"",$5); print $5}')
    [[ -z "$avg_s" || -z "$p90_s" || -z "$otps" ]] && return
    local ok=1
    awk "BEGIN{exit !($avg_s > ${th[0]})}"  && ok=0
    awk "BEGIN{exit !($p90_s > ${th[1]})}"  && ok=0
    awk "BEGIN{exit !($otps < 30)}"         && ok=0
    [[ "$sr" != "100.00" ]]                 && ok=0
    if [[ $ok -eq 1 ]]; then
        echo "PASS (avg ${avg_s}s ≤ ${th[0]} / P90 ${p90_s}s ≤ ${th[1]} / OTPS ${otps} / 成功率 ${sr}%)"
    else
        echo "FAIL (avg ${avg_s}s vs ${th[0]} / P90 ${p90_s}s vs ${th[1]} / OTPS ${otps} vs 30 / 成功率 ${sr}%)"
    fi
}

# ---- 从 benchmark_summary.json 提取成功率（跑完后用）----
extract_success_rate() {
    local summary_file="$1"
    python3 -c "
import json, sys
with open('$summary_file') as f:
    d = json.load(f)
total = d.get('Total Requests', 0)
success = d.get('Success Requests', 0)
print(f'{success / max(total, 1) * 100:.2f}')
" 2>/dev/null || echo "0"
}

# ---- 全档位压测（梯度递进 + 冷却）----
# 说明：只在全部 bucket 正常跑完后自动生成报告。
# 若中途 Ctrl+C 中断或异常失败，请手动运行: bash run.sh report --client "供应商名称"
cmd_bench_all() {
    ensure_perf_deps
    auto_detect_tokenizer
    ensure_tokenizer_cached

    local buckets="${BUCKETS:-1k 9k 16k 32k 64k 128k 200k}"
    local conc_ladder="${CONCURRENCY_LADDER:-1 8 32 64 128}"
    local cooldown="${BUCKET_COOLDOWN:-120}"
    # 成功率早停阈值（百分比，默认 0=关闭；设为非 0 值如 99.9 开启）
    # 参考 benjaminswu-perf 分支：某档并发跑完后成功率低于阈值 → 跳过该 bucket 剩余并发
    local sr_min="${SUCCESS_RATE_MIN:-0}"

    local -a bucket_arr=($buckets)
    local -a conc_arr=($conc_ladder)
    local total=${#bucket_arr[@]} idx=0
    local n_done=0 n_fail=0

    local ts; ts=$(date +%Y%m%d-%H%M%S)
    local group_dir="results/_group_${MODEL_SLUG}_${ts}"
    mkdir -p "$group_dir"
    local group_log="$group_dir/group.log"

    tlog() { echo -e "$*" | tee -a "$group_log"; }

    tlog "========================================================="
    tlog "  全档位压测（梯度递进模式）"
    tlog "  Model    : $MODEL_NAME"
    tlog "  URL      : $API_URL"
    tlog "  Buckets  : $buckets"
    tlog "  并发梯度 : $conc_ladder"
    tlog "  档间冷却 : ${cooldown}s"
    tlog "  成功率早停阈值: ${sr_min}% (SUCCESS_RATE_MIN，设 0 关闭)"
    tlog "  Group dir: $group_dir"
    tlog "========================================================="

    for label in "${bucket_arr[@]}"; do
        idx=$((idx+1))
        local ds="datasets/perf_zh_${label}.jsonl"
        if [[ ! -f "$ds" ]]; then
            tlog "⚠ [$label] 数据集不存在: $ds，跳过"
            continue
        fi
        tlog ""
        tlog "========== [$idx/$total] bucket=$label  并发梯度: ${conc_arr[*]} (早停阈值: 成功率<${sr_min}%) =========="

        local bucket_stopped=0
        for p in "${conc_arr[@]}"; do
            local n_var="N_${label}"
            local n="${!n_var:-$(parallel_number "$p")}"

            tlog "---------- [bucket=$label parallel=$p number=$n] ----------"
            if [[ "${WARMUP:-1}" == "1" ]]; then
                warmup_ranks $(( p > 128 ? p : 128 ))
            fi
            # 点级 nonce: 每个并发点独立注入 (同档跨点共享 nonce -> 后续点命中前面点
            # 写入 store/GPU prefix cache 的 KV, TTFT 虚低; 0921 实测 128k/200k 档
            # c=128 命中 48.5%, 200k TTFT 15.4s vs 独立口径 28.7s)
            local ds_run="$ds"
            if [[ "${NONCE:-1}" == "1" ]]; then
                ds_run="$(inject_nonce "$ds")"
            fi

            local range=($(bucket_range "$label"))
            local minlen=${range[0]} maxlen=${range[1]}
            local run_ts=$(date +%Y%m%d-%H%M%S)
            local out="results/perf-${label}-${MODEL_SLUG}-${run_ts}"
            mkdir -p "$out"

            tlog "[bench] model=$MODEL_NAME bucket=$label parallel=$p number=$n"
            tlog "        out=$out"

            # stdbuf 让 evalscope 输出无缓冲，tee 同时写日志和控制台（QCI 可实时看到进度）
            stdbuf -oL -eL evalscope perf \
        --model "$MODEL_NAME" --api "$PROTOCOL" \
        --tokenizer-path "$TOKENIZER" \
        --url "$API_URL" --api-key "$API_KEY" \
        --temperature "$TEMPERATURE" \
        --parallel "$p" --number "$n" \
        --dataset custom_multi_turn \
        --dataset-path "$ds_run" \
        --min-prompt-length "$minlen" --max-prompt-length "$maxlen" \
        --max-tokens "${MAX_TOKENS:-4096}" --multi-turn --max-turns 1 \
        --connect-timeout 60 --read-timeout 600 --db-commit-interval 5 \
        --outputs-dir "$out" --no-timestamp --no-test-connection \
        2>&1 | tee "$out/run.log" &
            local pid=$!
            tlog "[bench] PID=$pid  日志: $out/run.log"

            # tee 的 PID 是 $pid，evalscope 是子进程；等待 tee 结束（evalscope 结束后 tee 也会结束）
            # 不做运行中 kill——参考 benjaminswu-perf：等 evalscope 自然跑完再判断成功率
            local tee_pid=$pid
            local wait_secs=0
            while kill -0 "$tee_pid" 2>/dev/null; do
                sleep 30
                wait_secs=$((wait_secs+30))
                tlog "⏳ [$label/p=$p] 仍在运行 ... 已等待 ${wait_secs}s (PID=$pid)"
            done
            tlog "⏳ [$label/p=$p] 已结束，耗时 ${wait_secs}s"

            # 注意：symlink 目标必须相对 group_dir 解析（"../<run>"，group_dir 位于 results/ 下）；
            # 直接用 "$out"（相对 thvv/perf 的路径）会得到悬空链接，导致汇总 HTML 扫不到 db
            ln -sfn "../$(basename "$out")" "$group_dir/$(basename "$out")"

            # 跑完后从 benchmark_summary.json 提取成功率，判断是否早停
            local summary
            summary=$(find "$out" -maxdepth 4 -name benchmark_summary.json 2>/dev/null | head -1)
            if [[ -n "$summary" ]]; then
                local sr
                sr=$(extract_success_rate "$summary")
                tlog "✅ [$label/p=$p] done → 成功率=${sr}%"
                local perf_summary
                perf_summary=$(find "$out" -maxdepth 4 -name performance_summary.txt 2>/dev/null | head -1)
                if [[ -n "$perf_summary" ]]; then
                    local sla; sla=$(sla_judge "$label" "$perf_summary" "$sr")
                    [[ -n "$sla" ]] && tlog "   SLA [$label/p=$p]: $sla"
                fi
                n_done=$((n_done+1))

                # 成功率 < 阈值 → 早停该 bucket（用 awk 做浮点比较）
                if [[ -n "$sr_min" ]] && echo "$sr_min <= 0" | bc | grep -q "^1$"; then
                    :  # 阈值为 0，关闭早停
                elif awk "BEGIN{exit !($sr < $sr_min)}"; then
                    tlog "🛑 [$label/p=$p] 成功率 ${sr}% < ${sr_min}%，停止该 bucket 剩余更高并发，切换下一个 bucket"
                    bucket_stopped=1; break
                fi
            else
                tlog "❌ [$label/p=$p] 未生成 benchmark_summary.json (查看 $out/run.log)"
                n_fail=$((n_fail+1)); bucket_stopped=1; break
            fi
        done

        if [[ $bucket_stopped -eq 0 ]]; then
            tlog "✅ [$label] 全部并发级别完成（未触发早停）"
        fi

        if [[ $idx -lt $total && $cooldown -gt 0 ]]; then
            tlog "⏸  bucket cooldown ${cooldown}s before next bucket ..."
            sleep "$cooldown"
        fi
    done

    tlog ""
    tlog "========================================================="
    tlog "  完成: $n_done  失败: $n_fail"
    tlog "  Group dir: $group_dir"
    tlog "  日志: $group_log"
    tlog "========================================================="

    # 报告出口：跨档位汇总性能测试报告.html（阅读版）+ 性能测试报告.xlsx（指标交付版）
    # （--run-dir 指向组目录；gen_perf_dashboard 的 os.walk 已开启 followlinks，
    #   组目录内的 run 软链可正常扫描，且不会混入 results/ 下其他历史 run）
    tlog "[report] 生成性能测试报告 ..."
    python3 scripts/gen_perf_dashboard.py \
        --run-dir "$group_dir" \
        --title "${MODEL_NAME} 性能压测报告 (全档位 ${ts})" \
        --model "$MODEL_NAME" \
        --out "$group_dir/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_${ts}.html" \
        2>&1 | tee -a "$group_log" || tlog "[warn] 报告生成失败，可手动运行: bash run.sh report --run-dir $group_dir"
    tlog "[report] 生成 Excel 指标报告 ..."
    python3 scripts/gen_report_from_db.py --results-dir "$group_dir" \
        --vendor "${PROVIDER:-unknown}" --model "$MODEL_NAME" \
        --no-tpm-html \
        --out "$group_dir/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_${ts}.xlsx" \
        2>&1 | tee -a "$group_log" || tlog "[warn] Excel 报告生成失败，可手动运行: bash run.sh report"

    tlog ""
    tlog "📄 报告(HTML): $group_dir/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_${ts}.html"
    tlog "📊 报告(Excel): $group_dir/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_${ts}.xlsx"
}

# ==================== SLA 并发上限探索（evalscope --sla-auto-tune） ====================

# ---- bucket -> SLA 约束 JSON ----
# 口径来源：thvv/perf/scripts/sla_eval.py TIERS 双阈值（每档 {"p50": avg_ttft 上限, "p90": p90_ttft 上限}），
# bucket 按平均输入长度归档（与 sla_eval.py 事后复判口径一致）：
#   1k(avg~1K)→<4K 2s/5s；9k、16k(avg 8K~18K)→<32K 4s/8s；32k(avg~32K)→<64K 8s/15s；
#   64k(avg~64K)→<128K 15s/35s；128k(avg~128K)、200k→<256K 30s/70s
# 另附 avg_tpot<=0.033 s/token（≈单请求 OTPS ≥ 30 t/s，对应 RATE_MIN_TOKENS_PER_SEC）；
# 请求成功率 100% 为 evalscope auto-tune 内置硬门槛，无需写入。
# 注意：evalscope Arguments.sla_params 要求数组（list of group），单组即 AND 语义；
#       TTFT/TPOT 指标单位为毫秒（ms），阈值按 TIERS 秒口径 ×1000 换算，
#       avg_tpot <= 33ms ≈ 单请求 OTPS ≥ 30 t/s。
sla_params_for_bucket() {
    case "$1" in
        1k)   echo '[{"avg_ttft": "<=2000",  "p90_ttft": "<=5000",  "avg_tpot": "<=33"}]' ;;
        9k)   echo '[{"avg_ttft": "<=4000",  "p90_ttft": "<=8000",  "avg_tpot": "<=33"}]' ;;
        16k)  echo '[{"avg_ttft": "<=4000",  "p90_ttft": "<=8000",  "avg_tpot": "<=33"}]' ;;
        32k)  echo '[{"avg_ttft": "<=8000",  "p90_ttft": "<=15000", "avg_tpot": "<=33"}]' ;;
        64k)  echo '[{"avg_ttft": "<=15000", "p90_ttft": "<=35000", "avg_tpot": "<=33"}]' ;;
        128k) echo '[{"avg_ttft": "<=30000", "p90_ttft": "<=70000", "avg_tpot": "<=33"}]' ;;
        200k) echo '[{"avg_ttft": "<=30000", "p90_ttft": "<=70000", "avg_tpot": "<=33"}]' ;;
        *)    echo "" ;;
    esac
}

# ---- 单档 SLA 探索（内部函数，cmd_sla_tune / cmd_sla_tune_all 复用）----
# 注意：auto-tune 多个并发点复用同一数据集文件；NONCE=1 已逐行注入独立 nonce，
#       但请求数 > 数据集行数时同一行（含其 nonce）被复用仍是相同 prompt；
#       严格零 cache 命中口径需保证数据集行数 ≥ 最大请求数（并发上界 × multiplier）。
_sla_tune_bucket() {
    local bucket="$1"
    local ds="datasets/perf_zh_${bucket}.jsonl"
    if [[ ! -f "$ds" ]]; then
        echo "⚠ [$bucket] 数据集不存在: $ds，跳过"
        return 1
    fi
    local sla_params="${SLA_PARAMS:-$(sla_params_for_bucket "$bucket")}"
    if [[ -z "$sla_params" ]]; then
        echo "[fail] 未知 bucket: $bucket（且未设置 SLA_PARAMS 覆盖）"
        return 1
    fi
    # evalscope Arguments.sla_params 要求数组：非 [ 开头的 dict 形式自动包一层
    if [[ "$sla_params" != \[* ]]; then
        sla_params="[$sla_params]"
    fi
    [[ -z "$API_KEY" ]] && { echo "[fail] API_KEY 未设置，请编辑 configs/.env"; return 1; }
    [[ -z "$API_URL" ]] && { echo "[fail] API_URL 未设置，请编辑 configs/.env"; return 1; }
    [[ -z "$MODEL_NAME" ]] && { echo "[fail] MODEL_NAME 未设置，请编辑 configs/.env"; return 1; }
    if [[ "${NONCE:-1}" == "1" ]]; then
        ds="$(inject_nonce "$ds")"
    fi

    local range=($(bucket_range "$bucket"))
    local minlen=${range[0]} maxlen=${range[1]}
    local lower="${SLA_LOWER_BOUND:-1}" upper="${SLA_UPPER_BOUND:-256}"
    local num_runs="${SLA_NUM_RUNS:-2}"
    local multiplier="${SLA_NUMBER_MULTIPLIER:-8}"
    local start_num=$(( lower * multiplier ))
    local ts=$(date +%Y%m%d-%H%M%S)
    local out="results/sla-tune-${bucket}-${MODEL_SLUG}-${ts}"
    mkdir -p "$out"

    echo "[sla-tune] model=$MODEL_NAME bucket=$bucket"
    echo "            SLA: $sla_params"
    echo "            parallel∈[$lower,$upper] num_runs=$num_runs multiplier=$multiplier (每点请求数=并发×$multiplier)"
    echo "            out=$out"

    local cmd=(
        evalscope perf
        --model "$MODEL_NAME" --api "$PROTOCOL"
        --tokenizer-path "$TOKENIZER"
        --url "$API_URL" --api-key "$API_KEY"
        --temperature "$TEMPERATURE"
        --parallel "$lower" --number "$start_num"
        --dataset custom_multi_turn
        --dataset-path "$ds"
        --min-prompt-length "$minlen" --max-prompt-length "$maxlen"
        --max-tokens "${MAX_TOKENS:-4096}" --multi-turn --max-turns 1
        --connect-timeout 60 --read-timeout 600 --db-commit-interval 5
        --outputs-dir "$out" --no-timestamp --no-test-connection
        --sla-auto-tune --sla-variable parallel
        --sla-lower-bound "$lower" --sla-upper-bound "$upper"
        --sla-num-runs "$num_runs"
        --sla-number-multiplier "$multiplier"
        --sla-params "$sla_params"
    )
    if [[ "${DRY_RUN:-0}" == "1" ]]; then
        echo "[dry-run] ${cmd[*]}"
        return 0
    fi

    stdbuf -oL -eL "${cmd[@]}" 2>&1 | tee "$out/run.log"
    if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
        echo "[fail] evalscope auto-tune 执行失败（详见 $out/run.log），跳过复判报告"
        return 1
    fi

    # 复判报告（容错：失败不阻断，给出手动命令）
    echo "[report] 生成 SLA 复判报告 ..."
    python3 scripts/sla_eval.py --evalscope-group-dir "$out" \
        2>&1 | tail -3 || echo "[warn] SLA 复判失败，可手动运行: python3 scripts/sla_eval.py --evalscope-group-dir $out"
    python3 scripts/gen_perf_dashboard.py \
        --run-dir "$out" \
        --title "${MODEL_NAME} SLA 并发上限探索 (${bucket})" \
        --model "$MODEL_NAME" \
        --out "$out/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_sla-tune_${ts}.html" \
        2>&1 | tail -3 || echo "[warn] 报告生成失败，可手动运行: python3 scripts/gen_perf_dashboard.py --run-dir $out"
    echo "[done] SLA 探索结果目录: $KIT_DIR/$out"
}

cmd_sla_tune() {
    ensure_perf_deps
    auto_detect_tokenizer
    ensure_tokenizer_cached
    _sla_tune_bucket "$1"
}

cmd_sla_tune_all() {
    ensure_perf_deps
    auto_detect_tokenizer
    ensure_tokenizer_cached

    local buckets="${BUCKETS:-1k 9k 16k 32k 64k 128k 200k}"
    local cooldown="${SLA_COOLDOWN:-120}"
    local -a bucket_arr=($buckets)
    local total=${#bucket_arr[@]} idx=0

    local ts; ts=$(date +%Y%m%d-%H%M%S)
    local group_dir="results/_sla_group_${ts}"
    mkdir -p "$group_dir"
    local group_log="$group_dir/group.log"
    tlog() { echo -e "$*" | tee -a "$group_log"; }

    tlog "========================================================="
    tlog "  SLA 并发上限探索（全档位）"
    tlog "  Model    : $MODEL_NAME"
    tlog "  Buckets  : $buckets"
    tlog "  搜索范围 : [${SLA_LOWER_BOUND:-1}, ${SLA_UPPER_BOUND:-256}]"
    tlog "  Group dir: $group_dir"
    tlog "========================================================="

    local -a ok_tags=()
    for label in "${bucket_arr[@]}"; do
        idx=$((idx+1))
        tlog ""
        tlog "========== [$idx/$total] bucket=$label =========="
        _sla_tune_bucket "$label" 2>&1 | tee -a "$group_log"
        if [[ ${PIPESTATUS[0]} -eq 0 ]]; then
            # 关联该 bucket 最新一次 sla-tune 结果目录到组目录（报告可统一扫描）
            local latest
            latest=$(ls -dt results/sla-tune-${label}-${MODEL_SLUG}-* 2>/dev/null | grep -v '\.bak' | head -1)
            if [[ -n "$latest" ]]; then
                ln -sfn "../$(basename "$latest")" "$group_dir/$(basename "$latest")"
                ok_tags+=("$(basename "$latest")")
            fi
        else
            tlog "⚠ [$label] SLA 探索失败，继续下一档"
        fi
        if [[ $idx -lt $total && $cooldown -gt 0 && "${DRY_RUN:-0}" != "1" ]]; then
            tlog "⏸  bucket cooldown ${cooldown}s before next bucket ..."
            sleep "$cooldown"
        fi
    done

    tlog ""
    tlog "[report] 生成全档位 SLA 汇总报告 ..."
    python3 scripts/sla_eval.py --evalscope-group-dir "$group_dir" \
        2>&1 | tee -a "$group_log" || tlog "[warn] SLA 汇总复判失败，可手动运行: python3 scripts/sla_eval.py --evalscope-group-dir $group_dir"
    python3 scripts/gen_perf_dashboard.py \
        --run-dir "$group_dir" \
        --title "${MODEL_NAME} SLA 并发上限探索 (全档位 ${ts})" \
        --model "$MODEL_NAME" \
        --out "$group_dir/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_sla-tune_${ts}.html" \
        2>&1 | tee -a "$group_log" || tlog "[warn] 汇总报告失败，可手动运行: python3 scripts/gen_perf_dashboard.py --run-dir $group_dir"
    # Excel 指标报告（--filter 支持逗号分隔，精确限定本轮各档 run-tag，避免混入历史/.bak 数据）
    if [[ ${#ok_tags[@]} -gt 0 && "${DRY_RUN:-0}" != "1" ]]; then
        local sla_filter
        sla_filter=$(IFS=,; echo "${ok_tags[*]}")
        tlog "[report] 生成 Excel 指标报告 ..."
        python3 scripts/gen_report_from_db.py --results-dir results \
            --filter "$sla_filter" \
            --vendor "${PROVIDER:-unknown}" --model "$MODEL_NAME" \
            --no-tpm-html \
            --out "$group_dir/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_sla-tune_${ts}.xlsx" \
            2>&1 | tee -a "$group_log" || tlog "[warn] Excel 报告生成失败，可手动运行: python3 scripts/gen_report_from_db.py --results-dir results --filter '$sla_filter'"
    fi
    tlog ""
    tlog "📄 报告(HTML): $group_dir/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_sla-tune_${ts}.html"
    tlog "📊 报告(Excel): $group_dir/性能测试报告_${PROVIDER:-unknown}_${MODEL_NAME}_sla-tune_${ts}.xlsx"
    tlog "📊 SLA 复判: $group_dir/sla_evaluation.json（各档子目录内亦有单档复判结果）"
}

main() {
    local sub="${1:-help}"
    shift || true
    case "$sub" in
        check)        cmd_check ;;
        bench)        cmd_bench "$@" ;;
        bench-all)    cmd_bench_all "$@" ;;
        sla-tune)     cmd_sla_tune "$@" ;;
        sla-tune-all) cmd_sla_tune_all "$@" ;;
        report)       cmd_report "$@" ;;
        *) sed -n '2,34p' "$0" | grep -E '^# '; exit 0 ;;
    esac
}

main "$@"
