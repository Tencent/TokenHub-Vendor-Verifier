#!/usr/bin/env bash
# ============================================================
# 大模型效果评测一键启动脚本
# ============================================================
# 用法：
#   bash run.sh check                          # 环境检查
#   bash run.sh bench <datasets> [options]     # 跑效果评测
#   bash run.sh list                           # 列出支持的数据集
#
# <datasets> = aime25,gpqa_diamond  或  all
#
# 配置：从上级 configs/.env 读取 API_URL / API_KEY / MODEL_NAME / PROTOCOL
# ============================================================
set -eu

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$KIT_DIR"

ROOT_DIR="$(cd "$KIT_DIR/.." && pwd)"

# ---- 加载 .env ----
load_env() {
    if [[ -f "$ROOT_DIR/configs/.env" ]]; then
        set -a; source "$ROOT_DIR/configs/.env"; set +a
    else
        echo "[warn] configs/.env 不存在，使用环境变量或默认值" >&2
    fi
}
load_env

# 统一变量名
API_URL="${API_URL:-}"
API_KEY="${API_KEY:-}"
MODEL_NAME="${MODEL_NAME:-}"
PROVIDER="${PROVIDER:-unknown}"

# ---- 检测并安装缺失依赖 ----
ensure_eval_deps() {
    local lock_file="/tmp/ai_evaluagtion_eval_pip_install.lock"
    echo "[deps] 按 requirements.txt 固定版本安装效果评测依赖..."
    (
        flock -w 600 9 || { echo "[fail] 等待 pip 安装锁超时"; exit 1; }
        python3 -m pip install -r requirements.txt -q --root-user-action=ignore --no-cache-dir || {
            python3 -m pip install -r requirements.txt -q --force-reinstall --root-user-action=ignore --no-cache-dir || {
                echo "[fail] 依赖安装失败，请手动执行: pip install -r requirements.txt"
                exit 1
            }
        }
    ) 9>"$lock_file"
    echo "[deps] 依赖安装完成"
}

cmd_check() {
    echo "[check] python3..."
    python3 --version
    echo "[check] evalscope..."
    evalscope --version || { echo "[fail] evalscope 未安装：pip install -r requirements.txt"; exit 1; }
    echo "[check] python deps..."
    python3 -c "import yaml" \
        && echo "  OK: PyYAML" \
        || { echo "[fail] 缺少依赖：pip install -r requirements.txt"; exit 1; }
    echo "[check] configs/.env..."
    [[ -f "$ROOT_DIR/configs/.env" ]] && echo "  OK" || echo "  [warn] 不存在"
    echo "[check] API_URL / API_KEY / MODEL_NAME..."
    [[ -n "$API_URL" && -n "$API_KEY" && -n "$MODEL_NAME" ]] \
        && echo "  OK" \
        || echo "  [warn] 缺少 API_URL/API_KEY/MODEL_NAME"
}

cmd_list() {
    python3 scripts/run_eval.py --list-datasets
}

cmd_bench() {
    ensure_eval_deps
    local datasets="${1:-}"
    shift || true

    [[ -z "$datasets" ]] && { echo "用法: bash run.sh bench <datasets> [options]"; echo "  datasets: aime25,gpqa_diamond 或 all"; echo "  bash run.sh list 查看全部"; exit 1; }
    [[ -z "$API_KEY" ]] && { echo "[fail] API_KEY 未设置，请编辑 configs/.env"; exit 1; }
    [[ -z "$API_URL" ]] && { echo "[fail] API_URL 未设置，请编辑 configs/.env"; exit 1; }
    [[ -z "$MODEL_NAME" ]] && { echo "[fail] MODEL_NAME 未设置，请编辑 configs/.env"; exit 1; }

    # API_URL 是完整请求路径，evalscope 需要 base_url（不含 /chat/completions）
    local base_url="$API_URL"
    base_url="${base_url%/chat/completions}"
    base_url="${base_url%/v1/messages}"

    echo "[bench] model=$MODEL_NAME provider=$PROVIDER"
    echo "        base_url=$base_url"
    echo "        datasets=$datasets"
    echo ""

    python3 scripts/run_eval.py \
        --datasets "$datasets" \
        --model_name "$MODEL_NAME" \
        --provider "$PROVIDER" \
        --base_url "$base_url" \
        --api_key "$API_KEY" \
        "$@"
}

main() {
    local sub="${1:-help}"
    shift || true
    case "$sub" in
        check) cmd_check ;;
        bench) cmd_bench "$@" ;;
        list)  cmd_list ;;
        *) sed -n '2,18p' "$0" | grep -E '^# '; exit 0 ;;
    esac
}

main "$@"
