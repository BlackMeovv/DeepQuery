"""单次运行的用量计量与预算熔断。

设计约束：预算护栏必须在编排层生效（每次 LLM 调用前检查），
而不是指望模型自己"省着用"。超限后 agent 走降级收尾，不再调用 LLM。
"""

import threading
from dataclasses import dataclass, field
from typing import Any


class BudgetExceeded(RuntimeError):
    """预算（token 或金额）超限。"""


class RunCancelled(BaseException):
    """调用方取消了本次运行（如浏览器断开 SSE）。

    继承 BaseException（同 asyncio.CancelledError）：各节点只捕获 LLMError / BudgetExceeded
    并走降级，取消信号必须穿透它们直接结束运行，而不是被当成一次普通失败继续往下跑。
    """


@dataclass
class UsageMeter:
    """按次运行累计 token / 成本 / 调用数。价格单位：每百万 token。"""

    price_input_per_m: float = 0.0
    price_output_per_m: float = 0.0
    max_tokens: int = 0  # 0 = 不限制
    max_cost: float = 0.0  # 0 = 不限制

    llm_calls: int = 0
    unmetered_calls: int = 0  # 上游未返回 usage、按字符估算记账的调用数
    prompt_tokens: int = 0
    completion_tokens: int = 0
    by_tag: dict[str, int] = field(default_factory=dict)
    cancel_event: Any = field(default=None, repr=False, compare=False)  # threading.Event | None
    # 分析模式里几个子查询并行跑、共用一个计量器：累加必须加锁
    _lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    def cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost(self) -> float:
        return (
            self.prompt_tokens * self.price_input_per_m
            + self.completion_tokens * self.price_output_per_m
        ) / 1_000_000

    def add(self, prompt_tokens: int, completion_tokens: int, tag: str = "", unmetered: bool = False) -> None:
        with self._lock:
            self.llm_calls += 1
            self.unmetered_calls += 1 if unmetered else 0
            self.prompt_tokens += int(prompt_tokens or 0)
            self.completion_tokens += int(completion_tokens or 0)
            if tag:
                self.by_tag[tag] = self.by_tag.get(tag, 0) + int(prompt_tokens or 0) + int(
                    completion_tokens or 0
                )

    def exceeded(self) -> bool:
        if self.max_tokens and self.total_tokens >= self.max_tokens:
            return True
        if self.max_cost and self.cost >= self.max_cost:
            return True
        return False

    def check(self) -> None:
        if self.cancelled():
            raise RunCancelled("调用方已取消本次运行")
        if self.exceeded():
            raise BudgetExceeded(
                f"预算超限: tokens={self.total_tokens}/{self.max_tokens or '∞'}, "
                f"cost={self.cost:.6f}/{self.max_cost or '∞'}"
            )

    def snapshot(self) -> dict:
        return {
            "llm_calls": self.llm_calls,
            "unmetered_calls": self.unmetered_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cost": round(self.cost, 6),
        }


class RunHandle:
    """调用方与一次运行之间的控制句柄：可随时取消，结束后可读到本次的用量。

    服务端用它在客户端断开时停止运行，并且无论成功、失败还是取消都按实际用量记账。
    """

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.meter: UsageMeter | None = None

    def cancel(self) -> None:
        self.cancelled.set()

    def usage(self) -> dict:
        return self.meter.snapshot() if self.meter else UsageMeter().snapshot()
