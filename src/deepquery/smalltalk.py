"""和数据无关的寒暄：打招呼、问"你是谁 / 能做什么"、道谢。

整句都是寒暄时不调用模型、直接回复：零延迟、零花费；回复内容来自数据集说明和示例问题，不会编造。
只认整句——"介绍一下销售额最高的品类""谢谢，那按州呢"都不算，照常查数据。
其余闲聊（"你是什么模型做的"）交给模型，按提示词用一段话简短回应。
"""

from __future__ import annotations

import re

_NOISE = re.compile(r"[\s，。！？!?,.~～、…:：;；\"'“”‘’()（）]+")
_GREET = r"(?:你好|您好|hi|hello|hey|嗨|哈喽|哈啰|在吗|在不在)"
_INTRO = (
    r"(?:你?先?(?:简单)?(?:做个|做一下)?(?:介绍一下|介绍下|介绍)(?:一下)?(?:你自己|你|自己)"
    r"|(?:做个|做一下)?自我介绍(?:一下)?|你是谁|你是什么|你叫什么(?:名字)?)"
)
_HELP = (
    r"(?:你(?:能|可以|会)(?:做|干|帮我做|帮我干|回答|查)?(?:些)?(?:什么|啥|哪些)(?:问题|事情|事|数据)?"
    r"|(?:怎么|如何)(?:用|使用)(?:你)?|使用说明|帮助|help|有什么功能|能问(?:什么|哪些|啥)(?:问题)?)"
)
_THANKS = r"(?:谢谢|多谢|感谢|thanks|thankyou|thx)(?:你|您|啦|了)*"


def kind(question: str) -> str | None:
    """整句是寒暄时返回 "intro"（打招呼 / 你是谁 / 能做什么）或 "thanks"，否则 None。"""
    text = _NOISE.sub("", (question or "").lower())
    text = re.sub(r"^(?:请|麻烦)+", "", text)
    text = re.sub(r"(?:吧|呢|啊|呀|哈|嘛|哦)+$", "", text)
    if not text or len(text) > 30:
        return None
    if re.fullmatch(_THANKS, text):
        return "thanks"
    if re.fullmatch(f"{_GREET}*(?:{_INTRO}|{_HELP})", text) or re.fullmatch(f"{_GREET}+", text):
        return "intro"
    return None


def reply(kind_: str, note: str = "", samples: list[str] | None = None, tables: list[str] | None = None) -> str:
    """寒暄的回复。note 是数据说明，samples 是示例问题，tables 是可查的表（没有数据说明时用）。"""
    if kind_ == "thanks":
        return "不客气！还有想查的数据，直接问就行。"
    lines = [
        "你好，我是 DeepQuery，一个用自然语言查数据的助手。你用中文提问，我把问题翻译成 SQL、"
        "在只读数据库上查询，再用一两句话给出结论，并标出数据来自哪几张表。"
    ]
    if note:
        lines.append(f"现在连接的数据：{note}")
    elif tables:
        shown = "、".join(tables[:8]) + ("等" if len(tables) > 8 else "")
        lines.append(f"现在连接的数据库有 {len(tables)} 张表：{shown}。")
    if samples:
        lines.append("可以试试这样问：\n" + "\n".join(f"- {q}" for q in samples[:3]))
    lines.append("口径不明确时我会先向你确认；也可以问某个指标是怎么算的、用了哪些表和字段。")
    return "\n".join(lines)
