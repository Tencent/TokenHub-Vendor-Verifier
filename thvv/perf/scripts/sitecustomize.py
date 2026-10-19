"""Python 启动自动加载（sitecustomize）——兼容性补丁。

修复 evalscope 在 transformers >= 5.x 下的 token 计数错误：

transformers 5.x 的 ``apply_chat_template(tokenize=True)`` 返回值从旧的
``List[int]`` 变成了 ``BatchEncoding``（dict-like，含 input_ids / attention_mask
等键）。旧版 evalscope 的 ``tokenize_chat_messages`` 直接对其 ``len()``，得到的是
键数（2）而非 token 数，导致 ``check_prompt_length`` 把全部 prompt 过滤掉，最终
报 ``Dataset "custom_multi_turn" produced no conversations!``。

本模块在 evalscope 导入后，把 ``tokenize_chat_messages`` 替换为对返回值做归一化
的版本，始终返回平坦的 ``List[int]``。对未安装 evalscope 的环境静默跳过，无副作用。
"""
try:
    import evalscope.perf.plugin.datasets.utils as _u
except Exception:  # 无 evalscope 的环境，跳过
    pass
else:

    def _normalize(result):
        """把 apply_chat_template 的返回值归一化为平坦的 List[int]；无法识别返回 None。"""
        if isinstance(result, dict) and 'input_ids' in result:
            ids = result['input_ids']
        elif hasattr(result, 'input_ids'):
            ids = result.input_ids
        elif isinstance(result, list):
            ids = result
        else:
            return None
        # tensor -> list
        ids = ids.tolist() if hasattr(ids, 'tolist') else list(ids)
        # 去掉意外多出的 batch 维度：[[ids]] -> [ids]
        if ids and isinstance(ids[0], list):
            return ids[0]
        return ids

    _orig = _u.tokenize_chat_messages

    def _patched(tokenizer, messages, add_generation_prompt=True):
        result = tokenizer.apply_chat_template(
            messages, tokenize=True, add_generation_prompt=add_generation_prompt
        )
        normalized = _normalize(result)
        if normalized is not None:
            return normalized
        # 兜底：交给原实现处理
        return _orig(tokenizer, messages, add_generation_prompt)

    _u.tokenize_chat_messages = _patched
