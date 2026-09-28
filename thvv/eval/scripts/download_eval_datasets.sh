#!/usr/bin/env bash
# ============================================================
# 预下载 eval 数据集到本地镜像（离线加载用）
# ============================================================
# 原理：
#   run_eval.py 检测 eval/datasets/repos/<org--name>/.mirror_complete 存在时，
#   注入 local_path，evalscope 走 HubType.LOCAL → datasets.load_dataset(本地目录)，
#   完全离线加载（不访问 ModelScope/HuggingFace）。
#   （tau2_bench 例外：adapter 覆写 load()，直接以目录为 TAU2_DATA_DIR）
#
# 兼容性修复（下载后自动执行，幂等）：
#   1) 删除 dataset_infos.json —— ModelScope 元数据 features 为旧格式
#      {"_type":"Value"}（无 dtype），HF datasets>=3.x 解析必崩
#   2) 给 gpqa/simple_qa/longbench_v2 的 README.md YAML 补 configs 块 ——
#      否则 HF datasets 自动打包全部进 train split，与 evalscope 的
#      eval_split（test/train）不齐
#
# subset 说明：mmlu_pro/hle/longbench_v2 走 evalscope reformat_subset 机制，
#   只加载 default config，再按 category/length 列重分组，无需按子集建 config。
#
# 用法：
#   bash scripts/download_eval_datasets.sh              # 下载全部 8 个 + 修复 + 验证
#   bash scripts/download_eval_datasets.sh aime25 hle   # 只处理指定数据集
#   bash scripts/download_eval_datasets.sh --verify     # 只验证
#   bash scripts/download_eval_datasets.sh --fix        # 只修复（不下载）
#
# 特性：幂等可重跑（.mirror_complete 存在即跳过下载）；ModelScope 源（国内直连）
# ============================================================
set -euo pipefail

KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # eval/
BASE="$KIT_DIR/datasets/repos"

MODE="all"      # all | fix | verify
DATASETS=()
for a in "$@"; do
    case "$a" in
        --verify) MODE="verify" ;;
        --fix)    MODE="fix" ;;
        -h|--help) sed -n '2,30p' "$0" | grep '^# ' ; exit 0 ;;
        *) DATASETS+=("$a") ;;
    esac
done

# thvv 数据集 key -> ModelScope dataset id / repo 目录名（与 run_eval.py repo_map 保持一致）
declare -A REPO_ID=(
    [aime25]="evalscope/aime25"
    [aime26]="evalscope/aime26"
    [gpqa_diamond]="AI-ModelScope/gpqa_diamond"
    [hle]="cais/hle"
    [simple_qa]="evalscope/SimpleQA"
    [mmlu_pro]="TIGER-Lab/MMLU-Pro"
    [longbench_v2]="ZhipuAI/LongBench-v2"
    [tau2_bench]="evalscope/tau2-bench-data"
)
declare -A REPO_DIR=(
    [aime25]="evalscope--aime25"
    [aime26]="evalscope--aime26"
    [gpqa_diamond]="AI-ModelScope--gpqa_diamond"
    [hle]="cais--hle"
    [simple_qa]="evalscope--SimpleQA"
    [mmlu_pro]="TIGER-Lab--MMLU-Pro"
    [longbench_v2]="ZhipuAI--LongBench-v2"
    [tau2_bench]="evalscope--tau2-bench-data"
)

