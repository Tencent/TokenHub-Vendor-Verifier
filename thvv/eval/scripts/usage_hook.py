# encoding:utf-8
"""evalscope 启动器：在不修改 evalscope 源码的前提下，逐次记录每个 Chat 请求的原始 usage。

用法（由 run_eval.py 自动调用，无需手动执行）：
    THVV_USAGE_LEDGER=<work_dir>/usage_ledger.jsonl \
        python3 usage_hook.py eval --model ... --api-url ... [evalscope eval 的全部参数]

原理：
  evalscope 1.9.0 的 OpenAICompatibleAPI.generate / generate_async 在拿到完整 completion
  （流式时为聚合了末帧 usage 的 ChatCompletion）后调用 self.on_response(completion.model_dump())。
  这里包装 generate / generate_async 记录请求上下文（消息条数、工具数），
  再包装 on_response 把原始 usage 追加写入 ledger（一行一次调用，JSONL）。

  另外包装 DefaultEvaluator._process_work_item，在处理每道题时记下题目归属
  （benchmark / subset / sample_id），以及被测模型实例的 id，用来区分：
    - role=subject：被测模型本身发出的调用（答题 / Agent 轮次）；
    - role=aux：同一道题里其他模型实例发出的调用（tau2 用户模拟器、Judge 等）。
  这样报告能按题目给出缓存命中率。

  - 覆盖所有经由 OpenAICompatibleAPI 发出的调用：单轮题、tau2 agent 与用户模拟器的每一轮、
    SWE agentic 的每一步、Judge 调用（汇总时按被测模型名过滤掉）。
  - 只记录 usage 与少量元数据，不记录请求/回复正文。
  - 任何记录异常都被吞掉，绝不影响评测主流程。
"""
import contextvars
import json
import os
import sys
import threading
import time

LEDGER_ENV = 'THVV_USAGE_LEDGER'
LEDGER_SCHEMA = 2

_call_ctx = contextvars.ContextVar('thvv_usage_call_ctx', default=None)
_sample_ctx = contextvars.ContextVar('thvv_usage_sample_ctx', default=None)
_write_lock = threading.Lock()


def _append(path, record):
    line = json.dumps(record, ensure_ascii=False, default=str) + '\n'
    with _write_lock:
        # 每次以 append 方式打开：多线程靠锁、多进程靠 O_APPEND 单行写
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line)


def _install_sample_context():
    """包装 DefaultEvaluator._process_work_item，记录当前题目归属。失败不影响 usage 记录。"""
    try:
        from evalscope.evaluator.evaluator import DefaultEvaluator
    except Exception as e:  # pragma: no cover
        sys.stderr.write(f'[thvv usage_hook] 未能加载 DefaultEvaluator，按题归属不可用: {e}\n')
        return
    if getattr(DefaultEvaluator, '_thvv_sample_patched', False):
        return
    orig = DefaultEvaluator._process_work_item

    def _process_work_item(self, item, *args, **kwargs):
        ctx = None
        try:
            sample = getattr(item, 'sample', None)
            model = getattr(self, 'model', None)
            ctx = {
                'benchmark': getattr(self, 'benchmark_name', None),
                'subset': getattr(item, 'subset', None),
                'sample_id': getattr(sample, 'id', None),
                'group_id': getattr(sample, 'group_id', None),
                'subject_api_id': id(getattr(model, 'api', None)) if model is not None else None,
            }
        except Exception:
            ctx = None
        token = _sample_ctx.set(ctx)
        try:
            return orig(self, item, *args, **kwargs)
        finally:
            _sample_ctx.reset(token)

    DefaultEvaluator._process_work_item = _process_work_item
    DefaultEvaluator._thvv_sample_patched = True


def install(ledger_path):
    """给 OpenAICompatibleAPI 打补丁。成功返回 True。"""
    try:
        from evalscope.models.openai_compatible import OpenAICompatibleAPI
    except Exception as e:  # pragma: no cover
        sys.stderr.write(f'[thvv usage_hook] 未能加载 OpenAICompatibleAPI，跳过 usage 记录: {e}\n')
        return False

    if getattr(OpenAICompatibleAPI, '_thvv_usage_patched', False):
        return True

    os.makedirs(os.path.dirname(os.path.abspath(ledger_path)), exist_ok=True)
    _install_sample_context()

    orig_generate = OpenAICompatibleAPI.generate
    orig_generate_async = OpenAICompatibleAPI.generate_async
    orig_on_response = OpenAICompatibleAPI.on_response

    def _ctx(input, tools):
        try:
            return {'n_messages': len(input or []), 'n_tools': len(tools or [])}
        except Exception:
            return {'n_messages': None, 'n_tools': None}

    def generate(self, input, tools, tool_choice, config):
        token = _call_ctx.set(_ctx(input, tools))
        try:
            return orig_generate(self, input, tools, tool_choice, config)
        finally:
            _call_ctx.reset(token)

    async def generate_async(self, input, tools, tool_choice, config):
        token = _call_ctx.set(_ctx(input, tools))
        try:
            return await orig_generate_async(self, input, tools, tool_choice, config)
        finally:
            _call_ctx.reset(token)

    def on_response(self, response):
        try:
            ctx = _call_ctx.get() or {}
            sctx = _sample_ctx.get() or {}
            usage = response.get('usage') if isinstance(response, dict) else None
            role = None
            if sctx.get('subject_api_id') is not None:
                role = 'subject' if id(self) == sctx['subject_api_id'] else 'aux'
            _append(ledger_path, {
                'v': LEDGER_SCHEMA,
                'ts': round(time.time(), 3),
                'pid': os.getpid(),
                'api': type(self).__name__,
                'model': getattr(self, 'model_name', None),
                'base_url': getattr(self, 'base_url', None),
                'response_model': response.get('model') if isinstance(response, dict) else None,
                'benchmark': sctx.get('benchmark'),
                'subset': sctx.get('subset'),
                'sample_id': sctx.get('sample_id'),
                'group_id': sctx.get('group_id'),
                'role': role,
                'n_messages': ctx.get('n_messages'),
                'n_tools': ctx.get('n_tools'),
                'usage': usage,
            })
        except Exception:
            pass
        return orig_on_response(self, response)

    OpenAICompatibleAPI.generate = generate
    OpenAICompatibleAPI.generate_async = generate_async
    OpenAICompatibleAPI.on_response = on_response
    OpenAICompatibleAPI._thvv_usage_patched = True
    return True


def main():
    ledger = os.environ.get(LEDGER_ENV)
    if ledger:
        install(ledger)
    from evalscope.cli.cli import run_cmd
    sys.argv = ['evalscope'] + sys.argv[1:]
    run_cmd()


if __name__ == '__main__':
    main()
