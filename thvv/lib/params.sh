#!/usr/bin/env bash
# ============================================================
# THVV 公共参数解析 — 多模型配置选择（--env）与端点参数直传
# ============================================================
# 被 quickstart.sh / perf/run.sh / eval/run.sh source，提供：
#   thvv_parse_env_args <configs_dir> "$@"
#       解析 THVV 专属参数（从位置参数剥离，剩余写入数组 THVV_REST_ARGS）：
#         --env <name> | -e <name> | --env=<name>
#             选择命名 env 配置（查找顺序：configs/env.d/<name>.env → configs/.env.<name>）
#             等价环境变量：THVV_ENV=<name>；定位成功后 export THVV_ENV_FILE（绝对路径）
#         --url <url>          覆盖 API_URL（完整请求路径）
#         --api-key <key>      覆盖 API_KEY
#         --model <name>       覆盖 MODEL_NAME
#         --protocol <p>       覆盖 PROTOCOL（openai | anthropic）
#         --tokenizer <tok>    覆盖 TOKENIZER
#         --provider <name>    覆盖 PROVIDER（报告署名 / eval 产物目录前缀）
#         --judge-api-key <k>  覆盖 JUDGE_API_KEY（hle / simple_qa Judge）
#         （均支持 --opt=value 形式；覆盖项存入 THVV_OV_* 变量，由
#          thvv_apply_overrides 在 env 文件加载之后统一 export）
#   thvv_apply_overrides
#       将非空覆盖项 export 为环境变量（必须在 thvv_load_env_file 之后调用，
#       保证 CLI 参数优先于 env 文件）
#   thvv_load_env_file <file>
#       加载 env 文件（BOM/CRLF 容错）；外部已 export 的环境变量优先，
#       不被 env 文件中的同名值覆盖（备份-恢复）
#   thvv_list_envs <configs_dir>
#       列出可用的命名 env 配置（供报错提示 / check 展示）
#
# 配置优先级（高 → 低）：
#   CLI 参数（--url 等） > 外部环境变量 > --env 选中的命名 env 文件 > configs/.env
# ============================================================

