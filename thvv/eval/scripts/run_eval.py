# encoding:utf-8
"""
evalscope 通用效果评测脚本

支持 11 个数据集的 LLM 精度评测，基于 evalscope 框架。
包含预检查、限流重试、HTML 报告生成、结果打包。

用法示例:
  # 单数据集
  python3 scripts/run_eval.py --datasets aime25 \
      --model_name glm-5.2 --base_url https://open.bigmodel.cn/api/paas/v4 \
      --api_key sk-xxx

  # 多数据集
  python3 scripts/run_eval.py --datasets aime25,gpqa_diamond \
      --model_name glm-5.2 --base_url https://... --api_key sk-xxx

  # 全部数据集
  python3 scripts/run_eval.py --datasets all \
      --model_name glm-5.2 --base_url https://... --api_key sk-xxx

  # 列出支持的数据集
  python3 scripts/run_eval.py --list-datasets

  # HLE（需要 judge model）
  python3 scripts/run_eval.py --datasets hle \
      --model_name glm-5.2 --base_url https://... --api_key sk-xxx \
      --judge_model deepseek-v3-0324 \
      --judge_base_url https://api.example.com/v1 \
      --judge_api_key sk-xxx
"""
import os
import sys
import re
import json
import time
import csv
import shutil
import argparse
import subprocess
import tarfile
from datetime import datetime

# v2 效果评测报告生成器（eval_report_v2.py 与本脚本同目录）：
# 六章模版（核心结论/效果稳定性/性能/配置/逐题证据/异常跳过）。
# 导入失败时降级为「不出报告」，绝不阻断主流程。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import eval_report_v2
except ImportError:  # pragma: no cover
    eval_report_v2 = None

# ---------------------------------------------------------------------------
# 数据集注册表
# ---------------------------------------------------------------------------

_FALLBACK_DATASET_CONFIG = {
    'label': None,
    'default_repeats': 1,
    'default_eval_batch_size': 10,
    'judge_strategy': 'auto',
    'requires_judge_model': False,
    'requires_sandbox': False,
    'description': None,
}

SUPPORTED_DATASETS = {
    'aime25': {
        'label': 'aime25',
        'default_repeats': 16,
        'default_eval_batch_size': 30,
        'judge_strategy': 'auto',
        'requires_judge_model': False,
        'description': 'AIME25 数学竞赛题评测',
    },
    'aime26': {
        'label': 'aime26',
        'default_repeats': 16,
        'default_eval_batch_size': 30,
        'judge_strategy': 'auto',
        'requires_judge_model': False,
        'description': 'AIME 2026 数学竞赛题评测',
    },
    'gpqa_diamond': {
        'label': 'gpqa_diamond',
        'default_repeats': 3,
        'default_eval_batch_size': 20,
        'judge_strategy': 'auto',
        'requires_judge_model': False,
        'description': 'GPQA-Diamond 研究生级问答评测',
    },
    'hle': {
        'label': 'hle',
        'default_repeats': 1,
        'default_eval_batch_size': 30,
        'judge_strategy': 'llm',
        'requires_judge_model': True,
        'description': "Humanity's Last Exam 综合评测（需要 LLM Judge）",
    },
    'tau2_bench': {
        'label': 'tau2_bench',
        'default_repeats': 5,
        'default_eval_batch_size': 5,
        'judge_strategy': 'auto',
        'requires_judge_model': False,
        'description': 'tau2-bench Agent 对话评测',
    },
    'mmlu_pro': {
        'label': 'mmlu_pro',
        'default_repeats': 1,
        'default_eval_batch_size': 30,
        'judge_strategy': 'auto',
        'requires_judge_model': False,
        'description': 'MMLU-Pro 多学科多选题评测',
    },
    'simple_qa': {
        'label': 'simple_qa',
        'default_repeats': 1,
        'default_eval_batch_size': 30,
        'judge_strategy': 'llm',
        'requires_judge_model': True,
        'description': 'SimpleQA 事实准确性评测（LLM-as-judge）',
    },
    'longbench_v2': {
        'label': 'longbench_v2',
        'default_repeats': 1,
        'default_eval_batch_size': 10,
        'judge_strategy': 'auto',
        'requires_judge_model': False,
        'description': 'LongBench v2 长上下文评测',
    },
    'live_code_bench': {
        'label': 'live_code_bench',
        'default_repeats': 1,
        'default_eval_batch_size': 5,
        'judge_strategy': 'auto',
        'requires_judge_model': False,
        'requires_docker': True,
        'description': 'LiveCodeBench 实时代码生成评测（推荐 Docker）',
    },
    'swe_bench_verified_mini_agentic': {
        'label': 'swe_bench_verified_mini_agentic',
        'default_repeats': 1,
        'default_eval_batch_size': 1,
        'judge_strategy': 'auto',
        'requires_judge_model': False,
        'requires_docker': True,
        'requires_swebench': True,
        'requires_ms_enclave': True,
        'description': 'SWE-Bench Verified Agentic 评测（需要 Docker + swebench + ms_enclave）',
    },
    'swe_bench_pro': {
        'label': 'swe_bench_pro',
        'default_repeats': 1,
        'default_eval_batch_size': 1,
        'judge_strategy': 'auto',
        'requires_judge_model': False,
        'requires_sandbox': True,
        'requires_ms_enclave': True,
        'description': 'SWE-Bench Pro 软件工程评测（需要 Docker + ms_enclave）',
    },
}

_SANDBOX_DATASET_PREFIXES = ('swe_bench',)
_DOCKER_DATASET_PREFIXES = ('swe_bench', 'live_code_bench')
_SWEBENCH_DATASET_KEYS = ('swe_bench_verified', 'swe_bench_verified_mini', 'swe_bench_lite')
_MS_ENCLAVE_DATASET_KEYS = (
    'swe_bench_verified_mini_agentic', 'swe_bench_verified_agentic',
    'swe_bench_lite_agentic', 'swe_bench_pro_agentic', 'swe_bench_pro',
)

def get_dataset_config(dataset_key):
    """获取数据集配置，已知返回预设值，未知返回兜底配置"""
    if dataset_key in SUPPORTED_DATASETS:
        return dict(SUPPORTED_DATASETS[dataset_key])
    cfg = dict(_FALLBACK_DATASET_CONFIG)
    cfg['label'] = dataset_key
    cfg['description'] = f'{dataset_key}（外部数据集，使用通用默认参数）'
    cfg['requires_docker'] = False
    cfg['requires_swebench'] = False
    cfg['requires_ms_enclave'] = False
    for prefix in _SANDBOX_DATASET_PREFIXES:
        if dataset_key.startswith(prefix):
            cfg['requires_sandbox'] = True
            break
    for prefix in _DOCKER_DATASET_PREFIXES:
        if dataset_key.startswith(prefix):
            cfg['requires_docker'] = True
            break
    for k in _SWEBENCH_DATASET_KEYS:
        if k in dataset_key and '_agentic' not in dataset_key:
            cfg['requires_swebench'] = True
            break
    for k in _MS_ENCLAVE_DATASET_KEYS:
        if k in dataset_key:
            cfg['requires_ms_enclave'] = True
            break
    return cfg


# ---------------------------------------------------------------------------
# 预检查
# ---------------------------------------------------------------------------

def _validate_url(url, name='URL'):
    if not url:
        return f"{name} 不能为空"
    if not re.match(r'^https?://', url):
        return f"{name} 格式无效，必须以 http:// 或 https:// 开头: {url}"
    return None


def _validate_not_empty(value, name):
    if not value or not str(value).strip():
        return f"{name} 不能为空"
    return None


