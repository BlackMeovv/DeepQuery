"""用模型给缺说明的表和列写中文说明。

只补建表语句里没有注释、也没有人工写过说明的表 / 列；生成的说明标"待确认"，
人工改过的不会被覆盖。模型只看建表语句、样例取值和建档统计，看不出含义的要求写"不确定"，
这类说明直接丢掉，不进档案。
"""

from __future__ import annotations

import re

from ..budget import UsageMeter
from ..llm import BaseLLM
from .profiler import ColumnProfile, TableProfile, TableStructure

DESCRIBE_SYSTEM = """你在给一个业务数据库写数据字典，读者是要写 SQL 的同事。
只根据给出的建表语句、列的取值特征来写，不要编造业务背景；从名字和取值看不出含义的，写"不确定"。
表说明不超过 30 个字，列说明不超过 20 个字，说清楚"这一列存的是什么"，有单位、编码含义的写上。

只输出一个 ```describe 代码块，格式：
```describe
表：一句话说明
列名：说明
列名：说明
```"""

_MAX_COLUMNS_PER_CALL = 60
_LINE = re.compile(r"^\s*[-*]?\s*`?([^:：`]+?)`?\s*[:：]\s*(.+?)\s*$")
_BLOCK = re.compile(r"```describe[ \t]*\n?(.*?)```", re.DOTALL | re.IGNORECASE)
_UNSURE = re.compile(r"不确定|不清楚|未知|无法判断|看不出")


def _column_brief(col: ColumnProfile | None) -> str:
    if col is None:
        return ""
    if col.kind == "enum" and col.values:
        return "取值：" + " / ".join(col.values[:10])
    if col.kind == "date":
        span = f"，{col.date_min} ~ {col.date_max}" if col.date_min else ""
        return f"日期，格式 {col.date_format}{span}"
    if col.kind == "text" and col.examples:
        return "样例：" + " / ".join(col.examples)
    return {"number": "数值", "key": "编号 / 关联键", "longtext": "长文本", "empty": "全部为空"}.get(col.kind, "")


def build_prompt(structure: TableStructure, profile: TableProfile | None, columns: list[str], need_table: bool) -> str:
    lines = [f"表名：{structure.name}"]
    if structure.note:
        lines.append(f"已有的表说明：{structure.note}")
    lines.append("列（名称 · 类型 · 取值特征 · 已有说明）：")
    for name, ctype in structure.columns:
        col = profile.columns.get(name) if profile else None
        known = structure.column_notes.get(name, "")
        parts = [name, ctype or "未声明类型", _column_brief(col)]
        if known:
            parts.append(f"已有说明：{known}")
        lines.append("- " + " · ".join(p for p in parts if p))
    asks = []
    if need_table:
        asks.append("表")
    asks += columns
    lines.append("")
    lines.append(f"请为这些写说明：{'、'.join(asks)}")
    return "\n".join(lines)


def parse_reply(text: str, columns: list[str]) -> tuple[str, dict[str, str]]:
    m = _BLOCK.search(text or "")
    body = m.group(1) if m else (text or "")
    wanted = {c.lower(): c for c in columns}
    table_desc, out = "", {}
    for raw in body.splitlines():
        lm = _LINE.match(raw)
        if not lm:
            continue
        name, desc = lm.group(1).strip(), lm.group(2).strip()
        if not desc or _UNSURE.search(desc):
            continue
        if name in ("表", "表说明"):
            table_desc = desc[:60]
        elif name.lower() in wanted:
            out[wanted[name.lower()]] = desc[:40]
    return table_desc, out


def describe_table(
    llm: BaseLLM,
    structure: TableStructure,
    profile: TableProfile | None,
    columns: list[str],
    need_table: bool,
    meter: UsageMeter,
) -> tuple[str, dict[str, str]]:
    """返回 (表说明, {列: 说明})。模型调用失败或超预算时抛出 LLMError / BudgetExceeded，
    由调用方记一条事件后继续——说明是锦上添花，不能让建档失败。"""
    columns = columns[:_MAX_COLUMNS_PER_CALL]
    if not columns and not need_table:
        return "", {}
    messages = [
        {"role": "system", "content": DESCRIBE_SYSTEM},
        {"role": "user", "content": build_prompt(structure, profile, columns, need_table)},
    ]
    reply = llm.chat(messages, meter, tag="catalog_describe")
    return parse_reply(reply.text, columns)
