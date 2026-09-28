#!/usr/bin/env bash
# ============================================================
# THVV — 一键启动脚本（perf 性能压测 / eval 效果评测）
# ============================================================
# 用法：
#   bash quickstart.sh check                    # 环境检查
#   bash quickstart.sh install                  # 安装依赖
#   bash quickstart.sh perf bench <bucket> [请求数N] [并发P]  # 单档压测（N 默认 500，P 默认 10）
#   bash quickstart.sh perf bench-all           # 全档位 × 并发梯度压测
#   bash quickstart.sh perf report              # 生成报告
#   bash quickstart.sh eval bench <datasets>    # 效果评测
#   bash quickstart.sh eval list                # 列出支持的数据集
#   bash quickstart.sh perf bench 16k 500 10 --env glm53   # 用 configs/env.d/glm53.env 切换模型
#   bash quickstart.sh eval bench aime26 --model X --url Y --api-key Z   # 端点参数直传（无需 .env）
#
# 协议（在 configs/.env 中设置 PROTOCOL，或命令行 export）：
#   PROTOCOL=openai      → OpenAI Chat Completions
#   PROTOCOL=anthropic   → Anthropic Messages / Claude 系列
# ============================================================
set -euo pipefail

KIT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$KIT_DIR"

# ---- 公共参数解析（--env 命名配置选择 + --url/--model 等端点参数直传）----
source "$KIT_DIR/lib/params.sh"