KEYS=()
if [[ ${#DATASETS[@]} -gt 0 ]]; then
    for k in "${DATASETS[@]}"; do
        [[ -n "${REPO_ID[$k]:-}" ]] || { echo "[fail] 未知数据集: $k（可选: ${!REPO_ID[*]}）"; exit 1; }
        KEYS+=("$k")
    done
else
    KEYS=("${!REPO_ID[@]}")
fi

download_one() {
    local key="$1" repo_id="$2" repo_dir="$3" target="$BASE/$4"
    if [[ -f "$target/.mirror_complete" && -n "$(ls -A "$target" 2>/dev/null | grep -v '^\.\|^$')" ]]; then
        echo "[skip] $key（已镜像: $repo_dir）"
        return 0
    fi
    echo "[down] $key  <-  modelscope: $repo_id"
    python3 - "$repo_id" "$target" <<'PY'
import sys
from modelscope import snapshot_download
repo_id, local_dir = sys.argv[1], sys.argv[2]
snapshot_download(repo_id, repo_type='dataset', local_dir=local_dir)
PY
    touch "$target/.mirror_complete"
    echo "[done] $key  ->  $repo_dir"
}

fix_all() {
    echo ""
    echo "=== 兼容性修复（幂等） ==="
    python3 - "$BASE" "$@" <<'PY'
import os, sys

base, keys = sys.argv[1], sys.argv[2:]
REPO_DIR = {
    'aime25': 'evalscope--aime25',
    'aime26': 'evalscope--aime26',
    'gpqa_diamond': 'AI-ModelScope--gpqa_diamond',
    'hle': 'cais--hle',
    'simple_qa': 'evalscope--SimpleQA',
    'mmlu_pro': 'TIGER-Lab--MMLU-Pro',
    'longbench_v2': 'ZhipuAI--LongBench-v2',
    'tau2_bench': 'evalscope--tau2-bench-data',
}
# 需要补 configs 块的 README（data_files: split -> path）
CONFIGS_YAML = {
    'gpqa_diamond': 'configs:\n- config_name: default\n  data_files:\n  - split: train\n    path: train.jsonl\n',
    'simple_qa': 'configs:\n- config_name: default\n  data_files:\n  - split: test\n    path: simple_qa_test_set.csv\n',
    'longbench_v2': 'configs:\n- config_name: default\n  data_files:\n  - split: train\n    path: data.json\n',
}

for key in keys:
    path = os.path.join(base, REPO_DIR[key])
    if not os.path.isdir(path):
        print(f'SKIP {key:14s} 目录不存在')
        continue
    # 1) 删除旧格式 dataset_infos.json（HF datasets>=3.x 解析必崩）
    dij = os.path.join(path, 'dataset_infos.json')
    if os.path.isfile(dij):
        os.remove(dij)
        print(f'FIX  {key:14s} 删除 dataset_infos.json（旧格式 features）')
    # 2) README.md YAML 补 configs
    if key in CONFIGS_YAML:
        readme = os.path.join(path, 'README.md')
        with open(readme, encoding='utf-8') as f:
            lines = f.read().splitlines(keepends=True)
        if lines and lines[0].strip() == '---':
            end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
            if end is not None and not any(l.startswith('configs:') for l in lines[1:end]):
                lines.insert(end, CONFIGS_YAML[key])
                with open(readme, 'w', encoding='utf-8') as f:
                    f.writelines(lines)
                print(f'FIX  {key:14s} README.md 补 configs 块')
            else:
                print(f'OK   {key:14s} README 已有 configs')
        else:
            print(f'WARN {key:14s} README 无 YAML front matter，跳过')
    else:
        print(f'OK   {key:14s} 无需修复')
PY
}

verify_all() {
    echo ""
    echo "=== 验证本地镜像（离线直读） ==="
    python3 - "$BASE" "$@" <<'PY'
import os, sys
import datasets

base, keys = sys.argv[1], sys.argv[2:]
REPO_DIR = {
    'aime25': 'evalscope--aime25',
    'aime26': 'evalscope--aime26',
    'gpqa_diamond': 'AI-ModelScope--gpqa_diamond',
    'hle': 'cais--hle',
    'simple_qa': 'evalscope--SimpleQA',
    'mmlu_pro': 'TIGER-Lab--MMLU-Pro',
    'longbench_v2': 'ZhipuAI--LongBench-v2',
    'tau2_bench': 'evalscope--tau2-bench-data',
}
# (config_name, split)：mmlu_pro/hle/longbench_v2 走 reformat_subset，只加载 default；
# tau2_bench adapter 覆写 load() 直接用目录（TAU2_DATA_DIR），只查目录结构
LOAD_CHECKS = {
    'aime25':       [(None, 'test')],
    'aime26':       [(None, 'test')],
    'gpqa_diamond': [(None, 'train')],
    'hle':          [(None, 'test')],
    'simple_qa':    [(None, 'test')],
    'mmlu_pro':     [(None, 'test'), (None, 'validation')],
    'longbench_v2': [(None, 'train')],
}

keys = keys or list(REPO_DIR.keys())
npass = nfail = nskip = 0
for key in keys:
    path = os.path.join(base, REPO_DIR[key])
    if not os.path.isfile(os.path.join(path, '.mirror_complete')):
        print(f'SKIP {key:14s} 未镜像（无 .mirror_complete）')
        nskip += 1
        continue
    if key == 'tau2_bench':
        # telecom 布局与 airline/retail 不同（db.toml/main_policy.md，tau2 上游如此）
        DOMAIN_FILES = {
            'airline': ['db.json', 'policy.md', 'tasks.json'],
            'retail':  ['db.json', 'policy.md', 'tasks.json'],
            'telecom': ['db.toml', 'main_policy.md', 'tasks.json'],
        }
        ok, detail = True, []
        for dom, files in DOMAIN_FILES.items():
            for f in files:
                if not os.path.isfile(os.path.join(path, 'tau2', 'domains', dom, f)):
                    ok, detail = False, detail + [f'tau2/domains/{dom}/{f} 缺失']
        if ok:
            print(f'PASS {key:14s} tau2/domains/{{airline,retail,telecom}} 结构完整')
            npass += 1
        else:
            print(f'FAIL {key:14s} {"; ".join(detail)}')
            nfail += 1
        continue
    for name, split in LOAD_CHECKS.get(key, []):
        try:
            ds = datasets.load_dataset(path=path, name=name, split=split)
            print(f'PASS {key:14s} name={name} split={split} rows={len(ds)}')
            npass += 1
        except Exception as e:
            print(f'FAIL {key:14s} name={name} split={split}: {type(e).__name__}: {str(e)[:150]}')
            nfail += 1
print(f'\n验证结果: PASS={npass}  FAIL={nfail}  SKIP={nskip}')
sys.exit(1 if nfail else 0)
PY
}

echo "=== THVV eval 数据集本地镜像 ==="
echo "镜像目录: $BASE   模式: $MODE"
echo ""

if [[ "$MODE" == "verify" ]]; then
    verify_all "${KEYS[@]}"
    exit $?
fi

if [[ "$MODE" != "fix" ]]; then
    for k in "${KEYS[@]}"; do
        download_one "$k" "${REPO_ID[$k]}" "${REPO_DIR[$k]}" "${REPO_DIR[$k]}"
    done
fi

fix_all "${KEYS[@]}"

if [[ "$MODE" != "fix" ]]; then
    verify_all "${KEYS[@]}"
fi
