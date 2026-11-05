#!/usr/bin/env bash
# ============================================================
# 大模型性能压测（OpenAI 协议通用）一键启动脚本
# ============================================================
# 用法：
#   bash run.sh check                        # 环境检查
#   bash run.sh bench <bucket> [N] [P]       # 跑单档压测
#   bash run.sh bench-all                    # 全档位 × 并发梯度压测
#   bash run.sh report                       # 从 results/ 生成报告
#
# <bucket> = 1k | 9k | 16k | 32k | 64k | 128k | 200k
# N        = 请求总数，默认 500
# P        = 并发数，默认 10
#
# bench-all 环境变量：
#   BUCKETS             档位列表，默认 "1k 9k 16k 32k 64k 128k 200k 512k 1m"
#   CONCURRENCY_LADDER  并发梯度，默认 "1 8 32 64 128"。显式设置后不再追加档位专用并发。
#                       未设置时 200k 会自动补一个 16 并发点（bucket_extra_parallels）。
#   TPM_BUDGET          TPM 预算（tokens/min），默认 0 = 不限制。按「单条 token × 并发」裁剪并发阶梯，
#                       请求数也不超过该档可用并发数；某档连并发 1 都超预算则整档跳过。
#                       注意：该折算假设每条请求耗时 60 秒，实测更快时真实 TPM 会是数倍，
#                       不能当硬限流；要严格限速请直接用 evalscope 的 --rate。
#   BUCKET_COOLDOWN     档间冷却秒数，默认 120
#   N_<label>           覆盖特定档位的请求数（如 N_1k=20 N_9k=100），优先于 TARGET_DURATION
#   WARMUP_SECS         压测前预热秒数，默认 300（性能验收标准：低并发预热 5min）；0 = 不预热
#   WARMUP_BUCKET       预热用档位，默认 1k
#   WARMUP_PARALLEL     预热并发，默认 4。预热产物写到 results_warmup/，不计入报告
#   TARGET_DURATION     单个并发梯度的目标时长（秒），默认 600（标准要求 10–15min）。
#                       用本档上一梯度实测 Avg Latency 估算请求数，不少于默认请求数；
#                       本档第一个梯度沿用默认请求数。0 = 关闭，回到固定请求数
#   SUCCESS_RATE_MIN    成功率早停阈值（百分比，默认 0=关闭）；
#                       某档并发跑完后成功率低于此值时，跳过该 bucket 剩余更高并发，
#                       直接切到下一个 bucket。设为 0 可关闭早停。
#   DATASET_TOKENIZER   超长档位（512k/1m）计数/过滤用的分词器，默认 zai-org/GLM-4.6
#                       （与数据集构建一致）。仅影响 512k/1m，其它档位仍按 TOKENIZER/MODEL_NAME。
#                       未设置时若显式设置了 TOKENIZER，则这两档沿用该 TOKENIZER。
#
# 配置：cp configs/env.example configs/.env 后填写（支持任意 OpenAI 兼容端点）
# ============================================================
set -eu

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$KIT_DIR"

# 注入 sitecustomize 兼容补丁：修复 transformers 5.x 下 apply_chat_template 返回
# BatchEncoding 导致的 evalscope token 计数错误（produced no conversations）
export PYTHONPATH="$KIT_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}"

# .env 在项目根目录 configs/ 下（perf 的上一级）
ROOT_DIR="$(cd "$KIT_DIR/.." && pwd)"

# ---- 加载 .env ----
load_env() {
    if [[ -f "$ROOT_DIR/configs/.env" ]]; then
        set -a; source "$ROOT_DIR/configs/.env"; set +a
    elif [[ -f configs/.env ]]; then
        set -a; source configs/.env; set +a
    else
        echo "[warn] configs/.env 不存在，使用环境变量或默认值" >&2
    fi
}

load_env

# 数据集目录：默认优先 datasets_clean/（已脱敏，避免供应商内容安全拦截污染成功率），
# 不存在时回落到 datasets/（原始真实语料，含敏感内容）。
# 可用 DATASET_DIR=<目录> 显式指定。
if [[ -d "datasets_clean" ]]; then
    _DEFAULT_DATASET_DIR="datasets_clean"
