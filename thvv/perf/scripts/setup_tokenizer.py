#!/usr/bin/env python3
"""下载/校验 tokenizer。"""

import os
import sys

TOKENIZER_NAME = os.environ.get("TOKENIZER", "zai-org/GLM-4.6")


def _download_and_verify():
    """下载 tokenizer 并验证可加载（local_files_only=True）。不依赖手动 cache 路径探测。"""
    print(f"[info] 开始下载 {TOKENIZER_NAME} …")
    for mod_name in ("modelscope", "transformers"):
        try:
            mod = __import__(mod_name)
            tok = mod.AutoTokenizer.from_pretrained(TOKENIZER_NAME, trust_remote_code=True)
            # 验证下载后确实可加载（local_files_only=True）
            _ = mod.AutoTokenizer.from_pretrained(TOKENIZER_NAME, trust_remote_code=True, local_files_only=True)
            print(f"[OK] {TOKENIZER_NAME} 下载完成并验证通过 (via {mod_name})")
            return True
        except Exception as e:
            pass
    print(f"[fail] 下载 tokenizer 失败，请检查网络")
    return False


def main():
    print(f"[tokenizer] {TOKENIZER_NAME}")
    if not _download_and_verify():
        sys.exit(1)


if __name__ == "__main__":
    main()
