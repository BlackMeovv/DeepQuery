"""依赖索引：一条例句 / 口径 / 用户记忆引用了哪些表、哪些列、哪些具体取值。

表结构或取值变了以后，靠它找出"受影响的那几条"去复核，而不是全部重跑。

- 例句有 SQL：用 sqlglot 解析，表别名还原成真实表名，未带表名的列按"涉及的表里谁有这一列"归属；
  `列 = '取值'`、`列 IN (…)` 这类精确过滤记为取值依赖。
- 口径和用户记忆是自然语言：只认文本里出现的真实表名、`表.列` 写法，以及在库里唯一、
  不是 id / name 这类通用词的列名；`列 = '取值'` 记为取值依赖。纯中文的说法认不出依赖，
  这类条目不会被自动标记——宁可漏标，不能误伤。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from ..disclosure import text_filters
from .profiler import TableStructure

# 列名太通用：自然语言里出现了也不当成依赖（这些列名通常多张表都有，本来也归属不明）
_GENERIC = {
    "id", "name", "type", "date", "time", "value", "code", "year", "month", "day", "status",
    "amount", "price", "count", "total", "title", "desc", "note", "text", "data", "key",
}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_QUALIFIED = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
_EQ_VALUE = re.compile(r"\b(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s*(?:==|=)\s*'((?:[^']|'')*)'")
_IN_VALUES = re.compile(r"\b(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
_QUOTED = re.compile(r"'((?:[^']|'')*)'")
# 自然语言里长得像"表.列"、其实是文件名 / 网址 / 邮箱的：先去掉，免得 orders.xlsx 被当成失效的列
_URL_OR_EMAIL = re.compile(
    r"[a-z][a-z0-9+.-]*://\S+|[\w.+-]+@[\w-]+(?:\.[\w-]+)+|www\.\S+|"
    r"\b(?:[a-z0-9-]+\.)+(?:com|cn|net|org|io|edu|gov|ai|dev|app|cc|co)\b(?:/\S*)?",
    re.IGNORECASE,
)
_NOT_COLUMNS = {
    "xlsx", "xls", "csv", "tsv", "md", "txt", "json", "pdf", "doc", "docx", "ppt", "pptx", "py", "sql", "html",
    "htm", "png", "jpg", "jpeg", "gif", "zip", "log", "yaml", "yml", "xml", "parquet", "com", "cn", "net", "org", "io",
}


@dataclass
class Deps:
    refs: set[str] = field(default_factory=set)  # "表" 或 "表.列"（真实写法）
    values: set[tuple[str, str, str]] = field(default_factory=set)  # (表, 列, 取值)

    def tables(self) -> set[str]:
        return {r.split(".", 1)[0] for r in self.refs}

    def to_lists(self) -> tuple[list[str], list[list[str]]]:
        return sorted(self.refs), sorted([list(v) for v in self.values])


class _Schema:
    def __init__(self, structures: dict[str, TableStructure]):
        self.structures = structures
        self.tables = {t.lower(): t for t in structures}
        owners: dict[str, list[str]] = {}
        for t, s in structures.items():
            for c in s.column_names:
                owners.setdefault(c.lower(), []).append(t)
        self.owners = owners

    def table(self, name: str) -> str | None:
        return self.tables.get(name.lower())

    def column(self, table: str, name: str) -> str | None:
        s = self.structures.get(table)
        return s.real_column(name) if s else None


class SchemaIndex:
    """当前结构的查找索引。复核时对成千上万条条目逐条判断，索引只建一次。"""

    def __init__(self, structures: dict[str, TableStructure]):
        self.schema = _Schema(structures)


def missing_refs(refs, structures: dict[str, TableStructure] | None = None, index: SchemaIndex | None = None) -> list[str]:
    """依赖里在当前结构中已经不存在的表 / 列。"""
    schema = index.schema if index is not None else _Schema(structures or {})
    out = []
    for ref in refs:
        table, _, column = ref.partition(".")
        real = schema.table(table)
        if real is None or (column and schema.column(real, column) is None):
            out.append(ref)
    return out


def sql_deps(sql: str, structures: dict[str, TableStructure], dialect: str = "sqlite") -> Deps:
    """SQL 引用的表、列，以及精确过滤用到的取值。"""
    schema = _Schema(structures)
    deps = Deps()
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return deps
    if tree is None:
        return deps
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    alias: dict[str, str] = {}
    used: list[str] = []
    for t in tree.find_all(exp.Table):
        if t.name.lower() in ctes:
            continue
        real = schema.table(t.name) or t.name  # 库里没有的表也记下：它就是失效的依赖
        alias[t.alias_or_name.lower()] = real
        alias[t.name.lower()] = real
        deps.refs.add(real)
        if real not in used:
            used.append(real)
    for col in tree.find_all(exp.Column):
        if not col.name or isinstance(col.this, exp.Star):
            continue
        if col.table:
            table = alias.get(col.table.lower())
            if table:
                deps.refs.add(f"{table}.{schema.column(table, col.name) or col.name}")
            continue
        # 不带表名、哪张表都没有的列多半是 SELECT 别名或 CTE 的输出列，不记；真失效的话复核时
        # 试编译这条 SQL 会报出来
        owners = [t for t in used if schema.column(t, col.name)]
        if len(owners) == 1:
            deps.refs.add(f"{owners[0]}.{schema.column(owners[0], col.name)}")
    columns = {t: s.column_names for t, s in structures.items()}
    for f in text_filters(sql, columns, dialect):
        if f.exact and not f.negated:  # NOT IN / NOT (x = …) 里的取值消失了，条件照样成立
            for v in f.values:
                deps.values.add((f.table, f.column, v))
    return deps


def text_deps(text: str, structures: dict[str, TableStructure]) -> Deps:
    """自然语言条目里能确定的依赖。"""
    schema = _Schema(structures)
    deps = Deps()
    text = _URL_OR_EMAIL.sub(" ", text or "")
    mentioned_tables: list[str] = []
    for m in _QUALIFIED.finditer(text):
        table = schema.table(m.group(1))
        if not table:
            continue
        real = schema.column(table, m.group(2))
        if real is None and (len(m.group(2)) < 3 or m.group(2).lower() in _NOT_COLUMNS):
            continue  # orders.xlsx、products.e.g 这类：不当成引用了不存在的列
        deps.refs.add(f"{table}.{real or m.group(2)}")
        mentioned_tables.append(table)
    stripped = _QUALIFIED.sub(" ", text)
    words = _WORD.findall(stripped)
    for w in words:
        table = schema.table(w)
        if table:
            deps.refs.add(table)
            mentioned_tables.append(table)
    for w in words:
        if schema.table(w) or w.lower() in _GENERIC or len(w) < 3:
            continue
        owner = _owner(schema, w, mentioned_tables)
        if owner:
            deps.refs.add(f"{owner}.{schema.column(owner, w)}")

    def add_value(qual: str | None, col: str, value: str) -> None:
        if qual:
            table = schema.table(qual)
        else:
            table = _owner(schema, col, mentioned_tables, allow_generic=True)
        real = schema.column(table, col) if table else None
        if table and real:
            deps.refs.add(f"{table}.{real}")
            deps.values.add((table, real, value.replace("''", "'")))

    for m in _EQ_VALUE.finditer(text):
        add_value(m.group(1), m.group(2), m.group(3))
    for m in _IN_VALUES.finditer(text):
        for v in _QUOTED.findall(m.group(3)):
            add_value(m.group(1), m.group(2), v)
    return deps


def _owner(schema: _Schema, column: str, mentioned: list[str], allow_generic: bool = False) -> str | None:
    owners = schema.owners.get(column.lower(), [])
    if not owners or (column.lower() in _GENERIC and not allow_generic):
        return None
    if len(owners) == 1:
        return owners[0]
    hinted = [t for t in owners if t in mentioned]
    return hinted[0] if len(set(hinted)) == 1 else None