else
    _DEFAULT_DATASET_DIR="datasets"
fi
DATASET_DIR="${DATASET_DIR:-$_DEFAULT_DATASET_DIR}"

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

# 超长档位（512k/1m）数据集是用 GLM-4.6 分词器构建并按固定区间（见 bucket_range）过滤的。
# 若压测时按被测模型自动换成别家分词器（如 DeepSeek-V3，对中文计数会少数个百分点），
# 样本会整体落到区间下限以下，被全部过滤，报 "produced no conversations"。
# 因此这些档位的 token 计数/过滤固定使用数据集构建时的分词器，与被测模型无关。
# 优先级：DATASET_TOKENIZER（显式指定）> 用户显式设置的 TOKENIZER > 默认 GLM-4.6。
LONG_BUCKETS="512k 1m"
DATASET_TOKENIZER_DEFAULT="zai-org/GLM-4.6"

bucket_tokenizer() {
    local bucket="$1" b
    for b in $LONG_BUCKETS; do
        if [[ "$bucket" == "$b" ]]; then
            if [[ -n "${DATASET_TOKENIZER:-}" ]]; then
                echo "$DATASET_TOKENIZER"
            elif [[ -n "$_TOKENIZER_USER" ]]; then
                echo "$TOKENIZER"
            else
                echo "$DATASET_TOKENIZER_DEFAULT"
            fi
            return 0
        fi
    done
    echo "$TOKENIZER"
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
        python3 -m pip install -r requirements.txt -q --root-user-action=ignore || {
            python3 -m pip install -r requirements.txt -q --force-reinstall --root-user-action=ignore || {
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
    # 可选参数 $1：指定要检查的 tokenizer；缺省为全局 $TOKENIZER
    local tok="${1:-$TOKENIZER}"
    [[ -z "$tok" ]] && return 0
    # 用 local_files_only=True 探测；命中则直接返回，未命中则触发下载
    python3 - "$tok" "$KIT_DIR/scripts/setup_tokenizer.py" <<'PY'
import sys, os, subprocess
tok_name = sys.argv[1]
setup_script = sys.argv[2]
try:
    from transformers import AutoTokenizer
    AutoTokenizer.from_pretrained(tok_name, trust_remote_code=True, local_files_only=True)
    print(f"  [tokenizer] 已缓存: {tok_name}")
except Exception:
    print(f"  [tokenizer] 未缓存，开始下载 {tok_name} ...")
    # 调用 setup_tokenizer.py 完成下载 + 注入 chat_template（显式传入目标 tokenizer 名）
    ret = subprocess.call(["python3", setup_script], env={**os.environ, "TOKENIZER": tok_name})
    if ret != 0:
        print(f"  [tokenizer] 下载失败，请检查网络或手动执行: python3 {setup_script}", file=sys.stderr)
        sys.exit(1)
    print(f"  [tokenizer] 下载完成: {tok_name}")
PY
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
        512k) echo "500000 540000" ;;
        1m)   echo "980000 1030000" ;;
        *) echo "0 999999" ;;
    esac
}

# ---- 档位单条 token 估算（取区间上界，保守）----
bucket_tokens() {
    local r=($(bucket_range "$1"))
    echo "${r[1]}"
}

# ---- 档位并发上限（按实测上下文/TPM 能力约定，超出的并发阶梯不跑）----
# 说明：32k 及以下沿用全局梯度的 128 上限；超长档位按实测能力收敛。
# 注意：下列档位在实测中 TPM 会超过 1000 万（64k×64 约 1251 万、128k×32 约 1317 万、
# 200k×16 约 1300 万），如被测网关有硬性 TPM 配额，高并发点会返回 429。
bucket_max_parallel() {
    case "$1" in
        1k|9k|16k|32k) echo 128 ;;
        64k)  echo 64 ;;   # 128 并发删掉
        128k) echo 32 ;;   # 64/128 并发删掉
        200k) echo 16 ;;   # 32/64/128 并发删掉
        512k) echo 8 ;;    # 32/64/128 并发删掉
        1m)   echo 8 ;;    # 32/64/128 并发删掉
        # 未知档位返回失败，由调用方跳过，避免拼错档位名时静默按 128 并发去跑
        *)    return 1 ;;
    esac
}

