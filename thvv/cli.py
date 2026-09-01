#!/usr/bin/env python3
"""THVV — 统一 CLI 入口。

用法:
  thvv perf bench 1k 20 1
  thvv perf bench-all
  thvv perf report
  thvv eval bench aime25
  thvv eval list
  thvv check
  thvv install

配置方式（优先级: CLI 参数 > 环境变量 > .env 文件）:
  1. thvv/configs/.env  — 写入 API_URL / API_KEY / MODEL_NAME / TOKENIZER / PROTOCOL
  2. 环境变量            — export API_URL=... API_KEY=... MODEL_NAME=...
  3. CLI 参数            — (perf/eval 参数经由 quickstart.sh 透传)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _PKG_DIR.parent


def _load_env() -> None:
    """加载 thvv/configs/.env 到环境变量（不覆盖已有的环境变量）"""
    env_file = _PKG_DIR / "configs" / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _run_subcommand(cmd: str, args: list[str]) -> int:
    """perf / eval — 委托给 quickstart.sh

    对 perf 和 eval 做参数简化：
      thvv perf 1k 20 1   → quickstart.sh perf bench 1k 20 1
      thvv perf all       → quickstart.sh perf bench-all
      thvv perf report    → quickstart.sh perf report
      thvv eval aime25    → quickstart.sh eval bench aime25
      thvv eval list      → quickstart.sh eval list
    """
    if cmd == "perf" and args:
        if args[0] in ("bench", "bench-all", "report", "check"):
            pass  # 原样透传
        elif args[0] == "all":
            args = ["bench-all"] + args[1:]
        else:
            args = ["bench"] + args
    elif cmd == "eval" and args:
        if args[0] not in ("bench", "list"):
            args = ["bench"] + args
    quickstart = _PKG_DIR / "quickstart.sh"
    if not quickstart.exists():
        print(f"错误: {quickstart} 不存在", file=sys.stderr)
        return 1
    env = dict(os.environ)
    return subprocess.call(["bash", str(quickstart), cmd, *args], env=env)


def _check() -> int:
    """环境检查"""
    print("=== 环境检查 ===")
    import shutil

    print(f"  Python: {sys.version.split()[0]}")

    evalscope = shutil.which("evalscope")
    print(f"  evalscope (perf/eval): {'✓ ' + evalscope if evalscope else '✗ 未安装'}")

    # .env 文件
    env_file = _PKG_DIR / "configs" / ".env"
    if env_file.exists():
        print("  configs/.env: ✓ 已配置")
    else:
        example = _PKG_DIR / "configs" / "env.example"
        print("  configs/.env: ✗ 未配置")
        if example.exists():
            print(f"    请运行: cp {example} {env_file}")

    # 环境变量
    print("\n--- 环境变量 ---")
    for var in ("API_URL", "API_KEY", "MODEL_NAME", "PROTOCOL", "TOKENIZER"):
        val = os.environ.get(var, "")
        if val:
            display = val[:8] + "..." if var == "API_KEY" and len(val) > 12 else val
            print(f"  {var}: ✓ {display}")
        else:
            print(f"  {var}: —")

    return 0


def _help() -> int:
    print("""THVV — TokenHub Vendor Verifier

用法: thvv <command> [args...]

Commands:
  perf     性能压测 — 7 档中文输入 (1k~200k)
           thvv perf <bucket> [N] [P]          单档压测 (如 thvv perf 1k 20 1)
           thvv perf all                        全档矩阵
           thvv perf report                     生成报告

  eval     效果评测 — 11 个数据集 (AIME/GPQA/HLE/MMLU-Pro/...)
           thvv eval <datasets>                 评测 (如 thvv eval aime25)
           thvv eval list                       列出数据集

  check    环境检查
  install  安装依赖

配置（优先级: CLI 参数 > 环境变量 > .env 文件）:
  cp thvv/configs/env.example thvv/configs/.env
  # 或 export API_URL=... API_KEY=... MODEL_NAME=...
""")
    return 0


def main(argv: list[str] | None = None) -> int:
    # 启动时加载 .env（不影响已有环境变量）
    _load_env()

    args = argv if argv is not None else sys.argv[1:]

    if not args or args[0] in ("-h", "--help", "help"):
        return _help()

    cmd = args[0]
    rest = args[1:]

    if cmd in ("perf", "eval"):
        return _run_subcommand(cmd, rest)
    elif cmd == "check":
        return _check()
    elif cmd == "install":
        return _run_subcommand("install", rest)
    else:
        print(f"未知命令: {cmd}")
        print("可用命令: perf, eval, check, install")
        print("详细用法: thvv --help")
        return 1


if __name__ == "__main__":
    sys.exit(main())