# ---- 解析 THVV 专属参数 ----
thvv_parse_env_args() {
    local configs_dir="$1"
    shift
    THVV_REST_ARGS=()
    THVV_ENV_NAME="${THVV_ENV:-}"
    THVV_ENV_FILE=""
    THVV_OV_API_URL=""
    THVV_OV_API_KEY=""
    THVV_OV_MODEL_NAME=""
    THVV_OV_PROTOCOL=""
    THVV_OV_TOKENIZER=""
    THVV_OV_PROVIDER=""
    THVV_OV_JUDGE_API_KEY=""
    local opt
    while [[ $# -gt 0 ]]; do
        opt="$1"
        case "$opt" in
            --env|-e)
                [[ $# -lt 2 ]] && { echo "[fail] $opt 需要参数：命名 env 名（如 --env glm53）" >&2; return 1; }
                THVV_ENV_NAME="$2"; shift 2 ;;
            --env=*)        THVV_ENV_NAME="${opt#--env=}"; shift ;;
            --url)
                [[ $# -lt 2 ]] && { echo "[fail] $opt 需要参数：API_URL（完整请求路径）" >&2; return 1; }
                THVV_OV_API_URL="$2"; shift 2 ;;
            --url=*)        THVV_OV_API_URL="${opt#--url=}"; shift ;;
            --api-key)
                [[ $# -lt 2 ]] && { echo "[fail] $opt 需要参数：API_KEY" >&2; return 1; }
                THVV_OV_API_KEY="$2"; shift 2 ;;
            --api-key=*)    THVV_OV_API_KEY="${opt#--api-key=}"; shift ;;
            --model)
                [[ $# -lt 2 ]] && { echo "[fail] $opt 需要参数：MODEL_NAME" >&2; return 1; }
                THVV_OV_MODEL_NAME="$2"; shift 2 ;;
            --model=*)      THVV_OV_MODEL_NAME="${opt#--model=}"; shift ;;
            --protocol)
                [[ $# -lt 2 ]] && { echo "[fail] $opt 需要参数：openai 或 anthropic" >&2; return 1; }
                THVV_OV_PROTOCOL="$2"; shift 2 ;;
            --protocol=*)   THVV_OV_PROTOCOL="${opt#--protocol=}"; shift ;;
            --tokenizer)
                [[ $# -lt 2 ]] && { echo "[fail] $opt 需要参数：tokenizer 仓库名" >&2; return 1; }
                THVV_OV_TOKENIZER="$2"; shift 2 ;;
            --tokenizer=*)  THVV_OV_TOKENIZER="${opt#--tokenizer=}"; shift ;;
            --provider)
                [[ $# -lt 2 ]] && { echo "[fail] $opt 需要参数：PROVIDER 名称" >&2; return 1; }
                THVV_OV_PROVIDER="$2"; shift 2 ;;
            --provider=*)   THVV_OV_PROVIDER="${opt#--provider=}"; shift ;;
            --judge-api-key)
                [[ $# -lt 2 ]] && { echo "[fail] $opt 需要参数：JUDGE_API_KEY" >&2; return 1; }
                THVV_OV_JUDGE_API_KEY="$2"; shift 2 ;;
            --judge-api-key=*) THVV_OV_JUDGE_API_KEY="${opt#--judge-api-key=}"; shift ;;
            *)              THVV_REST_ARGS+=("$opt"); shift ;;
        esac
    done
    # 定位命名 env 文件（env.d/<name>.env 优先，兼容散放 .env.<name>）
    if [[ -n "$THVV_ENV_NAME" ]]; then
        local f
        for f in "$configs_dir/env.d/${THVV_ENV_NAME}.env" "$configs_dir/.env.${THVV_ENV_NAME}"; do
            if [[ -f "$f" ]]; then
                THVV_ENV_FILE="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
                break
            fi
        done
        if [[ -z "$THVV_ENV_FILE" ]]; then
            echo "[fail] 未找到命名 env 配置 '$THVV_ENV_NAME'（查找: $configs_dir/env.d/${THVV_ENV_NAME}.env 或 $configs_dir/.env.${THVV_ENV_NAME}）" >&2
            thvv_list_envs "$configs_dir"
            return 1
        fi
        export THVV_ENV="$THVV_ENV_NAME" THVV_ENV_NAME THVV_ENV_FILE
    fi
    return 0
}

# ---- 将覆盖项 export 为环境变量（须在 thvv_load_env_file 之后调用）----
thvv_apply_overrides() {
    [[ -n "$THVV_OV_API_URL" ]]       && export API_URL="$THVV_OV_API_URL"
    [[ -n "$THVV_OV_API_KEY" ]]       && export API_KEY="$THVV_OV_API_KEY"
    [[ -n "$THVV_OV_MODEL_NAME" ]]    && export MODEL_NAME="$THVV_OV_MODEL_NAME"
    [[ -n "$THVV_OV_PROTOCOL" ]]      && export PROTOCOL="$THVV_OV_PROTOCOL"
    [[ -n "$THVV_OV_TOKENIZER" ]]     && export TOKENIZER="$THVV_OV_TOKENIZER"
    [[ -n "$THVV_OV_PROVIDER" ]]      && export PROVIDER="$THVV_OV_PROVIDER"
    [[ -n "$THVV_OV_JUDGE_API_KEY" ]] && export JUDGE_API_KEY="$THVV_OV_JUDGE_API_KEY"
    return 0
}

# ---- 加载 env 文件（外部已 export 的环境变量优先，不被文件值覆盖）----
thvv_load_env_file() {
    local envfile="$1"
    if [[ ! -f "$envfile" ]]; then
        return 0
    fi
    local -a _names=() _vals=()
    local _k
    while IFS= read -r _k; do
        [[ -z "$_k" ]] && continue
        if [[ -n "${!_k+x}" ]]; then
            _names+=("$_k")
            _vals+=("${!_k}")
        fi
    done < <(sed '1s/^\xEF\xBB\xBF//' "$envfile" | grep -oE '^[A-Za-z_][A-Za-z0-9_]*' | sort -u)
    set -a; source <(sed '1s/^\xEF\xBB\xBF//; s/\r$//' "$envfile"); set +a
    local _i
    for _i in "${!_names[@]}"; do
        export "${_names[$_i]}"="${_vals[$_i]}"
    done
    echo "[env] 已加载: $envfile${THVV_ENV_NAME:+ (--env $THVV_ENV_NAME)}" >&2
    return 0
}

# ---- 列出可用的命名 env 配置 ----
thvv_list_envs() {
    local configs_dir="$1"
    local -a names=()
    local e b
    if [[ -d "$configs_dir/env.d" ]]; then
        for e in "$configs_dir"/env.d/*.env; do
            [[ -f "$e" ]] || continue
            b="$(basename "$e" .env)"
            [[ "$b" == "README" ]] && continue
            names+=("$b")
        done
    fi
    for e in "$configs_dir"/.env.*; do
        [[ -f "$e" ]] || continue
        b="$(basename "$e")"
        b="${b#.env.}"
        names+=("$b")
    done
    if [[ ${#names[@]} -gt 0 ]]; then
        echo "[help] 可用的命名 env: ${names[*]}（目录: $configs_dir/env.d/）" >&2
    else
        echo "[help] 尚无命名 env 配置，请新建: $configs_dir/env.d/<name>.env（可从 configs/env.example 复制）" >&2
    fi
    return 0
}