# ---- 档位补充并发（不在全局梯度里、但某档位需要单独补的并发点）----
# 200k 在 8 与 32 之间补 16：观察中长上下文的中段拐点。
# 仅在未显式设置 CONCURRENCY_LADDER 时生效；显式指定梯度时以用户给定的为准。
bucket_extra_parallels() {
    case "$1" in
        200k) echo 16 ;;
        *)    echo "" ;;
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
    echo "[check] datasets... (DATASET_DIR=$DATASET_DIR)"
    for f in 1k 9k 16k 32k 64k 128k 200k 512k 1m; do
        if [[ -f "${DATASET_DIR}/perf_zh_${f}.jsonl" ]]; then
            sz=$(du -h "${DATASET_DIR}/perf_zh_${f}.jsonl" | cut -f1)
            echo "  OK: perf_zh_${f}.jsonl ($sz)"
        else
            echo "  [warn] 缺失: ${DATASET_DIR}/perf_zh_${f}.jsonl"
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

# ---- evalscope perf 公共参数 → 全局数组 EVALSCOPE_ARGS ----
# 用法: build_evalscope_args <dataset> <tokenizer> <minlen> <maxlen> <parallel> <number> <outdir>
# 调用方: evalscope perf "${EVALSCOPE_ARGS[@]}"（bench / bench-all / 预热共用，保证参数口径一致）
build_evalscope_args() {
    EVALSCOPE_ARGS=(
        --model "$MODEL_NAME" --api "$PROTOCOL"
        --tokenizer-path "$2"
        --url "$API_URL" --api-key "$API_KEY"
        --temperature "$TEMPERATURE"
        --parallel "$5" --number "$6"
        --dataset custom_multi_turn
        --dataset-path "$1"
        --min-prompt-length "$3" --max-prompt-length "$4"
        --max-tokens 4096 --multi-turn --max-turns 1
        --connect-timeout 60 --read-timeout 600 --db-commit-interval 5
        --outputs-dir "$7" --no-timestamp --no-test-connection
    )
}

