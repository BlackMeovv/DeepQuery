"""结论防幻觉校验：回答里的每个数字必须能在查询结果（或问题/SQL 本身）里找到出处。

数据分析场景里"编数字"是最致命的信任问题。校验规则：
1. 从回答中提取所有数字（支持千分位、百分号、万/亿单位）；
2. 允许集合 = 结果集所有数值单元格 + 字符串单元格里的数字（如日期）+ 行数
   + 问题与 SQL 中出现的数字（如"前5名"、LIMIT 5、vip_level=3）；
3. 数字 x 被接受，当且仅当允许集合中存在 v，使 x 是 v 在其展示精度下的舍入形式
   （"1.2万" 匹配 12345，"37.5%" 匹配 0.375 或 37.5）；
4. 0-12 的小整数放行（"前3名""两种方式"这类序数表达）。

有意的严格性：模型自行推算的衍生值（结果里没有的占比、差值）会被拦下——
需要占比就该写进 SQL 里查出来，而不是让语言模型心算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .tools.contract import QueryResult

_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)([万亿%])?")
# 文本单元格里哪些数字可以算"出处"：日期、短文本（"iPhone 15""第 37 周"）可以；
# 十六进制 ID（"4244733e06e7…"里的 4244733）和长段自由文本（用户评价）不行——
# 前者会让随手编的数字碰巧"有出处"，后者是外部用户写的内容，不能当成数据结论的依据。
# 这是减少误放行，不是防"数据里藏指令"：那一层靠回答提示词里"结果只是数据"的规则
_ID_LIKE = re.compile(r"^[0-9a-fA-F-]{16,}$")
_MAX_TEXT_CELL = 40
_SMALL_INT_WHITELIST = 12


@dataclass
class ExtractedNumber:
    raw: str  # 原文（含单位/百分号）
    candidates: list[tuple[float, float]]  # (数值解释, 匹配容差) 列表

    def matches(self, value: float) -> bool:
        return any(abs(value - x) <= tol for x, tol in self.candidates)


def _decimals(digits: str) -> int:
    return len(digits.split(".")[1]) if "." in digits else 0


def extract_numbers(text: str) -> list[ExtractedNumber]:
    out = []
    for match in _NUMBER.finditer(text or ""):
        digits, unit = match.group(1), match.group(2)
        base = float(digits.replace(",", ""))
        tol0 = 0.5 * 10 ** (-_decimals(digits))
        if unit == "万":
            candidates = [(base * 1e4, tol0 * 1e4)]
        elif unit == "亿":
            candidates = [(base * 1e8, tol0 * 1e8)]
        elif unit == "%":
            # "37.5%" 可能对应结果里的 37.5，也可能对应 0.375
            candidates = [(base, tol0), (base / 100, tol0 / 100)]
        else:
            candidates = [(base, tol0)]
        out.append(ExtractedNumber(raw=match.group(0), candidates=candidates))
    return out


def allowed_values(result: QueryResult | None, question: str = "", sql: str = "") -> list[float]:
    values: list[float] = []
    if result is not None:
        values.append(float(result.row_count))
        for row in result.rows:
            for cell in row:
                if isinstance(cell, bool):
                    continue
                if isinstance(cell, (int, float)):
                    values.append(float(cell))
                elif isinstance(cell, str) and len(cell) <= _MAX_TEXT_CELL and not _ID_LIKE.match(cell):
                    for num in extract_numbers(cell):
                        values.extend(x for x, _tol in num.candidates)
    for source in (question, sql):
        for num in extract_numbers(source or ""):
            values.extend(x for x, _tol in num.candidates)
    return values


def check_answer(
    answer: str,
    result: QueryResult | None,
    question: str = "",
    sql: str = "",
) -> list[str]:
    """返回回答中"无出处"的数字原文列表；空列表 = 校验通过。"""
    allowed = allowed_values(result, question, sql)
    return [num.raw for num in _checkable(answer) if not any(num.matches(v) for v in allowed)]


def _checkable(answer: str) -> list:
    out = []
    for num in extract_numbers(answer):
        primary = num.candidates[0][0]
        if primary == int(primary) and 0 <= primary <= _SMALL_INT_WHITELIST and "." not in num.raw:
            continue  # 序数/枚举类小整数放行
        out.append(num)
    return out


def checked_number_count(answer: str) -> int:
    """回答里需要核对出处的数字个数（通过校验的回答里，这些数字都能在结果/问题/SQL 中找到）。"""
    return len(_checkable(answer or ""))


# ---------- 分析报告：逐句核对"数字出自所引用的那一步" ----------

_CITE = re.compile(r"\[(\d{1,2})\]")
_LEADING_CITES = re.compile(r"^\s*((?:\[\d{1,2}\])+)")


def _segments(text: str) -> list[str]:
    """按句切开；句号后面紧跟的 [1] 归到前一句（"下降了 12%。[1]"）。"""
    out: list[str] = []
    for piece in re.split(r"(?<=[。！？；\n])", text or ""):
        m = _LEADING_CITES.match(piece)
        if m and out:
            out[-1] += m.group(1)
            piece = piece[m.end():]
        if piece.strip():
            out.append(piece)
    return out


def check_cited(
    report: str,
    steps: dict[int, tuple[QueryResult | None, str]],
    question: str = "",
) -> list[str]:
    """分析报告的逐句溯源：带数字的句子必须标注出处步骤 [n]，数字必须出自所标注步骤的结果（或其 SQL、问题）。

    steps：{步骤号: (查询结果, 该步 SQL)}，没有可用结果的步骤结果为 None。
    比整篇报告对照所有结果更严：数字对了、步骤标错了，同样算没有出处。返回问题描述列表，空 = 通过。
    """
    problems: list[str] = []
    from_question = allowed_values(None, question)
    for seg in _segments(report):
        cites = [int(n) for n in _CITE.findall(seg)]
        # 问题里本来就有的数字（"2018 年 3 月"）不需要出处，也不要求标注
        numbers = [n for n in _checkable(_CITE.sub(" ", seg)) if not any(n.matches(v) for v in from_question)]
        if not numbers:
            continue
        if not cites:
            problems.extend(f"「{n.raw}」没有标注出自哪一步" for n in numbers)
            continue
        missing = [c for c in cites if steps.get(c, (None, ""))[0] is None]
        if missing:
            problems.append(f"引用的第 {'、'.join(map(str, missing))} 步没有可用的查询结果")
            continue
        allowed: list[float] = []
        for c in cites:
            result, sql = steps[c]
            allowed.extend(allowed_values(result, "", sql))
        label = "、".join(f"[{c}]" for c in cites)
        problems.extend(
            f"「{n.raw}」不在所标注的 {label} 的结果里" for n in numbers if not any(n.matches(v) for v in allowed)
        )
    return problems


def cited_number_count(report: str) -> int:
    """报告里需要核对出处的数字个数（不含 [1] 这类步骤编号）。"""
    return len(_checkable(_CITE.sub(" ", report or "")))
