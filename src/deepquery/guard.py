"""SQL 守卫：基于 sqlglot AST 的白名单校验 + 行数限额改写。

这是第一道防线（给出结构化拒绝理由、注入 LIMIT）；
第二道防线在 tools/database.py（只读连接 + authorizer + 超时中断），
即使守卫被绕过，数据库层也不可写——纵深防御，两层缺一不可。
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# 禁止出现在 AST 任何位置的节点类型（按名字取，兼容 sqlglot 版本差异）
_FORBIDDEN_NODE_NAMES = (
    "Insert",
    "Update",
    "Delete",
    "Drop",
    "Create",
    "Alter",
    "AlterTable",
    "Merge",
    "TruncateTable",
    "Grant",
    "Pragma",
    "Attach",
    "Detach",
    "Command",  # sqlglot 解析不了的裸命令兜底
    "Transaction",
    "Commit",
    "Rollback",
    "Set",
)
_FORBIDDEN_NODES = tuple(
    t for t in (getattr(exp, name, None) for name in _FORBIDDEN_NODE_NAMES) if t is not None
)

_SET_OPS = tuple(
    t for t in (getattr(exp, name, None) for name in ("Union", "Except", "Intersect")) if t is not None
)


@dataclass
class GuardVerdict:
    allowed: bool
    sql: str  # allowed 时为改写后的 SQL（已强制 LIMIT）
    error_kind: str | None = None  # syntax_error / guard_rejected
    reason: str | None = None

    @classmethod
    def reject(cls, sql: str, error_kind: str, reason: str) -> "GuardVerdict":
        return cls(allowed=False, sql=sql, error_kind=error_kind, reason=reason)


def validate(
    sql: str,
    allowed_tables: set[str],
    max_rows: int = 200,
    dialect: str = "sqlite",
) -> GuardVerdict:
    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        return GuardVerdict.reject(sql, "guard_rejected", "SQL 为空")

    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.SqlglotError as e:
        # SqlglotError 覆盖 ParseError 与 TokenError（未闭合引号等词法级畸形，
        # BIRD 真实跑批中出现过）——解析不了一律拒绝并交给修复循环，绝不冲出守卫
        return GuardVerdict.reject(sql, "syntax_error", f"SQL 解析失败: {e}")

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return GuardVerdict.reject(
            sql, "guard_rejected", f"只允许单条语句，实际解析出 {len(statements)} 条"
        )
    tree = statements[0]

    if not isinstance(tree, (exp.Select, *_SET_OPS)):
        return GuardVerdict.reject(
            sql,
            "guard_rejected",
            f"只允许 SELECT 查询，不允许 {tree.key.upper()} 类语句",
        )

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            return GuardVerdict.reject(
                sql, "guard_rejected", f"检测到被禁止的操作: {node.key.upper()}"
            )

    # 表白名单：排除 CTE 别名后，所有引用的表必须在白名单内
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    allowed_lower = {t.lower() for t in allowed_tables}
    for table in tree.find_all(exp.Table):
        if table.args.get("db") or table.args.get("catalog"):
            return GuardVerdict.reject(
                sql, "guard_rejected", f"不允许跨库/带模式前缀访问: {table.sql(dialect=dialect)}"
            )
        if not isinstance(table.this, exp.Identifier):
            return GuardVerdict.reject(
                sql, "guard_rejected", f"不允许表值函数: {table.sql(dialect=dialect)}"
            )
        name = table.name.lower()
        if name in cte_names:
            continue
        if name.startswith("sqlite_"):
            return GuardVerdict.reject(sql, "guard_rejected", f"不允许访问系统表: {table.name}")
        if name not in allowed_lower:
            return GuardVerdict.reject(
                sql,
                "guard_rejected",
                f"表 `{table.name}` 不在白名单内（可用表: {', '.join(sorted(allowed_tables))}）",
            )

    tree = _enforce_limit(tree, max_rows)
    return GuardVerdict(allowed=True, sql=tree.sql(dialect=dialect))


def _enforce_limit(tree: exp.Expression, max_rows: int) -> exp.Expression:
    """顶层强制 LIMIT：无 LIMIT 则加上；已有但超限/非字面量则改写为上限。"""
    limit_node = tree.args.get("limit")
    if limit_node is None:
        return tree.limit(max_rows)
    value = limit_node.expression
    if isinstance(value, exp.Literal) and value.is_int:
        if int(value.this) > max_rows:
            value.set("this", str(max_rows))
        return tree
    return tree.limit(max_rows)


def tables_in_sql(sql: str, dialect: str = "sqlite") -> set[str]:
    """提取 SQL 引用的真实表名（排除 CTE 别名，小写）。

    用于评测的选表召回率，以及回答下方展示"数据来自哪几张表"。
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError:
        return set()
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    return {
        t.name.lower()
        for t in tree.find_all(exp.Table)
        if isinstance(t.this, exp.Identifier) and t.name.lower() not in cte_names
    }
