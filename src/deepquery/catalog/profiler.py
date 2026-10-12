"""建档：只读地读出每张表的结构，并采集"写对 SQL 要用到"的取值特征。

只采集四类会直接改变 SQL 写法的信息，其余一律不采：
    枚举取值   status 存的是 'completed' 还是 '已完成'，决定过滤条件怎么写
    日期范围   日期列的格式和起止，决定日期函数怎么用、"去年"有没有数据
    格式样例   编码类文本列的真实写法（大小写、前缀、分隔符）
    关联关系   声明的外键 + 按列名推断、再用数据核对过的关联，决定 JOIN 怎么写
空值率只在超过一半时标一句"多数为空"；行数只在内部用（判断抽样是否覆盖全表）；
数值列不给最小 / 最大值——这些数字进了提示词，模型容易拿来当答案。

所有查询都先过 SQL 守卫、再走只读连接，和 Agent 自己的查询是同一条路径。
统计在每张表的前 sample_rows 行上做（日期起止除外，那是全表的 MIN / MAX），
大表建档的开销因此有上限；抽样没覆盖全表时，枚举取值标记为"可能不全"。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field

import sqlglot
from sqlglot import exp

from ..disclosure import table_note
from ..guard import validate

class ProfileError(RuntimeError):
    """这张表这次建不了档（读不出行数）。调用方不存档，下次同步重试。"""


# ---------- 结构 ----------

_COL_COMMENT = re.compile(r'^\s*[`"\[]?([^`"\]\s]+)[`"\]]?\s+[A-Za-z][^\n]*?--\s*(.+?)\s*$')
_MYSQL_COL_COMMENT = re.compile(r"^\s*`?([^`\s]+)`?\s+[^\n]*?\bCOMMENT\s+'((?:[^'\\]|\\.)*)'", re.IGNORECASE)


@dataclass
class TableStructure:
    """一张表的结构。指纹只由列名、类型、主键、外键决定：增删改行数据不会改变它。"""

    name: str
    columns: list[tuple[str, str]]  # (列名, 声明类型)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[tuple[str, str, str]] = field(default_factory=list)  # (列, 引用表, 引用列；未写列时为空串)
    note: str = ""  # 建表语句里的表注释
    column_notes: dict[str, str] = field(default_factory=dict)  # 建表语句里的列注释

    @property
    def column_names(self) -> list[str]:
        return [c for c, _t in self.columns]

    def has_column(self, name: str) -> bool:
        return any(c.lower() == name.lower() for c, _t in self.columns)

    def real_column(self, name: str) -> str | None:
        return next((c for c, _t in self.columns if c.lower() == name.lower()), None)

    def column_type(self, name: str) -> str:
        return next((t for c, t in self.columns if c.lower() == name.lower()), "")

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "columns": [[c, _norm_type(t)] for c, t in self.columns],
                "pk": sorted(c.lower() for c in self.primary_key),
                "fk": sorted([c.lower(), t.lower(), r.lower()] for c, t, r in self.foreign_keys),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "TableStructure":
        d = json.loads(text)
        return cls(
            name=d["name"],
            columns=[tuple(c) for c in d.get("columns", [])],
            primary_key=list(d.get("primary_key", [])),
            foreign_keys=[tuple(f) for f in d.get("foreign_keys", [])],
            note=d.get("note", ""),
            column_notes=dict(d.get("column_notes", {})),
        )


def _norm_type(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


def _ident(node) -> str:
    if isinstance(node, exp.Ordered):
        node = node.this
    return node.name if node is not None else ""


def keys_from_ddl(ddl: str, dialect: str = "sqlite") -> tuple[list[str], list[tuple[str, str, str]]]:
    """从建表语句解析主键和外键；解析不了时返回空（PG 的伪 DDL 就没有键信息，靠推断补）。"""
    text = (ddl or "").strip()
    if not text:
        return [], []
    text = text.split(";\n", 1)[0].strip().rstrip(";")
    try:
        tree = sqlglot.parse_one(text, read=dialect)
    except Exception:  # noqa: BLE001 —— 解析不了的建表语句（方言怪癖）就当没有键信息，靠推断补
        return [], []
    if not isinstance(tree, exp.Create):
        return [], []
    pk: list[str] = []
    for node in tree.find_all(exp.PrimaryKeyColumnConstraint):
        coldef = node.find_ancestor(exp.ColumnDef)
        if coldef is not None and coldef.name not in pk:
            pk.append(coldef.name)
    for node in tree.find_all(exp.PrimaryKey):
        for e in node.expressions:
            name = _ident(e)
            if name and name not in pk:
                pk.append(name)

    fks: list[tuple[str, str, str]] = []

    def target(ref: exp.Reference) -> tuple[str, list[str]]:
        node = ref.this
        if isinstance(node, exp.Schema):
            return node.this.name if node.this is not None else "", [_ident(x) for x in node.expressions]
        if isinstance(node, exp.Table):
            return node.name, []
        return "", []

    for node in tree.find_all(exp.ForeignKey):
        ref = node.args.get("reference")
        if not isinstance(ref, exp.Reference):
            continue
        table, ref_cols = target(ref)
        cols = [_ident(e) for e in node.expressions]
        for i, col in enumerate(cols):
            if table and col:
                fks.append((col, table, ref_cols[i] if i < len(ref_cols) else ""))
    for ref in tree.find_all(exp.Reference):
        coldef = ref.find_ancestor(exp.ColumnDef)
        if coldef is None:
            continue  # 表级 FOREIGN KEY 已在上面处理
        table, ref_cols = target(ref)
        if table:
            fks.append((coldef.name, table, ref_cols[0] if ref_cols else ""))
    return pk, list(dict.fromkeys(fks))


def column_notes_from_ddl(ddl: str, columns: list[str]) -> dict[str, str]:
    """建表语句里的列注释（SQLite / PG 伪 DDL 的 `-- 注释`，MySQL 的 COMMENT '…'）。"""
    wanted = {c.lower(): c for c in columns}
    out: dict[str, str] = {}
    body = (ddl or "").split(";\n", 1)[0]
    for line in body.splitlines()[1:]:
        m = _MYSQL_COL_COMMENT.match(line) or _COL_COMMENT.match(line)
        if m:
            real = wanted.get(m.group(1).lower())
            if real and m.group(2).strip():
                out[real] = m.group(2).strip()[:120]
    return out


def read_structures(db, tables: set[str] | None = None) -> dict[str, TableStructure]:
    """读出（可见的）每张表的结构。只读元数据：SQLite 的 PRAGMA、服务器库的 information_schema。"""
    raw = db.table_columns()
    try:
        ddls = db.schema_by_table(sample_rows=0)
    except TypeError:  # 不支持 sample_rows 参数的自定义引擎
        ddls = db.schema_by_table()
    dialect = getattr(db, "dialect", "sqlite")
    out: dict[str, TableStructure] = {}
    for table, cols in raw.items():
        if tables is not None and table not in tables:
            continue
        ddl = ddls.get(table, "")
        names = [c["name"] for c in cols]
        pk, fks = keys_from_ddl(ddl, dialect)
        out[table] = TableStructure(
            name=table,
            columns=[(c["name"], c.get("type") or "") for c in cols],
            primary_key=[p for p in pk if p.lower() in {n.lower() for n in names}],
            foreign_keys=fks,
            note=table_note(ddl),
            column_notes=column_notes_from_ddl(ddl, names),
        )
    return out


# ---------- 取值特征 ----------

_DATE_NAME = re.compile(
    r"(_at|_date|date|time|_on|_dt|_day|_ts|timestamp)$|^(date|time|day|dt|ts)$|^(date|time)|时间|日期", re.IGNORECASE
)
# (正则, 格式说明, 是否可以按字符串比较大小——只有这类格式的 MIN/MAX 才是真实的起止)
_DATE_FORMATS = [
    (re.compile(r"^\d{4}-\d{2}-\d{2}$"), "YYYY-MM-DD", True),
    (re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"), "YYYY-MM-DD HH:MM:SS", True),
    (re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}$"), "YYYY-MM-DD HH:MM", True),
    (re.compile(r"^\d{4}-\d{2}$"), "YYYY-MM", True),
    (re.compile(r"^\d{4}/\d{2}/\d{2}$"), "YYYY/MM/DD", True),
    (re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$"), "YYYY/M/D（不补零）", False),
    (re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$"), "MM/DD/YYYY 或 DD/MM/YYYY", False),
    (re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$"), "YYYY年M月D日", False),
]
_COMPACT_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
# 试编译遇到这些错误说明是临时故障（连不上、超时、被锁），不能据此判定条目失效
_TRANSIENT = re.compile(
    r"连接失败|database is locked|database is busy|interrupted|timeout|timed out|could not connect|"
    r"connection (refused|reset|lost|closed)|lost connection|server closed|too many connections|"
    r"canceling statement",
    re.IGNORECASE,
)
_LONG_TEXT = 60  # 平均长度超过它的文本列（备注、评论）不给样例
_MAX_VALUE_CHARS = 40
_STATS_CHUNK = 40  # 一条统计 SQL 最多带几列


@dataclass
class ColumnProfile:
    name: str
    kind: str  # enum / date / text / longtext / number / key / empty / other
    non_null: int = 0
    distinct: int = 0
    null_ratio: float = 0.0
    values: list[str] = field(default_factory=list)  # 枚举取值，按出现次数从多到少
    complete: bool = False  # values 是否就是全部取值
    date_format: str = ""
    date_min: str = ""
    date_max: str = ""
    examples: list[str] = field(default_factory=list)  # 格式样例


@dataclass
class Relation:
    column: str
    ref_table: str
    ref_column: str
    source: str  # declared（建表语句声明）/ inferred（按列名推断，并用数据核对过）
    match: float = 1.0  # 推断关联：抽样值能在被引用列里找到的比例

    def text(self) -> str:
        how = "外键" if self.source == "declared" else f"按列名推断，抽样核对 {self.match:.0%} 能对上"
        return f"关联 {self.ref_table}.{self.ref_column}（{how}）"


@dataclass
class TableProfile:
    table: str
    row_count: int
    sampled: int
    columns: dict[str, ColumnProfile] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "TableProfile":
        d = json.loads(text)
        return cls(
            table=d["table"],
            row_count=d.get("row_count", 0),
            sampled=d.get("sampled", 0),
            columns={k: ColumnProfile(**v) for k, v in (d.get("columns") or {}).items()},
        )


def _type_class(declared: str) -> str:
    t = (declared or "").lower()
    if not t:
        return "any"  # SQLite 不声明类型的列
    if "bool" in t:
        return "bool"
    if "date" in t or "time" in t:
        return "date"
    if "int" in t:
        return "int"
    if any(k in t for k in ("real", "floa", "doub", "dec", "num", "money")):
        return "real"
    if "blob" in t or "binary" in t or "bytea" in t:
        return "blob"
    if any(k in t for k in ("char", "text", "clob", "string", "enum", "uuid", "json")):
        return "text"
    return "any"


def short_value(value) -> str:
    text = str(value)
    return text[:_MAX_VALUE_CHARS] + "…" if len(text) > _MAX_VALUE_CHARS else text


def detect_date_format(values: list, name_hint: bool) -> tuple[str, bool] | None:
    """所有样本值都符合同一种日期写法时返回 (格式, 能否按字符串比较)；否则 None。"""
    texts = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if not texts:
        return None
    for pattern, label, sortable in _DATE_FORMATS:
        if all(pattern.match(t) for t in texts):
            return label, sortable
    if name_hint:
        compact = [_COMPACT_DATE.match(t) for t in texts]
        if all(m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31 for m in compact):
            return "YYYYMMDD（8 位数字）", True
        if all(t.isdigit() and 9 <= len(t) <= 10 for t in texts):
            return "Unix 时间戳（秒）", True
        if all(t.isdigit() and 12 <= len(t) <= 13 for t in texts):
            return "Unix 时间戳（毫秒）", True
    return None


class Profiler:
    """对一张表做只读统计。每条 SQL 先过守卫（白名单只含本次涉及的表），再交给只读连接执行。"""

    def __init__(self, db, sample_rows: int = 20000, enum_max: int = 30):
        self.db = db
        self.dialect = getattr(db, "dialect", "sqlite")
        self.sample_rows = max(100, int(sample_rows))
        self.enum_max = max(2, min(int(enum_max), 150))

    # ---- 基础 ----

    def q(self, name: str) -> str:
        return exp.to_identifier(name, quoted=True).sql(dialect=self.dialect)

    def run(self, sql: str, tables: set[str], max_rows: int = 200):
        verdict = validate(sql, allowed_tables=tables, max_rows=max_rows, dialect=self.dialect)
        if not verdict.allowed:
            return None
        return self.db.run_query(verdict.sql)

    def _rows(self, sql: str, tables: set[str], max_rows: int = 200) -> list[tuple]:
        result = self.run(sql, tables, max_rows)
        return list(result.rows) if result is not None and result.ok else []

    def _sample(self, table: str, columns: list[str], where: str = "") -> str:
        cols = ", ".join(self.q(c) for c in columns)
        return f"(SELECT {cols} FROM {self.q(table)}{where} LIMIT {self.sample_rows}) AS _dq_s"

    # ---- 单表 ----

    def bounded_count(self, table: str, cap: int) -> int | None:
        """行数，最多数到 cap（大表不做全表 COUNT）。查询失败返回 None——和"真的是空表"区分开。"""
        result = self.run(f"SELECT COUNT(*) FROM (SELECT 1 FROM {self.q(table)} LIMIT {int(cap)}) AS _dq_c", {table}, 1)
        if result is None or not result.ok or not result.rows:
            return None
        return int(result.rows[0][0] or 0)

    def profile_table(self, structure: TableStructure, key_columns: set[str] | None = None) -> TableProfile:
        """key_columns：主键、外键和推断出的关联列——它们的取值没有业务含义，不列出来。

        行数只数到"抽样上限 + 1"：够判断抽样是否覆盖全表，大表也不做全表 COUNT。
        数行数都失败（超时、连接断了）时抛 ProfileError：这张表不存档，下次同步重试，
        而不是存一份"空表"的错误档案。"""
        table = structure.name
        row_count = self.bounded_count(table, self.sample_rows + 1)
        if row_count is None:
            raise ProfileError(f"{table} 读不出行数（超时或连接失败）")
        profile = TableProfile(table=table, row_count=row_count, sampled=min(row_count, self.sample_rows))
        keys = {c.lower() for c in (key_columns or set())} | {c.lower() for c in structure.primary_key}
        keys |= {c.lower() for c, _t, _r in structure.foreign_keys}
        names = structure.column_names
        if row_count == 0 or not names:
            for name in names:
                profile.columns[name] = ColumnProfile(name=name, kind="empty")
            return profile

        stats: dict[str, tuple[int, int]] = {}
        sampled = profile.sampled
        for i in range(0, len(names), _STATS_CHUNK):
            chunk = names[i : i + _STATS_CHUNK]
            got = self._stats(table, chunk)
            if got is None and len(chunk) > 1:
                # 一批里只要有一列不支持 COUNT(DISTINCT)（PG 的 json / xml / point），整批都会失败：逐列重试
                for c in chunk:
                    one = self._stats(table, [c])
                    if one is not None:
                        sampled = one[0]
                        stats[c] = one[1][c]
                continue
            if got is not None:
                sampled = got[0]
                stats.update(got[1])
        profile.sampled = sampled

        for name in names:
            non_null, distinct = stats.get(name, (0, 0))
            col = ColumnProfile(name=name, kind="other", non_null=non_null, distinct=distinct)
            col.null_ratio = round(1 - non_null / sampled, 3) if sampled else 0.0
            if name not in stats:
                profile.columns[name] = col
                continue
            if non_null == 0:
                col.kind = "empty"
            elif name.lower() in keys:
                col.kind = "key"
            else:
                self._classify(structure, col, covers_all=row_count <= self.sample_rows)
            profile.columns[name] = col
        return profile

    def _stats(self, table: str, columns: list[str]) -> tuple[int, dict[str, tuple[int, int]]] | None:
        parts = ["COUNT(*)"]
        for c in columns:
            parts += [f"COUNT({self.q(c)})", f"COUNT(DISTINCT {self.q(c)})"]
        got = self._rows(f"SELECT {', '.join(parts)} FROM {self._sample(table, columns)}", {table}, 1)
        if not got:
            return None
        row = got[0]
        return int(row[0] or 0), {c: (int(row[1 + 2 * j] or 0), int(row[2 + 2 * j] or 0)) for j, c in enumerate(columns)}

    def _classify(self, structure: TableStructure, col: ColumnProfile, covers_all: bool) -> None:
        table, name = structure.name, col.name
        tclass = _type_class(structure.column_type(name))
        name_hint = bool(_DATE_NAME.search(name))
        if tclass == "blob":
            col.kind = "other"
            return

        # 日期：声明了日期类型，或者样本值全部符合某种日期写法（整数列只在列名像日期时才看）
        samples: list | None = None
        if tclass == "date" or tclass in ("text", "any") or (tclass == "int" and name_hint):
            samples = self._distinct_samples(table, name, 12)
            fmt = detect_date_format(samples, name_hint) if samples else None
            if tclass == "date" and fmt is None and samples:
                fmt = ("数据库日期类型", True)
            if fmt is not None:
                col.kind, col.date_format = "date", fmt[0]
                if fmt[1]:
                    got = self._rows(
                        f"SELECT MIN({self.q(name)}), MAX({self.q(name)}) FROM {self.q(table)} "
                        f"WHERE {self.q(name)} IS NOT NULL",
                        {table},
                        1,
                    )
                    if got and got[0][0] is not None:
                        col.date_min, col.date_max = str(got[0][0]), str(got[0][1])
                return

        numeric = tclass in ("int", "real") or (
            bool(samples) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in samples)
        )
        # 枚举：不同值很少。数值列要求取值有重复（否则多半是编号、金额），且不超过 12 个
        if numeric:
            is_enum = tclass != "real" and col.distinct <= 12 and col.distinct * 2 <= col.non_null
        else:
            is_enum = col.distinct <= self.enum_max and tclass in ("text", "any", "bool")
        if is_enum:
            sub = self._sample(table, [name])
            result = self.run(
                f"SELECT {self.q(name)}, COUNT(*) AS n FROM {sub} WHERE {self.q(name)} IS NOT NULL "
                f"GROUP BY {self.q(name)} ORDER BY n DESC LIMIT {self.enum_max}",
                {table},
                self.enum_max,
            )
            got = list(result.rows) if result is not None and result.ok else []
            values = [v for v, _n in got if v is not None]
            if values and not numeric and sum(len(str(v)) for v in values) / len(values) > _LONG_TEXT:
                col.kind = "longtext"
                return
            col.kind = "enum"
            col.values = [short_value(v) for v in values]
            # 结果被截断（SQL_MAX_ROWS 比枚举上限还小）时，列出来的就不是全部取值
            col.complete = covers_all and col.distinct <= len(values) and not (result is not None and result.truncated)
            return

        if numeric:
            col.kind = "number"
            return
        examples = (samples if samples is not None else self._distinct_samples(table, name, 3))[:3]
        if examples and sum(len(str(v)) for v in examples) / len(examples) > _LONG_TEXT:
            col.kind = "longtext"
            return
        col.kind = "text"
        col.examples = [short_value(v) for v in examples]

    def _distinct_samples(self, table: str, column: str, n: int) -> list:
        qc = self.q(column)
        sub = self._sample(table, [column], where=f" WHERE {qc} IS NOT NULL")
        return [r[0] for r in self._rows(f"SELECT DISTINCT {qc} FROM {sub}", {table}, n)]

    # ---- 关联 ----

    def infer_relations(
        self, structures: dict[str, TableStructure], targets: set[str] | None = None
    ) -> dict[str, list[Relation]]:
        """每张表的关联：建表语句声明的外键直接采用；没声明的按列名找候选，再用数据核对。

        targets：只计算涉及这些表的关联（增量同步时只算变了的表），None 表示全部。
        """
        by_lower = {t.lower(): t for t in structures}
        pk_of = {t: _single_pk(s) for t, s in structures.items()}
        counts: dict[str, int] = {}
        out: dict[str, list[Relation]] = {}
        for table, s in structures.items():
            rels: list[Relation] = []
            declared_cols = set()
            for col, ref_table, ref_col in s.foreign_keys:
                real = by_lower.get(ref_table.lower())
                if real is None or not s.has_column(col):
                    continue  # 引用了不存在的表：悬空外键不当关联
                ref_col = structures[real].real_column(ref_col) if ref_col else pk_of.get(real)
                if not ref_col:
                    continue
                if targets is None or table in targets or real in targets:
                    rels.append(Relation(s.real_column(col) or col, real, ref_col, "declared"))
                declared_cols.add(col.lower())
            for col in s.column_names:
                low = col.lower()
                # 主键列也可能引用别的表（BIRD 里 frpm.CDSCode 既是主键又指向 schools.CDSCode 的一对一表），
                # 方向由数据决定：只有"本表的值几乎都能在对方找到"才算关联
                if low in declared_cols or (low == "id" and low in {p.lower() for p in s.primary_key}):
                    continue
                candidates = []
                for other, pk in pk_of.items():
                    if other == table or not pk:
                        continue
                    if targets is not None and table not in targets and other not in targets:
                        continue
                    if not _name_matches(col, other, pk):
                        continue
                    rate = self._containment(table, col, other, pk)
                    if rate is not None and rate >= 0.95:
                        if other not in counts:
                            counts[other] = self.bounded_count(other, 1_000_001) or 0
                        candidates.append((counts[other], other, pk, rate))
                if candidates:
                    # 几张表都对得上（BIRD 里一组一对一表共用 CDSCode）：认行数最多的那张，它是主表
                    _n, other, pk, rate = max(candidates, key=lambda c: (c[0], c[1]))
                    rels.append(Relation(col, other, pk, "inferred", round(rate, 3)))
            if rels:
                out[table] = rels
        return out

    def _containment(self, table: str, column: str, ref_table: str, ref_column: str) -> float | None:
        qc, qr = self.q(column), self.q(ref_column)
        sub = f"(SELECT {qc} FROM {self.q(table)} WHERE {qc} IS NOT NULL LIMIT {self.sample_rows}) AS _dq_s"
        sql = (
            f"SELECT COUNT(*), SUM(CASE WHEN EXISTS (SELECT 1 FROM {self.q(ref_table)} AS _dq_r "
            f"WHERE _dq_r.{qr} = _dq_s.{qc}) THEN 1 ELSE 0 END) FROM {sub}"
        )
        got = self._rows(sql, {table, ref_table}, 1)
        if not got or not got[0][0]:
            return None
        return float(got[0][1] or 0) / float(got[0][0])

    # ---- 供复核用的两个探针 ----

    def value_exists(self, table: str, column: str, value: str) -> bool | None:
        """这个取值在列里还有没有（精确匹配）。查不了时返回 None，调用方不据此下结论。"""
        literal = exp.Literal.string(str(value)).sql(dialect=self.dialect)
        result = self.run(f"SELECT 1 FROM {self.q(table)} WHERE {self.q(column)} = {literal}", {table}, 1)
        if result is None:
            return None
        if result.ok:
            return True
        return False if result.error_kind == "empty_result" else None

    def dry_run(self, sql: str, allowed: set[str]) -> tuple[bool | None, str]:
        """只编译不取数：同一条 SQL 加上 LIMIT 0 执行。

        返回 (True, "") 能编译；(False, 原因) 表 / 列不存在、语法错误或越权——这类才说明条目失效；
        (None, 原因) 超时、连接失败这类临时故障——不能据此改条目的状态。"""
        verdict = validate(sql, allowed_tables=allowed, max_rows=0, dialect=self.dialect)
        if not verdict.allowed:
            return False, verdict.reason or "守卫拒绝"
        result = self.db.run_query(verdict.sql)
        if result.ok or result.error_kind == "empty_result":
            return True, ""
        reason = f"{result.error_kind}: {(result.error_message or '')[:120]}"
        if result.error_kind == "timeout" or _TRANSIENT.search(result.error_message or ""):
            return None, reason
        return False, reason  # 列名歧义、函数 / 运算符不存在这类编译错误同样说明条目失效


def _single_pk(s: TableStructure) -> str | None:
    if len(s.primary_key) == 1:
        return s.real_column(s.primary_key[0])
    if not s.primary_key:  # PG 伪 DDL 没有键信息：按惯例认 id 列
        return s.real_column("id")
    return None


def _singulars(table: str) -> set[str]:
    t = table.lower()
    out = {t}
    if t.endswith("ies"):
        out.add(t[:-3] + "y")
    if t.endswith("es"):
        out.add(t[:-2])
    if t.endswith("s"):
        out.add(t[:-1])
    return out


def _norm_name(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", name.lower())


def _name_matches(column: str, ref_table: str, ref_pk: str) -> bool:
    """列名像不像在引用另一张表的主键（只是候选，最终要数据核对）：
    - 主键叫 id：列名是 <表名单数>_id / <表名单数>id；
    - 主键有专门的名字（BIRD 常见的 CDSCode）：忽略大小写和下划线后相同，或者一个是另一个的前缀
      （satscores.cds → schools.CDSCode、events.cds_code → schools.CDSCode）。"""
    col, pk = _norm_name(column), _norm_name(ref_pk)
    if pk == "id":
        return any(col in (f"{_norm_name(s)}id",) for s in _singulars(ref_table))
    if col == pk:
        return True
    short, long_ = sorted((col, pk), key=len)
    return len(short) >= 3 and long_.startswith(short)
