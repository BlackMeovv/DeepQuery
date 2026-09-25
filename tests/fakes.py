"""测试用的按内容路由的假模型：并行调用时回复不依赖调用顺序。"""

from deepquery.budget import RunCancelled
from deepquery.llm import BaseLLM, LLMReply, estimate_tokens


class RoutedLLM(BaseLLM):
    """routes：[(匹配文本, 回复)]，按顺序找第一个出现在 system 或最后一条消息里的匹配文本。"""

    model_name = "routed"

    def __init__(self, routes: list[tuple[str, str]], default: str = "```sql\nSELECT COUNT(*) FROM orders\n```"):
        self.routes = routes
        self.default = default
        self.calls: list[list[dict]] = []

    def chat(self, messages, meter, tag="", on_delta=None, temperature=None) -> LLMReply:
        meter.check()
        self.calls.append(messages)
        haystack = messages[0]["content"] + "\n" + messages[-1]["content"]
        text = next((reply for key, reply in self.routes if key in haystack), self.default)
        if on_delta is not None:
            if meter.cancelled():
                raise RunCancelled("取消")
            on_delta(text)
        p, c = estimate_tokens(str(m.get("content", "")) for m in messages), max(1, estimate_tokens([text]))
        meter.add(p, c, tag=tag)
        return LLMReply(text, p, c, latency_ms=0)
