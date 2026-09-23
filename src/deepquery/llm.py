"""LLM 客户端：OpenAI 兼容接口 + 用量记账 + 指数退避重试。

约定：任何调用方必须传入本次运行的 UsageMeter，调用前做预算检查，
调用后记账——预算熔断因此对"每一次"模型调用都生效。
MockLLM 与真实客户端同接口，供离线测试与评测基建自检使用。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from .budget import RunCancelled, UsageMeter
from .config import Settings


@dataclass
class LLMReply:
    text: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class LLMError(RuntimeError):
    """重试耗尽后的最终失败。"""


def estimate_tokens(texts) -> int:
    """无 usage 时的保守 token 估算：中日韩字符约 1 字 1 token，其余约 4 字符 1 token。

    只按 4 字符/token 估算会把中文低估 2–4 倍，预算熔断和每日花费上限就会跟着失准。
    """
    total = 0
    for t in texts:
        cjk = sum(1 for ch in t if "\u2e80" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff" or "\uff00" <= ch <= "\uffef")
        total += cjk + (len(t) - cjk) // 4
    return total


class BaseLLM:
    model_name: str = "unknown"

    def chat(self, messages: list[dict], meter: UsageMeter, tag: str = "", on_delta=None) -> LLMReply:
        """on_delta: 可选的流式回调，每收到一段增量就以「当前累积全文」调用一次。
        实现方保证：无论是否流式，返回值语义完全一致。"""
        raise NotImplementedError


class LLMClient(BaseLLM):
    def __init__(self, settings: Settings):
        if not settings.llm_api_key:
            raise LLMError(
                "LLM_API_KEY 未配置。请 `cp .env.example .env` 并填入你的 API Key；"
                "离线场景请使用 MockLLM。"
            )
        self._settings = settings
        self.model_name = settings.llm_model
        self._client = OpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,  # 重试策略自己控制，便于记录与退避
        )

    def chat(self, messages: list[dict], meter: UsageMeter, tag: str = "", on_delta=None) -> LLMReply:
        meter.check()
        settings = self._settings
        delay = 2.0
        last_error: Exception | None = None
        for attempt in range(settings.llm_max_retries + 1):
            if attempt:
                meter.check()  # 重试前再确认：调用方可能已断开，或预算已在别处用尽
            start = time.monotonic()
            try:
                if on_delta is not None:
                    text, usage = self._chat_streaming(messages, on_delta, meter, tag)
                else:
                    resp = self._client.chat.completions.create(
                        model=settings.llm_model,
                        messages=messages,
                        temperature=settings.llm_temperature,
                    )
                    choices = getattr(resp, "choices", None)
                    if not choices:
                        raise LLMError(f"LLM 返回空 choices（model={settings.llm_model}）")
                    text = choices[0].message.content or ""
                    usage = getattr(resp, "usage", None)
                latency_ms = int((time.monotonic() - start) * 1000)
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                if prompt_tokens == 0 and completion_tokens == 0:
                    # 上游不回 usage 时按字符保守估算——预算熔断不允许静默失效
                    prompt_tokens = estimate_tokens(str(m.get("content", "")) for m in messages)
                    completion_tokens = max(1, estimate_tokens([text]))
                    meter.unmetered_calls += 1
                meter.add(prompt_tokens, completion_tokens, tag=tag)
                return LLMReply(text, prompt_tokens, completion_tokens, latency_ms)
            except (RateLimitError, APITimeoutError, APIConnectionError) as e:
                last_error = e
            except APIStatusError as e:
                # 5xx 可重试，4xx（如鉴权错误）直接失败
                if e.status_code < 500:
                    raise LLMError(f"LLM 调用失败（HTTP {e.status_code}）: {e}") from e
                last_error = e
            except LLMError:
                raise
            except OpenAIError as e:
                raise LLMError(f"LLM 调用失败: {e}") from e
            except Exception as e:  # 畸形响应等未知异常：包装而不是冲出 ask()
                raise LLMError(f"LLM 响应处理失败: {type(e).__name__}: {e}") from e
            if attempt < settings.llm_max_retries:
                time.sleep(delay)
                delay *= 2
        raise LLMError(f"LLM 调用重试 {settings.llm_max_retries} 次后仍失败: {last_error}") from last_error

    def _chat_streaming(self, messages: list[dict], on_delta, meter: UsageMeter, tag: str = ""):
        """流式调用：逐块累积文本并回调。

        不传 stream_options（部分中转会 400）；多数 OpenAI 兼容端会在末块带 usage，
        没有就落到调用方的字符估算兜底——预算熔断不因流式而失效。
        途中被取消时立即关闭连接（上游随之停止生成），并按已收到的内容估算记账后再抛出。
        """
        settings = self._settings
        stream = self._client.chat.completions.create(
            model=settings.llm_model,
            messages=messages,
            temperature=settings.llm_temperature,
            stream=True,
        )
        parts: list[str] = []
        usage = None
        try:
            for chunk in stream:
                if meter.cancelled():
                    raise RunCancelled("调用方已取消本次运行")
                u = getattr(chunk, "usage", None)
                if u is not None:
                    usage = u
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                piece = getattr(delta, "content", None) if delta is not None else None
                if piece:
                    parts.append(piece)
                    on_delta("".join(parts))
        except RunCancelled:
            stream.close()
            meter.unmetered_calls += 1
            meter.add(
                estimate_tokens(str(m.get("content", "")) for m in messages),
                estimate_tokens(parts),
                tag=tag,
            )
            raise
        if not parts and usage is None:
            raise LLMError(f"LLM 流式返回为空（model={settings.llm_model}）")
        return "".join(parts), usage


class MockLLM(BaseLLM):
    """离线 mock：按脚本顺序吐回复。replies 用完后重复最后一条；
    cycle=True 则循环整个列表（服务 mock 模式/压测用）。"""

    model_name = "mock"

    def __init__(self, replies: list[str], cycle: bool = False):
        if not replies:
            raise ValueError("MockLLM 至少需要一条回复")
        self._replies = list(replies)
        self._cycle = cycle
        self._i = 0
        # 服务 mock 模式（cycle）会长期运行：只保留最近的调用，避免内存随请求数增长
        self.calls: list[list[dict]] | deque = deque(maxlen=200) if cycle else []

    def chat(self, messages: list[dict], meter: UsageMeter, tag: str = "", on_delta=None) -> LLMReply:
        meter.check()
        self.calls.append(messages)
        if self._cycle:
            text = self._replies[self._i % len(self._replies)]
        else:
            text = self._replies[min(self._i, len(self._replies) - 1)]
        self._i += 1
        if on_delta is not None:  # 模拟流式：分片回调累积文本（演示 mock 模式也能看到流式）
            step = max(1, len(text) // 8)
            for end in range(step, len(text) + step, step):
                if meter.cancelled():  # 与真实客户端一致：流式途中可被取消
                    raise RunCancelled("调用方已取消本次运行")
                time.sleep(0.004)
                on_delta(text[:end])
        # 与真实客户端的无 usage 兜底同源，保证预算熔断在离线路径同样可测
        prompt_tokens = estimate_tokens(str(m.get("content", "")) for m in messages)
        completion_tokens = max(1, estimate_tokens([text]))
        meter.add(prompt_tokens, completion_tokens, tag=tag)
        return LLMReply(text, prompt_tokens, completion_tokens, latency_ms=0)