def _check_docker_available():
    if shutil.which('docker'):
        try:
            r = subprocess.run(
                ['docker', 'version', '--format', '{{.Server.Version}}'],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return True
        except Exception:
            pass
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


def _check_swebench_available():
    try:
        import swebench  # noqa: F401
        return True
    except ImportError:
        return False


def _check_ms_enclave_available():
    try:
        import ms_enclave  # noqa: F401
        return True
    except ImportError:
        return False


def pre_check(args):
    """执行所有参数预检查，返回错误列表"""
    errors = []

    # evalscope 环境
    evalscope_path = shutil.which('evalscope')
    if not evalscope_path:
        local_bin = os.path.expanduser('~/.local/bin/evalscope')
        if os.path.exists(local_bin):
            evalscope_path = local_bin
        else:
            errors.append("❌ evalscope 未安装或不在 PATH 中。请运行: pip install -r requirements.txt")

    # 必填参数
    err = _validate_not_empty(args.model_name, '--model_name')
    if err:
        errors.append(err)
    err = _validate_url(args.base_url, '--base_url')
    if err:
        errors.append(err)
    err = _validate_not_empty(args.api_key, '--api_key')
    if err:
        errors.append(err)

    # 数据集
    if not args.datasets:
        errors.append("❌ --datasets 不能为空，请指定数据集（如 aime25,gpqa_diamond）或 all")
    else:
        datasets = [d.strip() for d in args.datasets.split(',')]
        unknown = [d for d in datasets if d not in SUPPORTED_DATASETS and d != 'all']
        if unknown:
            pass  # 未知数据集不报错，用通用默认参数

    # 温度
    if args.temperature is not None:
        try:
            temp = float(args.temperature)
            if temp < 0.0 or temp > 2.0:
                errors.append(f"❌ --temperature 必须在 0.0 ~ 2.0 范围内，当前: {temp}")
        except (TypeError, ValueError):
            errors.append(f"❌ --temperature 必须是有效数字，当前: {args.temperature}")

    # 并发
    if args.eval_batch_size is not None:
        try:
            ebs = int(args.eval_batch_size)
            if ebs < 1:
                errors.append(f"❌ --eval_batch_size 必须 >= 1，当前: {ebs}")
        except (TypeError, ValueError):
            errors.append(f"❌ --eval_batch_size 必须是正整数，当前: {args.eval_batch_size}")

    # repeats
    if args.repeats is not None:
        try:
            r = int(args.repeats)
            if r < 1:
                errors.append(f"❌ --repeats 必须 >= 1，当前: {r}")
        except (TypeError, ValueError):
            errors.append(f"❌ --repeats 必须是正整数，当前: {args.repeats}")

    # limit
    if args.limit is not None:
        try:
            l = int(args.limit)
            if l < 1:
                errors.append(f"❌ --limit 必须 >= 1，当前: {l}")
        except (TypeError, ValueError):
            errors.append(f"❌ --limit 必须是正整数，当前: {args.limit}")

    # max_tokens
    if args.max_tokens is not None:
        try:
            mt = int(args.max_tokens)
            if mt < 1:
                errors.append(f"❌ --max_tokens 必须 >= 1，当前: {mt}")
        except (TypeError, ValueError):
            errors.append(f"❌ --max_tokens 必须是正整数，当前: {args.max_tokens}")

    # 需要 LLM Judge 的数据集
    datasets_list = [d.strip() for d in args.datasets.split(',')] if args.datasets else []
    _judge_datasets = {'hle', 'simple_qa'}
    if _judge_datasets & set(datasets_list) or args.datasets == 'all':
        err = _validate_not_empty(args.judge_model, '--judge_model（LLM Judge 需要）')
        if err:
            errors.append(err)
        err = _validate_url(args.judge_base_url, '--judge_base_url')
        if err:
            errors.append(err)
        err = _validate_not_empty(args.judge_api_key, '--judge_api_key')
        if err:
            errors.append(err)

    # tau2_bench 子集校验
    if 'tau2_bench' in datasets_list or args.datasets == 'all':
        if args.subset_list:
            subsets = [s.strip() for s in args.subset_list.split(',')]
            valid_domains = {'airline', 'retail', 'telecom'}
            invalid = [s for s in subsets if s not in valid_domains]
            if invalid:
                errors.append(
                    f"❌ tau2_bench 不支持的 domain: {', '.join(invalid)}。"
                    f"支持: {', '.join(sorted(valid_domains))}"
                )

    # tau2_bench 依赖
    if 'tau2_bench' in datasets_list or args.datasets == 'all':
        try:
            import tau2  # noqa: F401
        except ImportError:
            print(f"\n  ⚠️ tau2-bench 依赖缺失，自动安装...")
            try:
                subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '-r',
                     'requirements.txt', '-q'],
                    check=True, timeout=120
                )
                sys.modules.pop('tau2', None)
                import tau2  # noqa: F401
                print(f"  ✅ tau2-bench 安装成功")
            except Exception as e:
                errors.append(
                    f"❌ tau2-bench 自动安装失败: {e}。"
                    f"请手动: pip install -r requirements.txt"
                )

    # judge_strategy
    if args.judge_strategy is not None:
        valid_strategies = {'rule', 'llm', 'llm_recall', 'auto'}
        if args.judge_strategy not in valid_strategies:
            errors.append(
                f"❌ --judge_strategy 无效: {args.judge_strategy}。"
                f"可选: {', '.join(sorted(valid_strategies))}"
            )

    # sandbox 数据集
    sandbox_datasets = [d for d in datasets_list
                        if get_dataset_config(d).get('requires_sandbox')]
    if sandbox_datasets:
        if not args.sandbox:
            errors.append(
                f"❌ 以下数据集需要 Docker Sandbox: {', '.join(sandbox_datasets)}。"
                f"请添加 --sandbox 参数"
            )
        elif args.sandbox.strip().startswith('{'):
            try:
                json.loads(args.sandbox)
            except json.JSONDecodeError as e:
                errors.append(f"❌ --sandbox 不是有效的 JSON: {e}")

    # Docker 依赖
    docker_datasets = [d for d in datasets_list
                       if get_dataset_config(d).get('requires_docker')]
    if docker_datasets:
        if not _check_docker_available():
            errors.append(
                f"❌ 以下数据集依赖 Docker: {', '.join(docker_datasets)}。"
                f"当前环境未检测到可用的 Docker daemon"
            )

    # swebench 依赖
    swebench_datasets = [d for d in datasets_list
                         if get_dataset_config(d).get('requires_swebench')]
    if swebench_datasets:
        if not _check_swebench_available():
            errors.append(
                f"❌ 以下数据集需要 swebench 包: {', '.join(swebench_datasets)}。"
                f"请执行: pip install 'evalscope[swe_bench]' 或 pip install swebench==4.1.0"
            )

    # ms_enclave 依赖
    ms_enclave_datasets = [d for d in datasets_list
                           if get_dataset_config(d).get('requires_ms_enclave')]
    if ms_enclave_datasets:
        if not _check_ms_enclave_available():
            errors.append(
                f"❌ 以下数据集需要 ms_enclave 包: {', '.join(ms_enclave_datasets)}。"
                f"请执行: pip install 'evalscope[sandbox]' 或 pip install ms_enclave"
            )

    return errors


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def get_max_tokens(model_name, thinking):
    """根据模型名称和思考模式确定 max_tokens"""
    model_lower = model_name.lower()
    is_kimi = 'kimi' in model_lower
    if thinking:
        return 98304 if is_kimi else 131072
    return 16384


# 不同供应商/模型的 thinking.type 合法值不同：
#   - MiniMax（Anthropic Messages 风格）：adaptive / disabled
#   - 其余 OpenAI 兼容供应商：enabled / disabled
_THINKING_TYPE_BY_PROVIDER = {
    "minimax": "adaptive",
    "minimaxi": "adaptive",
}
_THINKING_TYPE_BY_MODEL = {
    "minimax": "adaptive",
}