# ---- 颜色 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ---- 检测并安装缺失依赖 ----
ensure_requirements() {
    local req_file="$1"
    local label="$2"
    if [[ ! -f "$req_file" ]]; then
        echo -e "  ${YELLOW}$req_file 不存在，跳过 $label${NC}"
        return 0
    fi
    local missing=()
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        local pkg
        pkg=$(echo "$line" | sed 's/\[.*\]//; s/[<>=!].*//; s/ //g')
        local import_name="$pkg"
        case "$pkg" in
            evalscope) import_name="evalscope" ;;
            transformers) import_name="transformers" ;;
            modelscope) import_name="modelscope" ;;
            openai) import_name="openai" ;;
            httpx) import_name="httpx" ;;
            numpy) import_name="numpy" ;;
            ijson) import_name="ijson" ;;
            matplotlib) import_name="matplotlib" ;;
        esac
        if ! python3 -c "import $import_name" 2>/dev/null; then
            missing+=("$line")
        fi
    done < "$req_file"
    
    if [[ ${#missing[@]} -eq 0 ]]; then
        echo -e "  ${GREEN}✓ $label 依赖已全部安装${NC}"
    else
        echo -e "  ${YELLOW}→ $label 缺少 ${#missing[@]} 个依赖，正在安装...${NC}"
        python3 -m pip install -r "$req_file" -q || {
            echo -e "  ${RED}✗ $label 依赖安装失败${NC}"
            return 1
        }
        echo -e "  ${GREEN}✓ $label 依赖安装成功${NC}"
    fi
}

# ---- 环境检查 ----
cmd_check() {
    echo -e "${BLUE}=== 环境检查 ===${NC}"
    
    echo -n "  Python3: "
    if command -v python3 &>/dev/null; then
        echo -e "${GREEN}$(python3 --version)${NC}"
    else
        echo -e "${RED}未安装${NC}"
        return 1
    fi
    
    echo -n "  pip: "
    if python3 -m pip --version &>/dev/null; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${RED}未安装${NC}"
        return 1
    fi
    
    echo -n "  evalscope: "
    if evalscope --version &>/dev/null 2>&1; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${YELLOW}未安装（性能压测需要）${NC}"
    fi
    
    echo -n "  matplotlib: "
    if python3 -c "import matplotlib" 2>/dev/null; then
        echo -e "${GREEN}OK${NC}"
    else
        echo -e "${YELLOW}未安装（TPM 趋势图需要）${NC}"
    fi
    
    echo ""
    echo -e "${BLUE}--- 配置检查 ---${NC}"
    if [[ -f configs/.env ]]; then
        echo -e "  configs/.env: ${GREEN}已配置${NC}"
        source <(sed '1s/^\xEF\xBB\xBF//; s/\r$//' configs/.env) 2>/dev/null || true
        [[ -n "${API_URL:-}" ]] && echo -e "    API_URL: ${GREEN}已设置${NC}" || echo -e "    API_URL: ${RED}未设置${NC}"
        [[ -n "${API_KEY:-}" ]] && echo -e "    API_KEY: ${GREEN}已设置${NC}" || echo -e "    API_KEY: ${RED}未设置${NC}"
        [[ -n "${MODEL_NAME:-}" ]] && echo -e "    MODEL_NAME: ${GREEN}已设置${NC}" || echo -e "    MODEL_NAME: ${RED}未设置${NC}"
        [[ -n "${TOKENIZER:-}" ]] && echo -e "    TOKENIZER: ${GREEN}${TOKENIZER}${NC}" || echo -e "    TOKENIZER: ${YELLOW}未设置（perf 压测需要）${NC}"
        case "${PROTOCOL:-}" in
            openai)    echo -e "    PROTOCOL: ${GREEN}openai${NC}" ;;
            anthropic) echo -e "    PROTOCOL: ${GREEN}anthropic${NC}（Claude 系列）" ;;
            *)         echo -e "    PROTOCOL: ${RED}未设置或非法（必须为 openai 或 anthropic）${NC}" ;;
        esac
    else
        echo -e "  configs/.env: ${YELLOW}未配置${NC}"
        echo -e "  请运行: ${BLUE}cp configs/env.example configs/.env${NC} 并填写你的 API 信息"
    fi

    echo ""
    echo -e "${BLUE}--- 命名 env 配置（--env <name> 选择）---${NC}"
    local _e
    local -a _envs=()
    for _e in configs/env.d/*.env; do
        [[ -f "$_e" ]] || continue
        _envs+=("$(basename "${_e%.env}")")
    done
    for _e in configs/.env.*; do
        [[ -f "$_e" ]] || continue
        _envs+=("$(basename "$_e" | sed 's/^\.env\.//')")
    done
    if [[ ${#_envs[@]} -gt 0 ]]; then
        echo -e "  可用: ${GREEN}${_envs[*]}${NC}"
    else
        echo -e "  ${YELLOW}无（多模型测试可在 configs/env.d/ 下新建 <name>.env）${NC}"
    fi

    echo ""
    echo -e "${BLUE}--- 数据集检查 ---${NC}"
    if [[ -d perf/datasets ]]; then
        local count=$(ls perf/datasets/*.jsonl 2>/dev/null | wc -l)
        echo -e "  perf/datasets: ${GREEN}${count} 个数据集${NC}"
    else
        echo -e "  perf/datasets: ${YELLOW}不存在（性能压测需要）${NC}"
    fi
}

# ---- 安装依赖（先检测再安装）----
cmd_install() {
    echo -e "${BLUE}=== 安装依赖 ===${NC}"
    
    echo -e "${BLUE}[1/2] 性能压测依赖...${NC}"
    ensure_requirements "$KIT_DIR/perf/requirements.txt" "性能压测"
    
    echo -e "${BLUE}[2/2] 效果评测依赖...${NC}"
    ensure_requirements "$KIT_DIR/eval/requirements.txt" "效果评测"
    
    echo -e "${GREEN}✅ 依赖检查完成${NC}"
}

# ---- 性能压测 ----
cmd_perf() {
    echo -e "${BLUE}=== 性能压测 ===${NC}"
    if [[ ! -f perf/run.sh ]]; then
        echo -e "${RED}perf/run.sh 不存在${NC}"
        return 1
    fi
    # 解析 THVV 专属参数（--env / --url / --model 等），其余透传给 perf/run.sh
    thvv_parse_env_args "$KIT_DIR/configs" "$@" || return 1
    # 加载 env 文件：--env 选中的命名配置，否则默认 configs/.env（外部环境变量优先）
    thvv_load_env_file "${THVV_ENV_FILE:-$KIT_DIR/configs/.env}"
    thvv_apply_overrides
    case "${PROTOCOL:-openai}" in
        openai|anthropic) ;;
        *)
            echo -e "${RED}✗ PROTOCOL 仅支持 openai 或 anthropic，当前 PROTOCOL=${PROTOCOL:-未设置}${NC}"
            return 1
            ;;
    esac
    ensure_requirements "$KIT_DIR/perf/requirements.txt" "性能压测"
    cd perf
    bash run.sh ${THVV_REST_ARGS[@]+"${THVV_REST_ARGS[@]}"}
}

# ---- 效果评测 ----
cmd_eval() {
    echo -e "${BLUE}=== 效果评测 ===${NC}"
    if [[ ! -f eval/run.sh ]]; then
        echo -e "${RED}eval/run.sh 不存在${NC}"
        return 1
    fi
    # 解析 THVV 专属参数（--env / --url / --model 等），其余透传给 eval/run.sh
    thvv_parse_env_args "$KIT_DIR/configs" "$@" || return 1
    # 加载 env 文件：--env 选中的命名配置，否则默认 configs/.env（外部环境变量优先）
    thvv_load_env_file "${THVV_ENV_FILE:-$KIT_DIR/configs/.env}"
    thvv_apply_overrides
    ensure_requirements "$KIT_DIR/eval/requirements.txt" "效果评测"
    cd "$KIT_DIR/eval"
    bash run.sh ${THVV_REST_ARGS[@]+"${THVV_REST_ARGS[@]}"}
}

# ---- 主入口 ----
main() {
    local sub="${1:-}"
    if [[ -z "$sub" ]]; then
        echo "THVV — 用法:"
        echo "  bash quickstart.sh check                    环境检查"
        echo "  bash quickstart.sh install                  安装依赖"
        echo "  bash quickstart.sh perf bench <bucket> [请求数N] [并发P]  单档压测（默认 500 请求 / 10 并发）"
        echo "  bash quickstart.sh perf bench-all           全档位 × 并发梯度压测"
        echo "  bash quickstart.sh perf report              生成报告"
        echo "  bash quickstart.sh eval bench <datasets>    效果评测"
        echo "  bash quickstart.sh eval list                列出支持的数据集"
        echo ""
        echo "  端点直传: perf/eval 子命令可用 --env <name> 选择命名配置（configs/env.d/<name>.env）"
        echo "            或直接 --url/--api-key/--model/--protocol/--tokenizer/--provider/--judge-api-key 覆盖"
        echo ""
        echo "协议: 在 configs/.env 中设置 PROTOCOL=openai 或 anthropic"
        exit 0
    fi
    shift || true
    case "$sub" in
        check)   cmd_check "$@" ;;
        install) cmd_install "$@" ;;
        perf)    cmd_perf "$@" ;;
        eval)    cmd_eval "$@" ;;
        -h|--help|help)
            echo "THVV — 一键启动脚本"
            echo ""
            echo "用法: bash quickstart.sh <command> [args...]"
            echo ""
            echo "命令:"
            echo "  check        环境检查"
            echo "  install      安装依赖"
            echo "  perf         进入性能压测（支持 openai / anthropic 协议）"
            echo "    bench <bucket> [请求数N] [并发P]   单档压测（默认 500 请求 / 10 并发）"
            echo "    bench-all                全档位 × 并发梯度"
            echo "    report                   生成报告"
            echo "  eval         效果评测（11 个数据集：AIME/GPQA/HLE/MMLU-Pro/...）"
            echo "    bench <datasets>         单/多数据集评测（如 aime25,gpqa_diamond）"
            echo "    list                     列出支持的数据集"
            echo ""
            echo "  端点直传: perf/eval 子命令可用 --env <name> 选择命名配置（configs/env.d/<name>.env）"
            echo "            或直接 --url/--api-key/--model/--protocol/--tokenizer/--provider/--judge-api-key 覆盖"
            echo ""
            echo "协议: 在 configs/.env 中设置 PROTOCOL=openai 或 anthropic"
            echo "  openai     → OpenAI Chat Completions"
            echo "  anthropic  → Anthropic Messages / Claude 系列"
            exit 0
            ;;
        *)
            echo "未知命令: $sub"
            echo "可用命令: check, install, perf, eval"
            echo "详细用法: bash quickstart.sh --help"
            exit 1
            ;;
    esac
}

main "$@"