cmd_bench() {
    ensure_perf_deps
    auto_detect_tokenizer
    ensure_tokenizer_cached
    local bucket="$1"
    local n="${2:-500}"
    local p="${3:-10}"
    local ds="${DATASET_DIR}/perf_zh_${bucket}.jsonl"
    [[ ! -f "$ds" ]] && { echo "数据集不存在: $ds"; exit 1; }
    [[ -z "$API_KEY" ]] && { echo "[fail] API_KEY 未设置，请编辑 configs/.env"; exit 1; }
    [[ -z "$API_URL" ]] && { echo "[fail] API_URL 未设置，请编辑 configs/.env"; exit 1; }
    [[ -z "$MODEL_NAME" ]] && { echo "[fail] MODEL_NAME 未设置，请编辑 configs/.env"; exit 1; }

    local range=($(bucket_range "$bucket"))
    local minlen=${range[0]} maxlen=${range[1]}
    local tok; tok=$(bucket_tokenizer "$bucket")
    if [[ "$tok" != "$TOKENIZER" ]]; then
        echo "[info] 档位 $bucket 使用数据集构建分词器: $tok（模型分词器: $TOKENIZER）" >&2
        ensure_tokenizer_cached "$tok"
    fi
    local ts=$(date +%Y%m%d-%H%M)
    local out="results/perf-${bucket}-${ts}"
    mkdir -p "$out"

    echo "[bench] model=$MODEL_NAME bucket=$bucket parallel=$p number=$n"
    echo "        url=$API_URL"
    echo "        tokenizer=$tok"
    echo "        out=$out"
    echo ""

    build_evalscope_args "$ds" "$tok" "$minlen" "$maxlen" "$p" "$n" "$out"
    evalscope perf "${EVALSCOPE_ARGS[@]}" 2>&1 | tee "$out/run.log"

    echo ""
    echo "[done] 结果目录: $KIT_DIR/$out"

    # 报告出口（跑完才会到这里；Ctrl+C 中断会直接终止脚本，不生成报告）：
    #   性能测试报告.html = 阅读版：总体结论 + 并发梯度矩阵 + 失败原因聚合 + 失败请求逐条明细
    #   性能测试报告.xlsx = 指标交付版：「性能指标」45 列聚合指标 + 「失败请求详情」sheet（14 列）
    echo "[report] 生成性能测试报告 ..."
    python3 scripts/gen_perf_dashboard.py \
        --run-dir "$out" \
        --title "${MODEL_NAME} 性能压测报告 (${bucket})" \
        --model "$MODEL_NAME" \
        --out "$out/性能测试报告.html" \
        2>&1 | tail -3 || echo "[warn] 报告生成失败，可手动运行: python3 scripts/gen_perf_dashboard.py --run-dir $out"
    echo "[report] 生成 Excel 指标报告 ..."
    python3 scripts/gen_report_from_db.py --results-dir results \
        --filter "perf-${bucket}-${ts}" \
        ${CLIENT:+--client "$CLIENT"} \
        ${API_URL:+--api-url "$API_URL"} \
        --no-tpm-html \
        --out "$out/性能测试报告.xlsx" \
        2>&1 | tail -3 || echo "[warn] Excel 报告生成失败，可手动运行: python3 scripts/gen_report_from_db.py --results-dir results --filter perf-${bucket}-${ts}"
    echo "[report] 📄 报告(HTML): $out/性能测试报告.html"
    echo "[report] 📊 报告(Excel): $out/性能测试报告.xlsx"
}

cmd_report() {
    # 手动补报告：参数直接透传给 gen_perf_dashboard.py
    # 例: bash run.sh report --run-dir results/perf-1k-20260831-2143 --model glm-5.3-flash
    python3 scripts/gen_perf_dashboard.py "$@"
}

# ---- 并发梯度 → 默认请求数 ----
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

# ---- 从 benchmark_summary.json 提取单请求平均耗时 Avg Latency (s)；取不到输出空串 ----
extract_avg_latency() {
    python3 -c "
import json, sys
try:
    v = float(json.load(open(sys.argv[1])).get('Avg Latency (s)') or 0)
    print(f'{v:.3f}' if v > 0 else '')
except Exception:
    print('')
" "$1" 2>/dev/null || true
}

# ---- 按目标时长估算请求数：ceil(并发 × 目标秒数 / 单请求耗时)，且不少于 floor ----
# 用法: duration_number <parallel> <target_secs> <latency_secs> <floor>
duration_number() {
    awk -v p="$1" -v t="$2" -v l="$3" -v f="$4" \
        'BEGIN { x = p * t / l; n = int(x); if (n < x) n++; if (n < f) n = f; print n }'
}

