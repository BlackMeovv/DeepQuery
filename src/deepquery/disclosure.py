"""渐进式披露：表结构分三层按需交给模型，而不是一次全塞进提示词。

    第 1 层 表目录（常驻）：表名 + 一句话说明 + 列名，体积只有完整定义的一小部分；
    第 2 层 表定义（按需）：模型看目录选表，系统只展开选中表的建表语句和样例行；
             之后的 SQL 里用到了没展开的表，修复前自动补上；
    第 3 层 列取值（按需）：模型申请时，或者查询没查到数据（空结果，或 COUNT/SUM 得 0 而过滤值
             在库里根本不存在）时，查出过滤列真实出现过的取值。

为什么不是一次性检索选表：检索漏选的表在后面的步骤里补不回来（BIRD 消融：选表召回率 95.5%，
准确率比全量直供低 3.3 个点，差异不显著）；目录让模型看得到每一张表，漏选了也能在修复时补看。
为什么不总是这样做：多一次模型调用。装得下完整 schema 的库仍然直接全量提供。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from .guard import validate

# 表定义第一行的注释（SQLite：CREATE TABLE t (  -- 说明）或 MySQL 的 COMMENT='说明'
_TABLE_NOTE = re.compile(r"CREATE TABLE[^\n(]*\([ \t]*--[ \t]*([^\n]+)", re.IGNORECASE)
_MYSQL_NOTE = re.compile(r"\)[^;]*?COMMENT\s*=\s*'((?:[^'\\]|\\.)*)'", re.IGNORECASE)
_CODE_BLOCK = re.compile(r"```([a-zA-Z0-9_-]*)[ \t]*\n?(.*?)```", re.DOTALL)

MAX_VALUE_PROBES = 4  # 一次最多查几列的取值
VALUES_PER_COLUMN = 15  # 每列最多列出几个取值
_MAX_VALUE_CHARS = 40


def table_note(doc: str) -> str:
    """从建表语句里取表的一句话说明；没有注释时返回空串。"""
    m = _TABLE_NOTE.search(doc or "") or _MYSQL_NOTE.search(doc or "")
    return m.group(1).strip()[:80] if m else ""


def build_catalog(docs: dict[str, str], columns: dict[str, list[str]]) -> dict[str, str]:
    """每张表一行目录：`表名：说明` + 列名清单。"""
    out = {}
    for table, doc in docs.items():
        note = table_note(doc)
        head = f"{table}：{note}" if note else table
        cols = columns.get(table) or []
        out[table] = f"{head}\n  列：{', '.join(cols)}" if cols else head
    return out


def compose(docs: dict[str, str], catalog: dict[str, str], expanded: list[str]) -> str:
    """已展开的表给完整定义，其余表只保留目录行（模型仍然知道它们存在）。"""
    parts = [docs[t] for t in expanded if t in docs]
    rest = [catalog[t] for t in catalog if t not in expanded]
    text = "\n\n".join(parts)
    if rest:
        text += (
            "\n\n其余的表（只列出说明和列名；SQL 里可以直接使用，系统会补上它们的完整定义）：\n"
            + "\n".join(rest)
        )
    return text


def _names(body: str) -> list[str]:
    out = []
    for raw in re.split(r"[\n,，、]", body):
        name = raw.strip().strip("-*•` ").strip()
        if name:
            out.append(name)
    return out


def parse_request(text: str, tables: set[str], columns: dict[str, list[str]]) -> tuple[list[str], list[tuple[str, str]]]:
    """解析模型的 ```tables / ```values 代码块，只保留真实存在的表和列（大小写不敏感，返回真实写法）。"""
    by_lower = {t.lower(): t for t in tables}
    picked: list[str] = []
    probes: list[tuple[str, str]] = []
    for lang, body in _CODE_BLOCK.findall(text or ""):
        lang = lang.lower()
        if lang == "tables":
            for name in _names(body):
                real = by_lower.get(name.lower())
                if real and real not in picked:
                    picked.append(real)
        elif lang == "values":
            for name in _names(body):
                table, _, column = name.partition(".")
                real = by_lower.get(table.strip().lower())
                col = _real_column(columns, real, column.strip()) if real else None
                if col and (real, col) not in probes:
                    probes.append((real, col))
    return picked, probes[:MAX_VALUE_PROBES]


def _real_column(columns: dict[str, list[str]], table: str, name: str) -> str | None:
    return next((c for c in columns.get(table, []) if c.lower() == name.lower()), None)


def probe_values(db, table: str, column: str, limit: int = VALUES_PER_COLUMN) -> str | None:
    """查一列最常见的取值及出现次数，给模型核对过滤条件的写法。

    表名列名必须已经核对过真实存在；SQL 仍然走一遍守卫，再用只读连接执行（有超时）。
    """
    dialect = getattr(db, "dialect", "sqlite")
    qt = exp.to_identifier(table, quoted=True).sql(dialect=dialect)
    qc = exp.to_identifier(column, quoted=True).sql(dialect=dialect)
    sql = (
        f"SELECT {qc}, COUNT(*) AS n FROM {qt} WHERE {qc} IS NOT NULL "
        f"GROUP BY {qc} ORDER BY n DESC LIMIT {int(limit)}"
    )
    verdict = validate(sql, allowed_tables={table}, max_rows=limit, dialect=dialect)
    if not verdict.allowed:
        return None
    result = db.run_query(verdict.sql)
    if not result.ok:
        return f"{table}.{column}：没有非空取值" if result.error_kind == "empty_result" else None
    items = []
    for value, count in result.rows:
        text = str(value)
        text = text[:_MAX_VALUE_CHARS] + "…" if len(text) > _MAX_VALUE_CHARS else text
        items.append(f"{text}（{count}）")
    more = "，只列出最常见的这些" if result.row_count >= limit else ""
    return f"{table}.{column}：{'、'.join(items)}{more}"


_UNWRAP = (exp.Lower, exp.Upper, exp.Trim)


@dataclass
class TextFilter:
    """SQL 里一个拿字符串字面量做比较的过滤条件。"""

    table: str
    column: str
    values: list[str]
    exact: bool  # 裸列上的 = / IN：字面量必须原样出现在列里才可能命中


def _column_of(node) -> tuple[exp.Column | None, bool]:
    wrapped = False
    while isinstance(node, _UNWRAP):
        node, wrapped = node.this, True
    return (node, wrapped) if isinstance(node, exp.Column) else (None, wrapped)


def _is_text(node) -> bool:
    return isinstance(node, exp.Literal) and node.is_string


def text_filters(sql: str, columns: dict[str, list[str]], dialect: str = "sqlite") -> list[TextFilter]:
    """找出 SQL 里拿字符串字面量做过滤的列（= / IN / LIKE），解析到真实的表和列，按出现顺序。"""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return []
    if tree is None:
        return []
    by_lower = {t.lower(): t for t in columns}
    alias: dict[str, str] = {}
    used: list[str] = []
    for t in tree.find_all(exp.Table):
        real = by_lower.get(t.name.lower())
        if real:
            alias[t.alias_or_name.lower()] = real
            alias[real.lower()] = real
            if real not in used:
                used.append(real)

    def resolve(col: exp.Column) -> tuple[str, str] | None:
        if col.table:
            table = alias.get(col.table.lower())
            candidates = [table] if table else []
        else:
            candidates = [t for t in used if _real_column(columns, t, col.name)]
        if len(candidates) != 1:  # 归属不明（多张表都有这一列）就不猜
            return None
        real = _real_column(columns, candidates[0], col.name)
        return (candidates[0], real) if real else None

    out: list[TextFilter] = []
    for node in tree.find_all(exp.EQ, exp.Like, exp.ILike, exp.In, bfs=False):
        if isinstance(node, exp.In):
            (col, wrapped), literals = _column_of(node.this), node.expressions
            if not literals or not all(_is_text(e) for e in literals):
                continue
        else:
            (col, wrapped), literals = _column_of(node.this), [node.expression]
            if col is None:
                (col, wrapped), literals = _column_of(node.expression), [node.this]
            if not _is_text(literals[0]):
                continue
        pair = resolve(col) if col is not None else None
        if pair:
            exact = not wrapped and isinstance(node, (exp.EQ, exp.In))
            out.append(TextFilter(pair[0], pair[1], [lit.this for lit in literals], exact))
    return out


def filtered_columns(sql: str, columns: dict[str, list[str]], dialect: str = "sqlite", limit: int = 3) -> list[tuple[str, str]]:
    """拿字符串做过滤的 (表, 列)，去重，最多 limit 个。

    查询返回空结果时，最常见的原因是过滤值写法和库里不一致（大小写、中英文、缩写）：
    把这些列的真实取值查出来交给修复轮，比让模型盲猜强。
    """
    out: list[tuple[str, str]] = []
    for f in text_filters(sql, columns, dialect):
        if (f.table, f.column) not in out:
            out.append((f.table, f.column))
    return out[:limit]


def missing_values(db, filters: list[TextFilter], limit: int = 3) -> list[str]:
    """精确过滤（= / IN）里，在对应列中一次都没出现过的字面量——这样的条件永远匹配不到任何行。"""
    dialect = getattr(db, "dialect", "sqlite")
    out: list[str] = []
    for f in filters:
        if not f.exact:
            continue
        qt = exp.to_identifier(f.table, quoted=True).sql(dialect=dialect)
        qc = exp.to_identifier(f.column, quoted=True).sql(dialect=dialect)
        for value in f.values[:5]:
            literal = exp.Literal.string(value).sql(dialect=dialect)
            verdict = validate(f"SELECT 1 FROM {qt} WHERE {qc} = {literal}", allowed_tables={f.table}, max_rows=1, dialect=dialect)
            if not verdict.allowed:
                continue
            result = db.run_query(verdict.sql)
            if result.error_kind == "empty_result":
                out.append(f"'{value}' 不在 {f.table}.{f.column} 里")
            if len(out) >= limit:
                return out
    return out


def looks_empty(result) -> bool:
    """只有一行、且每个值都是 0 或空：COUNT/SUM 类查询"查不到"时的样子，不会报错，最容易被当成真实答案。"""
    if not result.ok or result.row_count != 1 or not result.rows:
        return False
    return all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0) for v in result.rows[0])
