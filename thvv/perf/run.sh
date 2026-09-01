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
#   BUCKETS             档位列表，默认 "1k 9k 16k 32k 64k 128k 200k"
#   CONCURRENCY_LADDER  并发梯度，默认 "1 8 32 64 128"
#   BUCKET_COOLDOWN     档间冷却秒数，默认 120
#   N_<label>           覆盖特定档位的请求数（如 N_1k=20 N_9k=100）
#   SUCCESS_RATE_MIN    成功率早停阈值（百分比，默认 0=关闭）；
#                       某档并发跑完后成功率低于此值时，跳过该 bucket 剩余更高并发，
#                       直接切到下一个 bucket。设为 0 可关闭早停。
#
# 配置：cp configs/env.example configs/.env 后填写（支持任意 OpenAI 兼容端点）
# ============================================================
set -eu

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$KIT_DIR"

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
    [[ -z "$API_KEY" ]] && { echo "[fail] API_KEY 未设置，请编辑 configs/.env"; exit 1; }
    [[ -z "$API_URL" ]] && { echo "[fail] API_URL 未设置，请编辑 configs/.env"; exit 1; }
    [[ -z "$MODEL_NAME" ]] && { echo "[fail] MODEL_NAME 未设置，请编辑 configs/.env"; exit 1; }

    local range=($(bucket_range "$bucket"))
    local minlen=${range[0]} maxlen=${range[1]}
    local ts=$(date +%Y%m%d-%H%M)
    local out="results/perf-${bucket}-${ts}"
    mkdir -p "$out"

    echo "[bench] model=$MODEL_NAME bucket=$bucket parallel=$p number=$n"
    echo "        url=$API_URL"
    echo "        tokenizer=$TOKENIZER"
    echo "        out=$out"
    echo ""

    evalscope perf \
        --model "$MODEL_NAME" --api "$PROTOCOL" \
        --tokenizer-path "$TOKENIZER" \
        --url "$API_URL" --api-key "$API_KEY" \
        --temperature "$TEMPERATURE" \
        --parallel "$p" --number "$n" \
        --dataset custom_multi_turn \
        --dataset-path "$ds" \
        --min-prompt-length "$minlen" --max-prompt-length "$maxlen" \
        --max-tokens 4096 --multi-turn --max-turns 1 \
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
        --out "$out/性能测试报告.html" \
        2>&1 | tail -3 || echo "[warn] 报告生成失败，可手动运行: python3 scripts/gen_perf_dashboard.py --run-dir $out"
    echo "[report] 生成 Excel 指标报告 ..."
    python3 scripts/gen_report_from_db.py --results-dir results \
        --filter "perf-${bucket}-${ts}" \
        ${CLIENT:+--client "$CLIENT"} \
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
        4)   echo 50  ;;
        8)   echo 100 ;;
        16)  echo 150 ;;
        32)  echo 250 ;;
        64)  echo 400 ;;
        128) echo 500 ;;
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

            local range=($(bucket_range "$label"))
            local minlen=${range[0]} maxlen=${range[1]}
            local run_ts=$(date +%Y%m%d-%H%M)
            local out="results/perf-${label}-${run_ts}"
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
        --dataset-path "$ds" \
        --min-prompt-length "$minlen" --max-prompt-length "$maxlen" \
        --max-tokens 4096 --multi-turn --max-turns 1 \
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

            ln -sfn "$out" "$group_dir/$(basename "$out")"

            # 跑完后从 benchmark_summary.json 提取成功率，判断是否早停
            local summary
            summary=$(find "$out" -maxdepth 4 -name benchmark_summary.json 2>/dev/null | head -1)
            if [[ -n "$summary" ]]; then
                local sr
                sr=$(extract_success_rate "$summary")
                tlog "✅ [$label/p=$p] done → 成功率=${sr}%"
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
    python3 scripts/gen_report_from_db.py --results-dir results \
        ${CLIENT:+--client "$CLIENT"} \
        --no-tpm-html \
        --out "$group_dir/性能测试报告.xlsx" \
        2>&1 | tee -a "$group_log" || tlog "[warn] Excel 报告生成失败，可手动运行: bash run.sh report"

    tlog ""
    tlog "📄 报告(HTML): $group_dir/性能测试报告.html"
    tlog "📊 报告(Excel): $group_dir/性能测试报告.xlsx"
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