# ---- 预热：低并发持续发请求 secs 秒，排除冷启动影响 ----
# 产物写到 results_warmup/（不在 results/ 下：gen_report_from_db 会 rglob 整个 results/，
# 放进去会混入报告）。每轮 number = 2×并发，轮次之间检查是否已满 secs 秒。
# 某轮没产出 benchmark_summary.json（鉴权失败、端点不通等）立即停止预热，避免空转。
# 用法: run_warmup <secs> <bucket> <parallel> <logfile>
run_warmup() {
    local secs="$1" bucket="$2" par="$3" log="$4"
    local ds="${DATASET_DIR}/perf_zh_${bucket}.jsonl"
    if [[ ! -f "$ds" ]]; then
        echo "⚠ [warmup] 数据集不存在: $ds，跳过预热" | tee -a "$log"
        return 0
    fi
    local range=($(bucket_range "$bucket"))
    local tok; tok=$(bucket_tokenizer "$bucket")
    if [[ "$tok" != "$TOKENIZER" ]]; then
        ensure_tokenizer_cached "$tok"
    fi
    local wdir="results_warmup/warmup-$(date +%Y%m%d-%H%M%S)"
    local start=$SECONDS round=0
    echo "🔥 [warmup] bucket=$bucket parallel=$par 持续 ${secs}s（产物: $wdir，不计入报告）" | tee -a "$log"
    while (( SECONDS - start < secs )); do
        round=$((round+1))
        local out="$wdir/round_${round}"
        mkdir -p "$out"
        build_evalscope_args "$ds" "$tok" "${range[0]}" "${range[1]}" "$par" $((par*2)) "$out"
        evalscope perf "${EVALSCOPE_ARGS[@]}" > "$out/run.log" 2>&1 || true
        local summary
        summary=$(find "$out" -maxdepth 4 -name benchmark_summary.json 2>/dev/null | head -1)
        if [[ -z "$summary" ]]; then
            echo "⚠ [warmup] round $round 未产出 benchmark_summary.json（见 $out/run.log），停止预热" | tee -a "$log"
            return 0
        fi
        echo "🔥 [warmup] round $round 完成，成功率=$(extract_success_rate "$summary")%，已预热 $((SECONDS-start))s / ${secs}s" | tee -a "$log"
    done
    echo "✅ [warmup] 预热完成，共 $round 轮、$((SECONDS-start))s" | tee -a "$log"
}

