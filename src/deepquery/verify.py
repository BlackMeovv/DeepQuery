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
                elif isinstance(cell, str):
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
