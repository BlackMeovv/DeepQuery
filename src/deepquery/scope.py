"""把 SQL 翻译成一行看得懂的"统计口径"：筛选了什么、怎么分组、怎么排序、取多少条。

完全基于 sqlglot 语法树，不调用模型：口径说明本身不会编造。
列名尽量换成建表注释里的中文名（status → 订单状态），枚举值换成注释里的中文（canceled → 已取消），
看不懂 SQL 的业务人员也能核对"算的是不是我想要的"。
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

_COL_LINE = re.compile(r'^\s*[`"]?(\w+)[`"]?\s+[A-Za-z][^\n]*?--\s*(.+?)\s*$')
_MORE_COMMENT = re.compile(r"^\s*--\s*(.+?)\s*$")
_ENUM_PAIR = re.compile(r"([A-Za-z_][\w-]*)\s+([一-鿿]{2,8})")
_DATE_LITERAL = re.compile(r"^\d{4}-\d{2}(-\d{2})?")
_DATE_NAME = re.compile(r"(_at|_date|date|time|_on)$|时间|日期", re.IGNORECASE)
_MAX_EXPR = 36

Labels = dict[tuple[str, str], tuple[str, dict[str, str]]]


def column_labels(docs: dict[str, str]) -> Labels:
    """从建表注释里取列的中文名和枚举值的中文：{(表, 列): (中文名, {取值: 中文})}。"""
    out: Labels = {}
    for table, doc in docs.items():
        ddl = doc.split(";", 1)[0]
        comments: dict[str, str] = {}
        last = None
        for line in ddl.splitlines()[1:]:
            m = _COL_LINE.match(line)
            if m:
                last = m.group(1)
                comments[last] = m.group(2)
                continue
            more = _MORE_COMMENT.match(line)
            if more and last:
                comments[last] += " " + more.group(1)
            else:
                last = None
        for column, comment in comments.items():
            label = re.split(r"[:：,，（(;；'\s]", comment.lstrip("→ "), maxsplit=1)[0]
            if comment.startswith("→") or not (1 <= len(label) <= 10) or not re.search(r"[一-鿿]", label):
                label = ""
            pairs = dict(_ENUM_PAIR.findall(comment))
            out[(table.lower(), column.lower())] = (label, pairs if len(pairs) >= 2 else {})
        # 表自己的中文名（"订单表" → 订单），COUNT(DISTINCT o.id) 可以说成"不重复的订单数"
        first = doc.splitlines()[0] if doc else ""
        note = first.split("--", 1)[1].strip() if "--" in first else ""
        name = re.split(r"[:：,，（(；;]", note, maxsplit=1)[0].strip()
        name = re.sub(r"(维表|表)$", "", name)
        if 1 <= len(name) <= 8:
            out[(table.lower(), "*")] = (name, {})
    return out


def _short(node) -> str:
    text = node.sql()
    return text if len(text) <= _MAX_EXPR else text[: _MAX_EXPR - 1] + "…"


class _Describer:
    def __init__(self, tree: exp.Expression, labels: Labels):
        self.labels = labels
        self.alias: dict[str, str] = {}
        self.tables: list[str] = []
        for t in tree.find_all(exp.Table):
            real = t.name.lower()
            self.alias[t.alias_or_name.lower()] = real
            self.alias[real] = real
            if real not in self.tables:
                self.tables.append(real)
        self.select_alias: dict[str, str] = {}  # 表达式 SQL → 别名
        self.alias_expr: dict[str, exp.Expression] = {}  # 别名 → 表达式

    # ---------- 名字与取值 ----------

    def _column_key(self, col: exp.Column) -> tuple[str, str] | None:
        name = col.name.lower()
        if col.table:
            table = self.alias.get(col.table.lower())
            return (table, name) if table else None
        owners = [t for t in self.tables if (t, name) in self.labels]
        return (owners[0], name) if len(owners) == 1 else None

    def name(self, node) -> str:
        # 括号、类型转换、sqlglot 为 strftime 补的时间戳转换，口径上都等同于里面那一列
        if isinstance(node, (exp.Paren, exp.Cast, exp.TsOrDsToTimestamp, exp.TsOrDsToDate)):
            return self.name(node.this)
        if isinstance(node, exp.Column):
            key = self._column_key(node)
            label = self.labels.get(key, ("", {}))[0] if key else ""
            return label or node.name
        if isinstance(node, exp.Alias):
            return node.alias
        sql = node.sql().lower()
        if sql in self.select_alias:
            return self.select_alias[sql]
        fmt_col = self._strftime(node)
        if fmt_col:
            return fmt_col
        inner = self._date_wrapped(node)
        if inner is not None:
            return self.name(inner)
        # 取整、空值补 0 只是格式处理；差值的绝对值读作"差距"
        if isinstance(node, (exp.Round, exp.Coalesce)):
            return self.name(node.this)
        if isinstance(node, exp.Abs):
            inner = node.this.this if isinstance(node.this, exp.Paren) else node.this
            if isinstance(inner, exp.Sub):
                return f"{self.name(inner.this)}与{self.name(inner.expression)}的差距"
            return f"{self.name(inner)}的绝对值"
        for kind, word in ((exp.Sub, "减"), (exp.Add, "加"), (exp.Mul, "乘"), (exp.Div, "除以")):
            if isinstance(node, kind):
                return f"{self.name(node.this)}{word}{self.name(node.expression)}"
        for kind, word in ((exp.Sum, "总和"), (exp.Avg, "平均值"), (exp.Max, "最大值"), (exp.Min, "最小值")):
            if isinstance(node, kind):
                return f"{self.name(node.this)}的{word}"
        if isinstance(node, exp.Count):
            arg = node.this
            if isinstance(arg, exp.Star) or arg is None:
                return "行数"
            if isinstance(arg, exp.Distinct):
                target = arg.expressions[0]
                key = self._column_key(target) if isinstance(target, exp.Column) else None
                if key and key[1] == "id" and (key[0], "*") in self.labels:
                    return f"不重复的{self.labels[(key[0], '*')][0]}数"
                return f"不重复的{self.name(target)}数"
            return f"{self.name(arg)}的个数"
        return _short(node)

    @staticmethod
    def _date_wrapped(node):
        """date(x) / julianday(x) / DATE(x) 这类只改格式的包装，口径上等同于 x 本身。"""
        if isinstance(node, (exp.Date, exp.TsOrDsToDate)) and not node.expressions:
            return node.this
        if isinstance(node, exp.Anonymous) and node.name.lower() in ("date", "julianday", "datetime") and len(node.expressions) == 1:
            return node.expressions[0]
        return None

    def _strftime(self, node) -> str | None:
        if isinstance(node, (exp.TimeToStr, exp.StrToTime)) or (
            isinstance(node, exp.Anonymous) and node.name.lower() == "strftime"
        ):
            fmt = node.args.get("format")
            target = node.this
            if isinstance(node, exp.Anonymous) and len(node.expressions) >= 2:
                fmt, target = node.expressions[0], node.expressions[1]
            fmt_text = fmt.this if isinstance(fmt, exp.Literal) else ""
            unit = {"%Y-%m": "年月", "%Y": "年份", "%m": "月份", "%Y-%m-%d": "日期", "%H": "小时", "%w": "星期"}.get(fmt_text)
            if unit and target is not None:
                return f"{self.name(target)}的{unit}"
        return None

    def value(self, node, column=None) -> str:
        if isinstance(node, exp.Literal):
            if node.is_string and isinstance(column, exp.Column):
                key = self._column_key(column)
                mapped = self.labels.get(key, ("", {}))[1].get(node.this) if key else None
                return mapped or node.this
            return node.this
        if isinstance(node, (exp.Subquery, exp.Select)):
            return "子查询的结果"
        if isinstance(node, exp.Null):
            return "空"
        return self.name(node)

    def _is_date(self, *nodes) -> bool:
        for n in nodes:
            if isinstance(n, exp.Column) and not n.table and n.name.lower() in self.alias_expr:
                n = self.alias_expr[n.name.lower()]  # ORDER BY month：看别名背后的表达式
            if isinstance(n, (exp.TimeToStr, exp.StrToTime)) or self._strftime(n):
                return True
            if isinstance(n, exp.Literal) and n.is_string and _DATE_LITERAL.match(n.this):
                return True
            col = n if isinstance(n, exp.Column) else (self._date_wrapped(n) if n is not None else None)
            if isinstance(col, exp.Column) and _DATE_NAME.search(col.name):
                return True
            if isinstance(col, exp.Column) and _DATE_NAME.search(self.name(col)):
                return True
        return False

    # ---------- 条件 ----------

    _COMPARE = {
        exp.EQ: ("为", "为"),
        exp.NEQ: ("不是", "不是"),
        exp.GT: ("大于", "晚于"),
        exp.GTE: ("不小于", "不早于"),
        exp.LT: ("小于", "早于"),
        exp.LTE: ("不大于", "不晚于"),
    }

    def condition(self, node) -> str | None:
        if isinstance(node, exp.Paren):
            return self.condition(node.this)
        if isinstance(node, exp.And):
            parts = [self.condition(x) for x in node.flatten()]
            return "，".join(p for p in parts if p) or None
        if isinstance(node, exp.Or):
            parts = [self.condition(x) for x in node.flatten()]
            return f"（{' 或 '.join(parts)}）" if all(parts) else _short(node)
        if isinstance(node, exp.Not):
            inner = node.this.this if isinstance(node.this, exp.Paren) else node.this
            if isinstance(inner, exp.Is):
                return f"{self.name(inner.this)} 不为空"
            if isinstance(inner, exp.In):
                return f"{self.name(inner.this)} 不是 {self._in_values(inner)}"
            if isinstance(inner, exp.Exists):
                return f"没有对应的 {self._sub_tables(inner)} 记录"
            if isinstance(inner, exp.Like):
                return f"{self.name(inner.this)} 不包含 {self._like_text(inner.expression)}"
            return _short(node)
        for kind, (word, date_word) in self._COMPARE.items():
            if isinstance(node, kind):
                left, right = node.this, node.expression
                if isinstance(node, exp.EQ) and isinstance(left, exp.Column) and isinstance(right, exp.Column):
                    return None  # 连接条件，不是筛选
                if isinstance(left, exp.Literal) and not isinstance(right, exp.Literal):
                    # "5 <= a" 读作"a 不小于 5"：交换两边时比较方向也要翻过来
                    left, right = right, left
                    flipped = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}.get(kind, kind)
                    word, date_word = self._COMPARE[flipped]
                verb = date_word if self._is_date(left, right) else word
                return f"{self.name(left)} {verb} {self.value(right, left)}"
        if isinstance(node, exp.In):
            if node.args.get("query"):
                return f"{self.name(node.this)} 在子查询的结果里"
            return f"{self.name(node.this)} 是 {self._in_values(node)}"
        if isinstance(node, exp.Between):
            return f"{self.name(node.this)} 在 {self.value(node.args['low'], node.this)} 到 {self.value(node.args['high'], node.this)} 之间"
        if isinstance(node, exp.Like):
            pattern = node.expression.this if isinstance(node.expression, exp.Literal) else ""
            text = self._like_text(node.expression)
            if node.args.get("negate"):  # sqlglot 把 "x NOT LIKE y" 解析成带 negate 标记的 Like
                return f"{self.name(node.this)} 不包含 {text}"
            if pattern.startswith("%") and pattern.endswith("%"):
                return f"{self.name(node.this)} 包含 {text}"
            if pattern.endswith("%"):
                return f"{self.name(node.this)} 以 {text} 开头"
            if pattern.startswith("%"):
                return f"{self.name(node.this)} 以 {text} 结尾"
            return f"{self.name(node.this)} 为 {text}"
        if isinstance(node, exp.Is):
            return f"{self.name(node.this)} 为空"
        if isinstance(node, exp.Exists):
            return f"存在对应的 {self._sub_tables(node)} 记录"
        return _short(node)

    def _in_values(self, node: exp.In) -> str:
        if node.args.get("query"):
            return "子查询的结果"
        values = [self.value(v, node.this) for v in node.expressions]
        return values[0] if len(values) == 1 else "、".join(values[:6]) + ("等" if len(values) > 6 else "")

    @staticmethod
    def _like_text(node) -> str:
        return node.this.strip("%") if isinstance(node, exp.Literal) else _short(node)

    @staticmethod
    def _sub_tables(node) -> str:
        names = sorted({t.name for t in node.find_all(exp.Table)})
        return "、".join(names) or "子查询"


def _population_selects(tree: exp.Expression) -> list[exp.Select]:
    """决定统计范围的 SELECT：主查询、CTE、FROM 里的子查询；
    EXISTS / IN / 标量子查询里的条件是比较用的，不算统计范围。"""
    out = []
    for select in tree.find_all(exp.Select):
        node, keep = select, True
        while node.parent is not None:
            parent = node.parent
            if isinstance(parent, exp.Exists) or (isinstance(parent, exp.In) and node is parent.args.get("query")):
                keep = False
                break
            if isinstance(node, exp.Subquery) and not isinstance(parent, (exp.From, exp.Join)):
                keep = False
                break
            node = parent
        if keep:
            out.append(select)
    return out


def describe(sql: str | None, dialect: str = "sqlite", labels: Labels | None = None) -> list[str]:
    """SQL → 口径说明的几段短语，如 ["筛选：订单状态 不是 已取消", "按 品类 分组", "按 销售额 从高到低", "取前 5 条"]。
    解析不了时返回空列表（界面上就不显示这一行）。"""
    if not sql:
        return []
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return []
    if tree is None:
        return []
    if not isinstance(tree, exp.Select):
        return ["合并了多个查询的结果"] if isinstance(tree, (exp.Union, exp.Except, exp.Intersect)) else []
    d = _Describer(tree, labels or {})
    for item in tree.expressions:
        if isinstance(item, exp.Alias):
            d.select_alias[item.this.sql().lower()] = item.alias
            d.alias_expr[item.alias.lower()] = item.this

    parts: list[str] = []
    filters: list[str] = []
    for select in _population_selects(tree):
        where = select.args.get("where")
        if where is not None:
            text = d.condition(where.this)
            if text and text not in filters:
                filters.append(text)
    if filters:
        parts.append("筛选：" + "；".join(filters))
    if tree.args.get("distinct"):
        parts.append("去重")
    # 先在子查询里分组、再在外层统计（"下过 2 单以上的客户有多少"）：把里层的分组也说出来
    for inner in _population_selects(tree):
        if inner is tree or inner.args.get("group") is None:
            continue
        text = "先按 " + "、".join(d.name(g) for g in inner.args["group"].expressions) + " 分组"
        having = inner.args.get("having")
        cond = d.condition(having.this) if having is not None else None
        parts.append(text + (f"，只保留 {cond} 的" if cond else ""))
    group = tree.args.get("group")
    if group is not None and group.expressions:
        parts.append("按 " + "、".join(d.name(g) for g in group.expressions) + " 分组")
    having = tree.args.get("having")
    if having is not None:
        text = d.condition(having.this)
        if text:
            parts.append("分组后只保留：" + text)
    order = tree.args.get("order")
    if order is not None and order.expressions:
        keys = []
        for o in order.expressions:
            target = o.this
            desc = bool(o.args.get("desc"))
            if d._is_date(target):
                keys.append(f"{d.name(target)} 从新到旧" if desc else f"{d.name(target)} 从旧到新")
            else:
                keys.append(f"{d.name(target)} 从高到低" if desc else f"{d.name(target)} 从低到高")
        parts.append("按 " + "，再按 ".join(keys))
    limit = tree.args.get("limit")
    if limit is not None and isinstance(limit.expression, exp.Literal):
        n = limit.expression.this
        parts.append(f"取前 {n} 条" if order is not None else f"只取 {n} 条")
    return parts