# ---- 全档位压测（梯度递进 + 冷却）----
# 说明：只在全部 bucket 正常跑完后自动生成报告。
# 若中途 Ctrl+C 中断或异常失败，请手动运行: bash run.sh report --client "供应商名称"
cmd_bench_all() {
    ensure_perf_deps
    auto_detect_tokenizer
    ensure_tokenizer_cached

    local buckets="${BUCKETS:-1k 9k 16k 32k 64k 128k 200k 512k 1m}"
    # 显式设置 CONCURRENCY_LADDER 时以用户梯度为准，不再追加档位专用并发。
    local ladder_user="${CONCURRENCY_LADDER:-}"
    local conc_ladder="${ladder_user:-1 8 32 64 128}"
    # TPM 预算（tokens/min）：0 = 不限制（默认）。
    # 注意：该预算按「单条 token × 并发」折算并发上限，等价于假设每条请求耗时 60 秒；
    # 实测单条远快于 60 秒时，真实 TPM 会是预览值的 60/耗时 倍，不能当作硬限流使用。
    # 需要严格限流请直接用 evalscope 的 --rate（每秒请求数）。
    # 按被测网关实际 TPM 设置，例如 TokenHub minimax-m3.1-flash 为 100 万。
    local tpm_budget="${TPM_BUDGET:-0}"
    local cooldown="${BUCKET_COOLDOWN:-120}"
    # 成功率早停阈值（百分比，默认 0=关闭；设为非 0 值如 99.9 开启）
    # 参考 benjaminswu-perf 分支：某档并发跑完后成功率低于阈值 → 跳过该 bucket 剩余并发
    local sr_min="${SUCCESS_RATE_MIN:-0}"
    # 性能验收标准：低并发预热 5min，每个并发梯度压 10–15min
    local warmup_secs="${WARMUP_SECS:-300}"
    local warmup_bucket="${WARMUP_BUCKET:-1k}"
    local warmup_par="${WARMUP_PARALLEL:-4}"
    local target_dur="${TARGET_DURATION:-600}"

    local -a bucket_arr=($buckets)
    local -a conc_arr=($conc_ladder)
    local total=${#bucket_arr[@]} idx=0
    local n_done=0 n_fail=0
    local -a group_runs=()   # 本次跑出的 run 目录名，用于 Excel 汇总时排除历史结果

    local ts; ts=$(date +%Y%m%d-%H%M)
    local group_dir="results/_group_${ts}"
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
    tlog "  预热     : ${warmup_secs}s，bucket=${warmup_bucket} 并发=${warmup_par} (WARMUP_SECS，设 0 关闭)"
    tlog "  单梯度目标时长: ${target_dur}s (TARGET_DURATION，设 0 = 固定请求数)"
    tlog "  Group dir: $group_dir"
    tlog "========================================================="

    if [[ "$warmup_secs" -gt 0 ]]; then
        run_warmup "$warmup_secs" "$warmup_bucket" "$warmup_par" "$group_log"
    fi

    for label in "${bucket_arr[@]}"; do
        idx=$((idx+1))
        local ds="${DATASET_DIR}/perf_zh_${label}.jsonl"
        if [[ ! -f "$ds" ]]; then
            tlog "⚠ [$label] 数据集不存在: $ds，跳过"
            continue
        fi

        # 并发上限 = 档位能力约定（bucket_max_parallel），若启用 TPM 预算则取更小者
        local once_tokens=$(bucket_tokens "$label")
        local max_par
        if ! max_par=$(bucket_max_parallel "$label"); then
            tlog "❌ [$label] 未知档位，可用档位: 1k 9k 16k 32k 64k 128k 200k 512k 1m，跳过"
            continue
        fi
        local tpm_par=999999
        if [[ "$tpm_budget" -gt 0 ]]; then
            tpm_par=$(( tpm_budget / once_tokens ))
            [[ $tpm_par -lt $max_par ]] && max_par=$tpm_par
        fi
        local -a cur_conc=()
        for p in "${conc_arr[@]}"; do
            [[ $p -le $max_par ]] && cur_conc+=("$p")
        done
        # 未显式指定梯度时，补该档位专用并发（如 200k 的 16）
        if [[ -z "$ladder_user" ]]; then
            local extra
            for extra in $(bucket_extra_parallels "$label"); do
                [[ $extra -le $max_par ]] && cur_conc+=("$extra")
            done
        fi
        # 无论梯度来自默认值还是用户输入，都去重升序，保证并发从小到大依次压测
        if [[ ${#cur_conc[@]} -gt 1 ]]; then
            mapfile -t cur_conc < <(printf '%s\n' "${cur_conc[@]}" | sort -n -u)
        fi
        if [[ ${#cur_conc[@]} -eq 0 ]]; then
            tlog "⏭ [$label] 并发上限 ${max_par}（单条 ${once_tokens} token，TPM 预算 ${tpm_budget}/min）低于梯度最小值，跳过该档位"
            continue
        fi

        tlog ""
        tlog "========== [$idx/$total] bucket=$label  并发梯度: ${cur_conc[*]}（单条 ${once_tokens} token，并发上限 ${max_par}）(早停阈值: 成功率<${sr_min}%) =========="

        local tok; tok=$(bucket_tokenizer "$label")
        if [[ "$tok" != "$TOKENIZER" ]]; then
            tlog "[info] 档位 $label 使用数据集构建分词器: $tok（模型分词器: $TOKENIZER）"
            ensure_tokenizer_cached "$tok"
        fi

        local bucket_stopped=0
        local prev_lat=""   # 本档上一并发梯度实测 Avg Latency，用于估算下一梯度请求数
        for p in "${cur_conc[@]}"; do
            local n_var="N_${label}"
            local n="${!n_var:-$(parallel_number "$p")}"
            # 按目标时长估算请求数（显式 N_<label> 优先）：用本档上一梯度的单请求耗时，
            # 请求数 = ceil(并发 × TARGET_DURATION / 耗时)，不少于默认请求数。
            # 本档第一个梯度没有实测值，沿用默认请求数（并发 1 × 20 条，实测约 6~20min）。
            # 并发升高后单请求耗时通常变长，所以实际时长一般会比目标略长。
            if [[ -z "${!n_var:-}" && "$target_dur" -gt 0 && -n "$prev_lat" ]]; then
                n=$(duration_number "$p" "$target_dur" "$prev_lat" "$n")
            fi
            # 仅在启用 TPM 预算时，请求数才按单分钟可发条数收敛（显式 N_<label> 优先）
            if [[ -z "${!n_var:-}" && "$tpm_budget" -gt 0 && $n -gt $tpm_par ]]; then
                n=$tpm_par
            fi

            tlog "---------- [bucket=$label parallel=$p number=$n] ----------"

            local range=($(bucket_range "$label"))
            local minlen=${range[0]} maxlen=${range[1]}
            local run_ts=$(date +%Y%m%d-%H%M)
            local out="results/perf-${label}-${run_ts}"
            mkdir -p "$out"

            tlog "[bench] model=$MODEL_NAME bucket=$label parallel=$p number=$n"
            tlog "        out=$out"

            # stdbuf 让 evalscope 输出无缓冲，tee 同时写日志和控制台（QCI 可实时看到进度）
            build_evalscope_args "$ds" "$tok" "$minlen" "$maxlen" "$p" "$n" "$out"
            stdbuf -oL -eL evalscope perf "${EVALSCOPE_ARGS[@]}" 2>&1 | tee "$out/run.log" &
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

            # 软链必须用相对 group 目录的路径：$out 是 results/perf-xxx，
            # 写成 "$out" 会被解析成 results/_group_xxx/results/perf-xxx（不存在），
            # 导致组目录下扫不到 benchmark_data.db、汇总 HTML 生成失败。
            ln -sfn "../$(basename "$out")" "$group_dir/$(basename "$out")"
            group_runs+=("$(basename "$out")")

            # 跑完后从 benchmark_summary.json 提取成功率，判断是否早停
            local summary
            summary=$(find "$out" -maxdepth 4 -name benchmark_summary.json 2>/dev/null | head -1)
            if [[ -n "$summary" ]]; then
                local sr
                sr=$(extract_success_rate "$summary")
                local lat; lat=$(extract_avg_latency "$summary")
                [[ -n "$lat" ]] && prev_lat="$lat"
                tlog "✅ [$label/p=$p] done → 成功率=${sr}%${lat:+，Avg Latency=${lat}s}"
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
        --out "$group_dir/性能测试报告.html" \
        2>&1 | tee -a "$group_log" || tlog "[warn] 报告生成失败，可手动运行: bash run.sh report --run-dir $group_dir"
    tlog "[report] 生成 Excel 指标报告 ..."
    # 只统计本次跑出的 run：否则 results/ 下的历史构建会被一起算进交付表格
    local -a xlsx_filter=()
    if [[ ${#group_runs[@]} -gt 0 ]]; then
        xlsx_filter=(--filter "${group_runs[@]}")
    fi
    python3 scripts/gen_report_from_db.py --results-dir results \
        ${xlsx_filter[@]+"${xlsx_filter[@]}"} \
        ${CLIENT:+--client "$CLIENT"} \
        ${API_URL:+--api-url "$API_URL"} \
        --no-tpm-html \
        --out "$group_dir/性能测试报告.xlsx" \
        2>&1 | tee -a "$group_log" || tlog "[warn] Excel 报告生成失败，可手动运行: bash run.sh report"

    tlog ""
    if [[ -f "$group_dir/性能测试报告.html" ]]; then
        tlog "📄 报告(HTML): $group_dir/性能测试报告.html"
    else
        tlog "⚠ 报告(HTML) 未生成，原因见 $group_log（可用 bash run.sh report --run-dir $group_dir 补）"
    fi
    if [[ -f "$group_dir/性能测试报告.xlsx" ]]; then
        tlog "📊 报告(Excel): $group_dir/性能测试报告.xlsx"
    else
        tlog "⚠ 报告(Excel) 未生成，原因见 $group_log"
    fi
}

main() {
    local sub="${1:-help}"
    shift || true
    case "$sub" in
        check)     cmd_check ;;
        bench)     cmd_bench "$@" ;;
        bench-all) cmd_bench_all "$@" ;;
        report)    cmd_report "$@" ;;
        *) sed -n '2,25p' "$0" | grep -E '^# '; exit 0 ;;
    esac
}

main "$@"