def resolve_thinking_type(provider, model_name, thinking, thinking_type=None):
    """返回 thinking.type 的具体值（enabled/adaptive/disabled）。

    优先级：显式 --thinking-type > 按 provider 映射 > 按 model 映射 > 默认 enabled。
    thinking=False 时不传 thinking（返回 None）。
    """
    if not thinking:
        return None
    if thinking_type:
        return thinking_type
    p = (provider or "").lower()
    m = (model_name or "").lower()
    for key, val in _THINKING_TYPE_BY_PROVIDER.items():
        if key in p:
            return val
    for key, val in _THINKING_TYPE_BY_MODEL.items():
        if key in m:
            return val
    return "enabled"


def make_run_dir(provider, model_name):
    """生成一次 run 的目录名: <供应商>-<模型名>-<时间戳>。
    时间戳固定到秒，一次 run 内所有数据集共用同一 run_dir，
    确保 eval_summary / all_eval_summary / tar.gz / HTML 落同一 run 目录，
    避免被 eval/results/*/*/eval_summary.json 这种 glob 捞到其他 run 的结果。"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return f"{provider}-{model_name}-{timestamp}"


def get_result_dir(provider, model_name, benchmark, run_dir=None):
    """生成结果目录: results/<run_dir>/<benchmark>。
    run_dir 不传时每次调用会生成新秒级时间戳（保留旧行为），
    建议一次 run 显式传入固定 run_dir，保证多数据集落同一目录。"""
    if not run_dir:
        run_dir = make_run_dir(provider, model_name)
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(scripts_dir, '..', 'results', run_dir, benchmark))


def resolve_datasets(datasets_arg):
    """解析 --datasets 参数，返回 (known_list, unknown_list)"""
    if datasets_arg.strip().lower() == 'all':
        return list(SUPPORTED_DATASETS.keys()), []
    all_ds = [d.strip() for d in datasets_arg.split(',')]
    known = [d for d in all_ds if d in SUPPORTED_DATASETS]
    unknown = [d for d in all_ds if d not in SUPPORTED_DATASETS]
    return known, unknown


def default_dataset_dir():
    """默认本地数据集缓存目录。"""
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(scripts_dir, '..', 'datasets'))


def build_eval_command(args, dataset_key, run_dir=None):
    """为指定数据集构建 evalscope eval 命令。返回 (cmd_list, output_dir)"""
    ds_config = get_dataset_config(dataset_key)

    max_tokens = args.max_tokens if args.max_tokens is not None else get_max_tokens(
        args.model_name, args.thinking
    )
    repeats = args.repeats if args.repeats is not None else ds_config['default_repeats']
    eval_batch_size = (args.eval_batch_size if args.eval_batch_size is not None
                       else ds_config['default_eval_batch_size'])
    judge_strategy = (args.judge_strategy if args.judge_strategy is not None
                      else ds_config['judge_strategy'])

    output_dir = args.output_dir or get_result_dir(args.provider, args.model_name, dataset_key, run_dir)

    cmd = [
        'evalscope', 'eval',
        '--model', args.model_name,
        '--api-url', args.base_url,
        '--api-key', args.api_key,
        '--datasets', ds_config['label'],
        '--work-dir', output_dir,
        '--no-timestamp',
        '--stream',
        '--judge-strategy', judge_strategy,
    ]
    if args.ignore_errors:
        cmd.append('--ignore-errors')

    # generation_config
    gen_config = {"max_tokens": max_tokens}
    if args.temperature is not None:
        gen_config["temperature"] = float(args.temperature)
    if args.top_p is not None:
        gen_config["top_p"] = float(args.top_p)
    thinking_type = resolve_thinking_type(
        args.provider, args.model_name, args.thinking,
        getattr(args, "thinking_type", None),
    )
    if thinking_type:
        gen_config.setdefault("extra_body", {})["thinking"] = {"type": thinking_type}
    if args.provider_override:
        gen_config.setdefault("extra_headers", {})["X-Provider-Override"] = args.provider_override
    cmd.extend(['--generation-config', json.dumps(gen_config)])

    if repeats > 1:
        cmd.extend(['--repeats', str(repeats)])
    cmd.extend(['--eval-batch-size', str(eval_batch_size)])

    # 需 LLM Judge 的数据集
    if dataset_key in ('hle', 'simple_qa'):
        judge_model_args = {
            "model_id": args.judge_model,
            "api_key": args.judge_api_key,
            "api_url": args.judge_base_url,
        }
        cmd.extend(['--judge-model-args', json.dumps(judge_model_args)])

    # 本地数据集缓存目录：默认 eval/datasets，可由 --dataset_dir 覆盖
    dataset_dir = args.dataset_dir or default_dataset_dir()
    if dataset_dir:
        cmd.extend(['--dataset-dir', dataset_dir])

    dataset_args = {}

    # 本地镜像注入：如果 eval/datasets/repos/<仓库名>/.mirror_complete 存在，
    # 通过 local_path 覆盖 dataset_id，让 evalscope 走 load_from_disk()
    repo_map = {
        'aime25': 'evalscope--aime25',
        'aime26': 'evalscope--aime26',
        'gpqa_diamond': 'AI-ModelScope--gpqa_diamond',
        'hle': 'cais--hle',
        'tau2_bench': 'evalscope--tau2-bench-data',
        'mmlu_pro': 'TIGER-Lab--MMLU-Pro',
        'simple_qa': 'evalscope--SimpleQA',
        'longbench_v2': 'ZhipuAI--LongBench-v2',
    }
    repo_dir = repo_map.get(dataset_key)
    if repo_dir:
        local_repo = os.path.join(dataset_dir, 'repos', repo_dir)
        if os.path.exists(os.path.join(local_repo, '.mirror_complete')):
            dataset_args.setdefault(ds_config['label'], {})['local_path'] = local_repo

    # HLE 多模态
    if dataset_key == 'hle':
        dataset_args.setdefault('hle', {}).setdefault('extra_params', {})['include_multi_modal'] = bool(args.include_multi_modal)

    # tau2_bench
    if dataset_key == 'tau2_bench':
        dataset_args.setdefault('tau2_bench', {})['extra_params'] = {
            "user_model": args.model_name,
            "api_key": args.api_key,
            "api_base": args.base_url,
            "generation_config": {
                "temperature": float(args.temperature) if args.temperature is not None else (
                    1.0 if str(args.model_name).lower().startswith(('kimi', 'moonshot')) else 0.0
                ),
                "max_tokens": 4096,
            },
        }
        subsets = []
        if args.subset_list:
            subsets = [s.strip() for s in args.subset_list.split(',')]
        if subsets:
            dataset_args["tau2_bench"]["subset_list"] = subsets

    # mmlu_pro 学科子集
    if dataset_key == 'mmlu_pro' and args.mmlu_pro_subset:
        subsets = [s.strip() for s in args.mmlu_pro_subset.split(',') if s.strip()]
        if subsets:
            dataset_args.setdefault('mmlu_pro', {})['subset_list'] = subsets

    # longbench_v2 长度子集
    if dataset_key == 'longbench_v2' and args.longbench_v2_subset:
        subsets = [s.strip() for s in args.longbench_v2_subset.split(',') if s.strip()]
        if subsets:
            dataset_args.setdefault('longbench_v2', {})['subset_list'] = subsets

    # live_code_bench release 子集
    if dataset_key == 'live_code_bench':
        if args.live_code_bench_subset:
            subsets = [s.strip() for s in args.live_code_bench_subset.split(',') if s.strip()]
            if subsets:
                dataset_args.setdefault('live_code_bench', {})['subset_list'] = subsets
        if args.live_code_bench_start_date:
            dataset_args.setdefault('live_code_bench', {})['start_date'] = args.live_code_bench_start_date
        if args.live_code_bench_end_date:
            dataset_args.setdefault('live_code_bench', {})['end_date'] = args.live_code_bench_end_date

    if dataset_args:
        cmd.extend(['--dataset-args', json.dumps(dataset_args)])

    # sandbox
    if ds_config.get('requires_sandbox') and args.sandbox:
        cmd.extend(['--sandbox', args.sandbox])

    # 通用可选
    if args.limit is not None:
        cmd.extend(['--limit', str(args.limit)])
    if args.use_cache:
        cmd.extend(['--use-cache', args.use_cache])
    if args.debug:
        cmd.append('--debug')

    return cmd, output_dir


# ---------------------------------------------------------------------------
# 重试与限流处理
# ---------------------------------------------------------------------------

_RATE_LIMIT_PATTERNS = [
    re.compile(r'429', re.IGNORECASE),
    re.compile(r'rate.?limit', re.IGNORECASE),
    re.compile(r'too many requests', re.IGNORECASE),
    re.compile(r'quota.?exceeded', re.IGNORECASE),
    re.compile(r'throttled', re.IGNORECASE),
]

MAX_RETRIES = 3
RETRY_DELAY_SEC = 60
FAILURE_RETRY_DELAY = 5


def _detect_rate_limit(text):
    if not text:
        return False
    for pattern in _RATE_LIMIT_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _find_cache_dir(output_dir, model_name, dataset_label):
    """自动探测 evalscope 生成的缓存目录"""
    candidates = [
        os.path.join(output_dir, 'cache'),
        os.path.join(output_dir, f'{model_name}_cache'),
        os.path.join(output_dir, f'{dataset_label}_cache'),
        os.path.join(output_dir, model_name, 'cache'),
    ]
    for cand in candidates:
        if os.path.isdir(cand) and os.listdir(cand):
            return cand
    for root, dirs, _ in os.walk(output_dir):
        for d in dirs:
            if 'cache' in d.lower():
                return os.path.join(root, d)
    return None


def _inject_use_cache(cmd, output_dir, model_name, dataset_label):
    """向命令注入 --use-cache 参数"""
    new_cmd = []
    skip_next = False
    for arg in cmd:
        if skip_next:
            skip_next = False
            continue
        if arg == '--use-cache':
            skip_next = True
            continue
        new_cmd.append(arg)
    cmd = new_cmd

    cache_dir = _find_cache_dir(output_dir, model_name, dataset_label)
    if cache_dir:
        try:
            idx = cmd.index('--datasets')
        except ValueError:
            idx = len(cmd)
        cmd.insert(idx + 2, '--use-cache')
        cmd.insert(idx + 3, cache_dir)
        return cmd, cache_dir

    cmd.append('--use-cache')
    cmd.append(output_dir)
    return cmd, output_dir


def _run_with_retry(cmd, output_dir, model_name, dataset_label):
    """带重试机制的执行函数。

    策略:
    1. 首次正常执行
    2. 失败时检测是否限流（429 等）
       - 限流: 等待 60s 后注入 --use-cache 重试
       - 其他失败: 等待 5s 后注入 --use-cache 重试
    3. 最多总重试 MAX_RETRIES 次

    返回: (success, output_dir, retries_used, rate_limited)
    """
    retries = 0
    rate_limited = False

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            print(f"\n  🔄 第 {attempt}/{MAX_RETRIES} 次尝试...")

        current_cmd = list(cmd)
        if attempt > 1:
            current_cmd, cache_path = _inject_use_cache(
                current_cmd, output_dir, model_name, dataset_label
            )
            print(f"  💾 使用缓存续跑: {cache_path}")

        start_time = datetime.now()
        try:
            proc = subprocess.Popen(
                current_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            import threading
            stdout_lines = []
            stderr_lines = []
            last_progress_time = [time.time()]
            last_progress_count = [-1]

            def _read_stream(stream, lines_list, prefix=''):
                for line in iter(stream.readline, ''):
                    if not line:
                        break
                    lines_list.append(line)
                    print(f"  {prefix}{line}", end='', flush=True)

            stdout_thread = threading.Thread(
                target=_read_stream, args=(proc.stdout, stdout_lines, ''), daemon=True
            )
            stderr_thread = threading.Thread(
                target=_read_stream, args=(proc.stderr, stderr_lines, '[stderr] '), daemon=True
            )
            stdout_thread.start()
            stderr_thread.start()

            while proc.poll() is None:
                elapsed = (datetime.now() - start_time).total_seconds()
                time.sleep(5)
                now = time.time()
                if now - last_progress_time[0] >= 60:
                    try:
                        pred_dir = os.path.join(output_dir, 'predictions')
                        if os.path.isdir(pred_dir):
                            count = len([f for f in os.listdir(pred_dir)
                                         if f.endswith(('.json', '.jsonl'))])
                            if count != last_progress_count[0]:
                                print(f"\n  ⏳ [{dataset_label}] 已运行 {elapsed:.0f}s, "
                                      f"已完成 {count} 个预测文件", flush=True)
                                last_progress_count[0] = count
                            else:
                                print(f"\n  ⏳ [{dataset_label}] 已运行 {elapsed:.0f}s (等待中...)",
                                      flush=True)
                        else:
                            print(f"\n  ⏳ [{dataset_label}] 已运行 {elapsed:.0f}s (评测进行中...)",
                                  flush=True)
                    except Exception:
                        print(f"\n  ⏳ [{dataset_label}] 已运行 {elapsed:.0f}s", flush=True)
                    last_progress_time[0] = now

            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)

            remaining = proc.stdout.read()
            if remaining:
                stdout_lines.append(remaining)
                print(remaining, end='', flush=True)
            remaining = proc.stderr.read()
            if remaining:
                stderr_lines.append(remaining)

            stderr_text = ''.join(stderr_lines)
            returncode = proc.returncode

            if returncode == 0:
                elapsed = (datetime.now() - start_time).total_seconds()
                print(f"\n  ✅ 评测完成! 耗时: {elapsed:.0f}s"
                      f"{' (已重试)' if attempt > 1 else ''}")
                return True, output_dir, attempt - 1, rate_limited

            is_import_error = 'ImportError' in stderr_text or 'check_import' in stderr_text
            if is_import_error:
                print(f"\n  ❌ 缺少依赖，无法重试（请安装对应的 pip 包）")
                return False, output_dir, attempt - 1, rate_limited

            is_rate_limit = _detect_rate_limit(stderr_text)
            if is_rate_limit:
                rate_limited = True
                print(f"\n  ⚠️ 检测到限流 (429/rate limit)，等待 {RETRY_DELAY_SEC}s 后重试...")
            else:
                print(f"\n  ❌ 评测失败 (rc={returncode})")
                if stderr_text.strip():
                    lines = [l for l in stderr_text.split('\n') if l.strip()]
                    tail = lines[-15:] if len(lines) > 15 else lines
                    print(f"  stderr 关键信息:")
                    for l in tail:
                        print(f"    {l}")

            if attempt >= MAX_RETRIES:
                break

            delay = RETRY_DELAY_SEC if is_rate_limit else FAILURE_RETRY_DELAY
            print(f"  等待 {delay}s 后重试...")
            time.sleep(delay)

        except FileNotFoundError:
            print(f"\n  ❌ evalscope 未安装，请运行: pip install -r requirements.txt")
            return False, output_dir, attempt - 1, rate_limited

    return False, output_dir, MAX_RETRIES - 1, rate_limited


# ---------------------------------------------------------------------------
# 结果持久化
# ---------------------------------------------------------------------------

# v2 效果评测报告文件名（六章模版：核心结论/效果稳定性/性能/配置/逐题证据/异常跳过）
_V2_REPORT_NAME = 'eval_report_v2.html'

# reports/*.json 中体积过大、不适合整段塞进汇总与报告的字段
_SUMMARY_DROP_KEYS = {
    'details', 'dataset_description', 'description',
    'prompt_template', 'query_template', 'sample_example',
}


def _save_dataset_summary(ds_key, ds_label, output_dir, success, report_data,
                          retries, rate_limited, elapsed, skipped_count=0):
    """将单个数据集的评测结果保存为 JSON。

    skipped_count 为被 ignore_errors 静默跳过的题数（由 v2 报告生成器
    解析 reviews / skipped_samples.jsonl / eval_log.log 得出）。
    """
    os.makedirs(output_dir, exist_ok=True)
    summary_path = os.path.join(output_dir, 'eval_summary.json')

    summary = {
        'dataset': ds_label,
        'success': success,
        'output_dir': output_dir,
        'retries_used': retries,
        'rate_limited': rate_limited,
        'elapsed_seconds': round(elapsed, 1),
        'timestamp': datetime.now().isoformat(),
    }
    if report_data:
        summary['score'] = report_data.get('score')
        summary['num_samples'] = report_data.get('num')
        # 剔除长文本字段：数据集描述/模板不属于评测结果
        summary['metrics'] = {
            k: v for k, v in report_data.items() if k not in _SUMMARY_DROP_KEYS
        }
        summary['perf_metrics'] = report_data.get('perf_metrics')

    summary['skipped_count'] = int(skipped_count or 0)
    summary['report_v2'] = os.path.join(output_dir, _V2_REPORT_NAME)

    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)
    print(f"  📄 结果摘要已保存: {summary_path}")
    if summary['skipped_count']:
        print(f"  ⚠️ 容错跳过 {summary['skipped_count']} 题，"
              f"有效样本数已小于实际请求题数（详见 eval_report_v2.html 第六章）")
    return summary


def _save_overall_summary(all_summaries, provider, model_name, output_base_dir):
    """将全部数据集的评测结果汇总保存"""
    overall_path = os.path.join(output_base_dir, 'all_eval_summary.json')
    os.makedirs(output_base_dir, exist_ok=True)

    overall = {
        'provider': provider,
        'model': model_name,
        'timestamp': datetime.now().isoformat(),
        'total_datasets': len(all_summaries),
        'success_count': sum(1 for s in all_summaries if s.get('success')),
        'failure_count': sum(1 for s in all_summaries if not s.get('success')),
        'rate_limited_count': sum(1 for s in all_summaries if s.get('rate_limited')),
        'results': all_summaries,
    }

    with open(overall_path, 'w') as f:
        json.dump(overall, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n  📄 总汇总已保存: {overall_path}")
    return overall_path


def _format_duration(seconds):
    if seconds is None:
        return 'N/A'
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{int(m)}m {int(s)}s"
    else:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{int(h)}h {int(m)}m {int(s)}s"


# ---------------------------------------------------------------------------
# 增强版详细报告（evalscope 原生 report.html + 平台注入卡片）
# ---------------------------------------------------------------------------

def _generate_v2_report(output_dir, model_name, success):
    """生成 v2 效果评测报告（六章模版，THVV 对外唯一报告形态）。

    数据源为 evalscope 落盘产物：reviews/、skipped_samples.jsonl、
    logs/eval_log.log（跳过兜底）、reports/*.json、configs/task_config.yaml。
    任何失败都不阻断评测主流程。

    返回 {'path': str|None, 'cases': int, 'skipped': int}
    """
    info = {'path': None, 'cases': 0, 'skipped': 0}
    if eval_report_v2 is None or not success:
        return info
    from pathlib import Path
    try:
        out_html = Path(output_dir) / _V2_REPORT_NAME
        n_cases, n_skip, _n_swe = eval_report_v2.generate_report(
            Path(output_dir), out_html, model_name)
        info.update(path=str(out_html), cases=n_cases, skipped=n_skip)
        print(f"\n  📄 效果评测报告(v2) 已生成: {out_html}")
        print(f"     题目 {n_cases} ｜ 异常跳过 {n_skip}")
    except SystemExit as e:
        print(f"\n  ⚠️ v2 报告生成中止: {e}")
    except Exception as e:
        print(f"\n  ⚠️ v2 报告生成失败（不影响主流程）: {e}")
    return info


# ---------------------------------------------------------------------------
# 打包归档
# ---------------------------------------------------------------------------

def _package_results(result_base_dir, html_report_path, all_summaries):
    """将所有产出打包为 tar.gz"""
    archive_name = os.path.join(result_base_dir, 'eval_results.tar.gz')
    files_to_pack = []

    if html_report_path and os.path.exists(html_report_path):
        files_to_pack.append((html_report_path, '精度测试报告.html'))

    overall_json = os.path.join(result_base_dir, 'all_eval_summary.json')
    if os.path.exists(overall_json):
        files_to_pack.append((overall_json, 'all_eval_summary.json'))

    for s in all_summaries:
        ds_label = s.get('dataset', 'unknown')
        output_dir = s.get('output_dir', '')
        if output_dir and os.path.isdir(output_dir):
            summary_path = os.path.join(output_dir, 'eval_summary.json')
            if os.path.exists(summary_path):
                files_to_pack.append((summary_path, f'{ds_label}/eval_summary.json'))
            # 逐题明细（若有）
            details_path = os.path.join(output_dir, 'per_sample_details.csv')
            if os.path.exists(details_path):
                files_to_pack.append((details_path, f'{ds_label}/per_sample_details.csv'))
            # v2 效果评测报告（六章模版），生成了就一并归档
            v2_path = os.path.join(output_dir, _V2_REPORT_NAME)
            if os.path.exists(v2_path):
                files_to_pack.append((v2_path, f'{ds_label}/{_V2_REPORT_NAME}'))
            reports_dir = os.path.join(output_dir, 'reports')
            if os.path.isdir(reports_dir):
                for root, _, files in os.walk(reports_dir):
                    for f in files:
                        full = os.path.join(root, f)
                        arcname = os.path.join(ds_label, 'reports',
                                               os.path.relpath(full, reports_dir))
                        files_to_pack.append((full, arcname))

    with tarfile.open(archive_name, 'w:gz') as tar:
        for src_path, arc_name in files_to_pack:
            if os.path.exists(src_path):
                tar.add(src_path, arcname=arc_name)

    print(f"\n  📦 结果打包完成: {archive_name}")
    print(f"     包含 {len(files_to_pack)} 个文件")
    return archive_name


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description='evalscope 通用效果评测脚本 - 支持指定数据集、温度、并发，带预检查',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
已注册的数据集:
{chr(10).join(f'  {k:40s} - {v["description"]}' for k, v in SUPPORTED_DATASETS.items())}
  all                                     - 运行以上全部数据集

示例:
  python3 run_eval.py --datasets aime25 --model_name glm-5.2 --base_url https://... --api_key sk-xxx
  python3 run_eval.py --datasets all --model_name glm-5.2 --base_url https://... --api_key sk-xxx
  python3 run_eval.py --list-datasets
        """
    )

    parser.add_argument('--datasets', type=str, default=os.environ.get('datasets', ''),
                        help=f'数据集名称，逗号分隔。已注册: {", ".join(SUPPORTED_DATASETS.keys())}, all')
    parser.add_argument('--model_name', default=os.environ.get('MODEL_NAME', ''), type=str,
                        help='模型名称')
    parser.add_argument('--provider', default=os.environ.get('PROVIDER', 'unknown'), type=str,
                        help='供应商名称')
    parser.add_argument('--base_url', default=os.environ.get('API_URL', ''), type=str,
                        help='API base URL')
    parser.add_argument('--api_key', default=os.environ.get('API_KEY', ''), type=str,
                        help='API Key')
    parser.add_argument('--list-datasets', action='store_true',
                        help='列出支持的数据集并退出')

    parser.add_argument('--temperature', default=None, type=float,
                        help='生成温度 (0.0 ~ 2.0)')
    parser.add_argument('--top_p', default=None, type=float,
                        help='核采样 top_p (0.0 ~ 1.0)，不传则用服务端默认值')
    parser.add_argument('--eval_batch_size', default=None, type=int, help='并发数')
    parser.add_argument('--repeats', default=None, type=int, help='重复次数/pass@k')
    parser.add_argument('--limit', default=None, type=int, help='最多跑多少条')
    parser.add_argument('--max_tokens', default=None, type=int, help='最大输出 token 数')
    parser.add_argument('--provider_override', default=os.environ.get('PROVIDER_OVERRIDE', ''),
                        type=str, help='供应商路由覆盖（注入 X-Provider-Override 请求头），留空不注入')

    parser.add_argument('--ignore_errors', default=True,
                        type=lambda x: x.lower() not in ('false', '0', 'no', 'off', 'disabled'),
                        help='是否忽略评测过程中的错误样本（跳过失败样本继续），默认开启')
    parser.add_argument('--thinking', default=True,
                        type=lambda x: x.lower() not in ('false', '0', 'no', 'off', 'disabled'),
                        help='是否开启思考模式，默认开启')
    parser.add_argument('--thinking-type', dest='thinking_type', default=None, type=str,
                        help='thinking.type 的具体值（如 enabled / adaptive / disabled）。'
                             '默认按 provider/model 自动映射：minimax → adaptive，其余 → enabled')
    parser.add_argument('--judge_strategy', default=None, type=str,
                        choices=['rule', 'llm', 'llm_recall', 'auto'],
                        help='Judge 策略')

    parser.add_argument('--judge_model',
                        default=os.environ.get('JUDGE_MODEL', 'deepseek-v4-pro'), type=str,
                        help='Judge 模型名称')
    parser.add_argument('--judge_base_url',
                        default=os.environ.get('JUDGE_BASE_URL', 'https://api.deepseek.com/v1'),
                        type=str, help='Judge 模型 API base URL')
    parser.add_argument('--judge_api_key',
                        default=os.environ.get('JUDGE_API_KEY', ''), type=str,
                        help='Judge 模型 API Key')
    parser.add_argument('--include_multi_modal', default=False,
                        type=lambda x: x.lower() in ('true', '1', 'yes', 'on', 'enabled'),
                        help='HLE 是否包含多模态题目')

    parser.add_argument('--subset_list', default=None, type=str,
                        help='tau2_bench domain: airline,retail,telecom')
    parser.add_argument('--mmlu_pro_subset', default=None, type=str,
                        help='mmlu_pro 学科子集')
    parser.add_argument('--longbench_v2_subset', default=None, type=str,
                        help='longbench_v2 长度子集: short,medium,long')
    parser.add_argument('--live_code_bench_subset', default=None, type=str,
                        help='live_code_bench release 子集')
    parser.add_argument('--live_code_bench_start_date', default=None, type=str)
    parser.add_argument('--live_code_bench_end_date', default=None, type=str)

    parser.add_argument('--sandbox', default=None, type=str,
                        help='Docker Sandbox 配置（JSON）')
    parser.add_argument('--dataset_dir', default=os.environ.get('EVAL_DATASET_DIR'), type=str,
                        help='本地数据集缓存目录，默认 eval/datasets')
    parser.add_argument('--use_cache', default=None, type=str, help='缓存续跑路径')
    parser.add_argument('--output_dir', default=None, type=str, help='输出目录')
    parser.add_argument('--debug', action='store_true', help='开启 debug 模式')

    return parser.parse_args()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _print_banner(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def main():
    args = parse_args()

    # --list-datasets
    if args.list_datasets:
        print(f"\n支持的数据集（{len(SUPPORTED_DATASETS)} 个）:\n")
        for k, v in SUPPORTED_DATASETS.items():
            judge = " [需 Judge]" if v.get('requires_judge_model') else ""
            docker = " [需 Docker]" if v.get('requires_docker') else ""
            print(f"  {k:40s} - {v['description']}{judge}{docker}")
        print(f"\n  all                                     - 运行以上全部数据集")
        return

    # 预检查
    _print_banner("🔍 预检查")
    errors = pre_check(args)
    if errors:
        print(f"\n❌ 预检查失败，发现 {len(errors)} 个错误:\n")
        for i, err in enumerate(errors, 1):
            print(f"  [{i}] {err}")
        print(f"\n💡 请修正以上错误后重新运行。\n")
        sys.exit(1)

    known_datasets, unknown_datasets = resolve_datasets(args.datasets)
    all_datasets = known_datasets + unknown_datasets

    # 一次 run 统一一个 run_dir：所有数据集 + 汇总产物落同一目录，
    # 保证 eval_summary / all_eval_summary / tar.gz / HTML 可被单一 run 目录圈定，
    # 避免 eval/results/*/*/eval_summary.json 捞到其他历史 run 的结果。
    run_dir = args.output_dir or make_run_dir(args.provider, args.model_name)

    # 确定结果根目录（run 目录本身，即所有产物的共同父目录）
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    result_base = run_dir if args.output_dir else os.path.normpath(
        os.path.join(scripts_dir, '..', 'results', run_dir)
    )
    overall_start_time = datetime.now()

    # 打印配置
    _print_banner("🔍 预检查")
    print(f"  ✅ evalscope 环境就绪")
    print(f"  ✅ 模型:         {args.model_name}")
    print(f"  ✅ Base URL:     {args.base_url}")
    print(f"  ✅ 思考模式:     {args.thinking}")
    _thinking_type_preview = resolve_thinking_type(
        args.provider, args.model_name, args.thinking,
        getattr(args, "thinking_type", None),
    )
    if _thinking_type_preview:
        print(f"  ✅ thinking.type: {_thinking_type_preview}")
    print(f"  ✅ 忽略错误样本: {args.ignore_errors}")
    if args.provider_override:
        print(f"  ✅ Provider Override: {args.provider_override}")
    if args.temperature is not None:
        print(f"  ✅ Temperature:  {args.temperature}")
    if args.top_p is not None:
        print(f"  ✅ Top P:        {args.top_p}")
    if args.max_tokens:
        print(f"  ✅ Max Tokens:   {args.max_tokens}（手动指定）")
    else:
        print(f"  ✅ Max Tokens:   {get_max_tokens(args.model_name, args.thinking)}（自动）")
    if known_datasets:
        print(f"  ✅ 已知数据集:   {', '.join(known_datasets)}")
    if unknown_datasets:
        print(f"  ⚠️  外部数据集:   {', '.join(unknown_datasets)}")
    print()

    # 执行评测
    all_results = {}
    all_summaries = []

    for ds_key in all_datasets:
        ds_config = get_dataset_config(ds_key)

        _print_banner(f"📊 [{ds_config['label']}] {ds_config['description']}")

        max_tok = args.max_tokens or get_max_tokens(args.model_name, args.thinking)
        repeats = args.repeats or ds_config['default_repeats']
        ebs = args.eval_batch_size or ds_config['default_eval_batch_size']
        judge = args.judge_strategy or ds_config['judge_strategy']

        print(f"  数据集:     {ds_config['label']}")
        print(f"  Judge策略:  {judge}")
        print(f"  MaxTokens:  {max_tok}")
        print(f"  Repeats:    {repeats}")
        print(f"  并发数:     {ebs}")
        if ds_key in ('hle', 'simple_qa'):
            print(f"  Judge模型:  {args.judge_model}")
        if ds_key == 'tau2_bench' and args.subset_list:
            print(f"  Domain:     {args.subset_list}")
        print(f"  开始时间:   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        cmd, output_dir = build_eval_command(args, ds_key, run_dir)
        print(f"\n  执行命令:\n  {' '.join(cmd)}\n")

        overhead_start = datetime.now()
        success, final_output_dir, retries, rate_limited = _run_with_retry(
            cmd, output_dir, args.model_name, ds_config['label']
        )
        elapsed = (datetime.now() - overhead_start).total_seconds()

        all_results[ds_key] = {
            'success': success,
            'output_dir': final_output_dir,
            'retries': retries,
            'rate_limited': rate_limited,
        }

        if success:
            print(f"  结果目录: {final_output_dir}")

        # 读取 evalscope 原生报告
        report_path = os.path.join(final_output_dir, 'reports', args.model_name,
                                   f'{ds_config["label"]}.json')
        report_data = None
        # fallback: 精确路径不存在时，在 reports/ 下递归搜索同名 JSON
        if not os.path.exists(report_path):
            reports_dir = os.path.join(final_output_dir, 'reports')
            if os.path.isdir(reports_dir):
                target_name = f'{ds_config["label"]}.json'
                for root, _, files in os.walk(reports_dir):
                    if target_name in files:
                        report_path = os.path.join(root, target_name)
                        break
        if os.path.exists(report_path):
            try:
                with open(report_path, 'r') as f:
                    report_data = json.load(f)
                print(f"\n  结果摘要:")
                for key, value in report_data.items():
                    if key in _SUMMARY_DROP_KEYS:
                        continue  # 长文本（数据集描述/模板）不刷屏
                    if isinstance(value, dict):
                        for k, v in value.items():
                            print(f"    {key}.{k}: {v}")
                    else:
                        print(f"    {key}: {value}")
            except Exception as e:
                print(f"\n  ⚠️ 报告读取异常: {e}")
        else:
            print(f"\n  ⚠️ 未找到 evalscope 报告: {report_path}")

        # 生成 v2 效果评测报告（六章模版，对外唯一报告形态）
        v2_info = _generate_v2_report(final_output_dir, args.model_name, success)

        summary = _save_dataset_summary(
            ds_key, ds_config['label'], final_output_dir,
            success, report_data, retries, rate_limited, elapsed,
            skipped_count=v2_info['skipped'],
        )
        all_summaries.append(summary)

    # 汇总 & 生成报告 & 打包
    total_elapsed = (datetime.now() - overall_start_time).total_seconds()

    _print_banner("📋 评测汇总")
    success_list = []
    fail_list = []
    for ds_key in all_datasets:
        r = all_results.get(ds_key, {})
        status = "✅" if r.get('success') else "❌"
        ds_label = get_dataset_config(ds_key)['label']
        retry_info = f" (重试{r['retries']}次)" if r.get('retries', 0) > 0 else ""
        limit_info = " [限流]" if r.get('rate_limited') else ""
        print(f"  {status} [{ds_label}]{retry_info}{limit_info}  {r.get('output_dir', 'N/A')}")
        if r.get('success'):
            success_list.append(ds_label)
        else:
            fail_list.append(ds_label)

    print(f"\n  成功: {len(success_list)}/{len(all_datasets)}")
    print(f"  总耗时: {_format_duration(total_elapsed)}")
    if fail_list:
        print(f"  失败: {', '.join(fail_list)}")

    _save_overall_summary(all_summaries, args.provider, args.model_name, result_base)
    # 按数据集生成产物：在每个数据集的 output_dir 下生成该数据集独立的
    # per_sample_details.csv（六章模版报告 eval_report_v2.html 已在单数据集执行阶段生成）。
    # 数据集级不再重复生成 all_eval_summary.json / eval_results.tar.gz——
    # 汇总只留根目录一份，打包只留 run 根一份，避免内容重复的产物。
    # 必须早于父级打包 —— per_sample_details.csv 在这里才生成，否则进不了父级 tar.gz。
    ds_artifacts = _export_per_dataset_artifacts(all_summaries)
    _package_results(result_base, None, all_summaries)
    # 打包完成后清理已提炼冗余的原始产物（predictions/reviews/reports）
    _cleanup_redundant_artifacts(all_summaries)

    if fail_list:
        sys.exit(1)
    else:
        print(f"\n  全部通过！")


def _export_per_dataset_artifacts(all_summaries):
    """在每个数据集的 output_dir 下生成该数据集独立的明细产物。

    v2 报告（eval_report_v2.html）已在单数据集执行阶段生成，这里补齐逐题明细：
      eval/results/<run>/
      ├── all_eval_summary.json           # 总汇总（仅 run 根一份）
      ├── eval_results.tar.gz             # 打包归档（仅 run 根一份）
      ├── aime26/
      │   ├── eval_summary.json           # 数据集摘要
      │   ├── eval_report_v2.html         # 六章模版报告（执行阶段已生成）
      │   ├── per_sample_details.csv      # 逐题明细
      │   ├── logs/  configs/             # evalscope 原始日志与配置
      │   └── （predictions/ reviews/ reports/ 打包后被清理，见 _cleanup_redundant_artifacts）
      └── hle/...

    这样流水线用 eval/results/*/aime26/ 即可精确捞 aime26 产物，
    eval/results/*/hle/ 精确捞 hle，不会串到其他数据集。
    """
    generated = []
    for s in all_summaries:
        ds_label = s.get('dataset', 'unknown')
        output_dir = s.get('output_dir', '')
        if not output_dir or not os.path.isdir(output_dir):
            continue

        # 逐题明细必须生成在打包之前，否则进不了 tar.gz
        _export_per_sample_details(output_dir)

        generated.append(ds_label)
        print(f"  📁 [{ds_label}] 数据集级产物已生成: {output_dir}/")

    return generated


def _cleanup_redundant_artifacts(all_summaries):
    """评测收尾时清理已被提炼覆盖的原始产物，大幅缩减 run 目录体积。

    - predictions/  模型原始预测（thinking 长文本，通常占产物 60%+），
      其"模型回复"已完整提炼进 per_sample_details.csv 的 response 列；
    - reviews/      逐题评判原始行，得分与题目已提炼进同一 CSV；
    - reports/      evalscope 原生 HTML/JSON 报告，与 eval_report_v2.html
      及 eval_summary.json 内容重复。

    三者均已随 eval_results.tar.gz 归档，散目录里保留只会放大体积。
    失败的数据集不做清理，保留原始产物便于排障。
    """
    freed = 0
    for s in all_summaries:
        if not s.get('success'):
            continue
        output_dir = s.get('output_dir', '')
        if not output_dir or not os.path.isdir(output_dir):
            continue
        for name in ('predictions', 'reviews', 'reports'):
            target = os.path.join(output_dir, name)
            if not os.path.isdir(target):
                continue
            for root, _, files in os.walk(target):
                for f in files:
                    try:
                        freed += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            shutil.rmtree(target, ignore_errors=True)
    if freed:
        print(f"\n  🧹 已清理 predictions/reviews/reports 冗余原始产物，"
              f"释放 {freed / 1024 / 1024:.1f} MB（均已归档于 eval_results.tar.gz）")


def _deep_find_score(obj, depth=0):
    """递归查找得分数值：优先 acc，其次 score/reward/correct/pass 等数值字段。"""
    if depth > 6 or obj is None:
        return None
    if isinstance(obj, dict):
        # 常见得分字段
        for key in ('acc', 'accuracy', 'reward', 'correct', 'pass', 'pass@1', 'score', 'value'):
            v = obj.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        # 嵌套 value 对象（如 {"value": {"acc": 1.0}}）
        v = obj.get('value')
        if isinstance(v, dict):
            s = _deep_find_score(v, depth + 1)
            if s is not None:
                return s
        for v in obj.values():
            s = _deep_find_score(v, depth + 1)
            if s is not None:
                return s
    elif isinstance(obj, list):
        for v in obj:
            s = _deep_find_score(v, depth + 1)
            if s is not None:
                return s
    return None


def _extract_request(review_row):
    """从 review 提取题目/请求内容（尽量可读的字符串）。"""
    try:
        sm = review_row.get('sample_metadata') or {}
        # 1) 用户场景指令（agent 类数据集）
        us = sm.get('user_scenario') or {}
        if isinstance(us, dict):
            for key in ('task_instructions', 'instructions', 'reason_for_call', 'question', 'input'):
                if us.get(key):
                    return str(us[key])
        # 2) messages 首条 user 的完整题目（优先于 target 编号）
        for m in review_row.get('messages', []) or []:
            content = m.get('content')
            if m.get('role') == 'user' and content:
                return str(content) if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
        # 3) 描述 purpose
        desc = sm.get('description') or {}
        if isinstance(desc, dict) and desc.get('purpose'):
            return str(desc['purpose'])
        # 4) target 字段（原题/编号）
        if review_row.get('target'):
            return str(review_row['target'])
        # 5) 兜底
        if sm:
            return json.dumps(sm, ensure_ascii=False, default=str)
        return ''
    except Exception:
        return ''


def _extract_response(review_row, pred_row):
    """从 review + prediction 提取模型回复（尽量可读的最终回复文本）。

    优先顺序：
      1. prediction.model_output.choices[0].message.content（若为 JSON 字符串则解出最终 assistant 文本）
      2. review.sample_score.score.extracted_prediction
      3. review.output
      4. review.agent_trace 中最后一条 assistant 的纯文本
      5. review.messages 最后一条 assistant 的纯文本
    """
    try:
        def _content_to_text(content):
            """把 content 归一化为可读文本：若是 JSON 字符串，尝试提取最后一条 assistant 文本。"""
            if not content:
                return ''
            if isinstance(content, str):
                # 尝试解析为 JSON（agent 轨迹/对象序列化）
                stripped = content.strip()
                if stripped.startswith('{') or stripped.startswith('['):
                    try:
                        parsed = json.loads(stripped)
                        return _extract_final_assistant_text(parsed) or stripped
                    except json.JSONDecodeError:
                        pass
                return stripped
            return _extract_final_assistant_text(content) or json.dumps(content, ensure_ascii=False, default=str)

        ss = review_row.get('sample_score') or {}
        score_obj = ss.get('score') or {}
        # 1) review 的 extracted_prediction（模型的"最终答案"，数学/代码/选项类最有用）
        if score_obj.get('extracted_prediction'):
            return _content_to_text(score_obj['extracted_prediction'])
        # 2) review 的 output
        if review_row.get('output'):
            return _content_to_text(review_row['output'])
        # 3) prediction 的 model_output（最终 assistant 文本）
        if pred_row:
            mo = pred_row.get('model_output') or {}
            try:
                choices = mo.get('choices') or []
                if choices and isinstance(choices[0], dict):
                    msg = choices[0].get('message') or {}
                    if msg.get('content'):
                        return _content_to_text(msg['content'])
            except Exception:
                pass
            if mo.get('content'):
                return _content_to_text(mo['content'])
        # 4) review 的 agent_trace
        if review_row.get('agent_trace'):
            final = _extract_final_assistant_text(review_row['agent_trace'])
            if final:
                return final
        # 5) review 的 messages 最后一条 assistant
        final = _extract_final_assistant_text(review_row.get('messages'))
        if final:
            return final
        return ''
    except Exception:
        return ''


def _extract_final_assistant_text(obj):
    """从可能嵌套的结构中提取最后一条 assistant 的纯文本 content。"""
    try:
        if isinstance(obj, dict):
            if obj.get('role') == 'assistant' and obj.get('content'):
                c = obj['content']
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    texts = [p.get('text') for p in c if isinstance(p, dict) and p.get('text')]
                    if texts:
                        return '\n'.join(texts)
                return json.dumps(c, ensure_ascii=False, default=str)
            # 递归找最后一条 assistant
            for key in ('messages', 'agent_trace', 'message', 'conversation'):
                if isinstance(obj.get(key), (list, dict)):
                    r = _extract_final_assistant_text(obj[key])
                    if r:
                        return r
            for v in reversed(list(obj.values())):
                if isinstance(v, (list, dict)):
                    r = _extract_final_assistant_text(v)
                    if r:
                        return r
        elif isinstance(obj, list):
            for item in reversed(obj):
                if isinstance(item, (list, dict)):
                    r = _extract_final_assistant_text(item)
                    if r:
                        return r
    except Exception:
        pass
    return ''


def _export_per_sample_details(output_dir):
    """从 reviews/ 和 predictions/ 提取每道题的 得分/request/response，生成 per_sample_details.csv。

    产物：<dataset>/per_sample_details.csv
      columns: sample_id, score, request, response

    数据来源：
      - reviews/<model>/*.jsonl  每行一道题：sample_score（得分）、sample_metadata（题目）
      - predictions/<model>/*.jsonl  每行：model_output（模型回复）

    兼容不同数据集字段差异（acc/reward/pass 等得分字段、target/messages 等题目字段）。
    """
    import glob as _glob

    reviews_dir = os.path.join(output_dir, 'reviews')
    predictions_dir = os.path.join(output_dir, 'predictions')
    if not os.path.isdir(reviews_dir):
        return

    # 收集所有 review 文件（reviews/<model>/*.jsonl 或 reviews/*.jsonl）
    review_files = sorted(_glob.glob(os.path.join(reviews_dir, '**', '*.jsonl'), recursive=True))
    if not review_files:
        return

    # 收集 prediction 文件并建立 index -> row 索引（支持多个模型目录合并）
    pred_by_idx = {}
    if os.path.isdir(predictions_dir):
        for pf in sorted(_glob.glob(os.path.join(predictions_dir, '**', '*.jsonl'), recursive=True)):
            try:
                with open(pf, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                            idx = row.get('index')
                            if idx is not None:
                                pred_by_idx[str(idx)] = row
                        except json.JSONDecodeError:
                            continue
            except Exception:
                continue

    # 防止超长 response 撑爆 CSV 字段上限
    try:
        csv.field_size_limit(2 ** 26)  # 64MB 字段上限
    except Exception:
        pass

    # 单条 response/request 最长保留字符数（超出截断，保持 CSV 可读可控）
    MAX_TEXT_LEN = 20000

    # 写 CSV（UTF-8 带 BOM，Excel 直接打开不乱码）
    csv_path = os.path.join(output_dir, 'per_sample_details.csv')
    rows_written = 0
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['sample_id', 'score', 'request', 'response'])
        for rf in review_files:
            try:
                with open(rf, 'r', encoding='utf-8') as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        # sample_id
                        ss = row.get('sample_score') or {}
                        sample_id = str(ss.get('sample_id', row.get('index', '')))
                        # score
                        score = _deep_find_score(ss.get('score'))
                        score_str = f"{score:.4f}" if score is not None else ''
                        # request / response（截断超长字段）
                        request = _extract_request(row)[:MAX_TEXT_LEN]
                        response = _extract_response(row, pred_by_idx.get(str(sample_id)) or pred_by_idx.get(str(row.get('index'))))[:MAX_TEXT_LEN]
                        writer.writerow([sample_id, score_str, request, response])
                        rows_written += 1
            except Exception:
                continue

    if rows_written:
        print(f"  📄 逐题明细已生成: {csv_path}  ({rows_written} 道题)")
    else:
        # 无数据时删除空 csv
        try:
            os.remove(csv_path)
        except OSError:
            pass


if __name__ == '__main__':
    main()
