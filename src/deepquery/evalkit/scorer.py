"""执行准确率（Execution Accuracy, EX）打分。

约定与 BIRD/Spider 的 EX 一致：比较两条 SQL 在同一库上的执行结果集，
不比较 SQL 文本本身。gold 无 ORDER BY 时按多重集合比较（行序无关），
有 ORDER BY 时按序比较。浮点按 4 位小数取整后比较。

并列容忍：gold 有 ORDER BY 但排序键出现并列时，gold 未指定决胜键的场合
组内任何顺序都是正确答案——按序比较失败后，把两侧按排序键切成连续分组、
组内按多重集合比较（排序键解析不到输出列时保持严格比较）。
已知局限：LIMIT 恰好切在并列组中间时，组内取哪些成员仍要求与 gold 一致。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp

from ..guard import tables_in_sql  # noqa: F401 —— 评测里算选表召回率用，保留原导入路径

_SCORER_MAX_ROWS = 10_000  # 打分不受 agent 行数限额影响，用更高的安全上限


@dataclass
class ExecutionScore:
    match: bool
    reason: str = ""
    pred_rows: int | None = None
    gold_rows: int | None = None


def _normalize_cell(value):
    if isinstance(value, float):
        return round(value, 4)
    return value


def _run(db_path: str | Path, sql: str) -> tuple[list[str], list[tuple]]:
    """返回 (列名, 行)。列信息取自 cursor.description——空结果集也有。"""
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        cur = conn.execute(sql)
        colnames = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(_SCORER_MAX_ROWS)
        return colnames, [tuple(_normalize_cell(v) for v in row) for row in rows]
    finally:
        conn.close()


def gold_order_matters(gold_sql: str) -> bool:
    try:
        tree = sqlglot.parse_one(gold_sql, read="sqlite")
    except sqlglot.errors.SqlglotError:
        return False
    return tree.args.get("order") is not None if isinstance(tree, exp.Expression) else False


def _order_key_indexes(gold_sql: str, colnames: list[str]) -> list[int] | None:
    """把 gold 顶层 ORDER BY 的每个表达式解析为输出列下标。

    支持序号（ORDER BY 2）与输出列名/别名；出现函数等复杂表达式或列名
    不在输出里时返回 None——此时无法定位排序键，保持严格按序比较。
    """
    try:
        tree = sqlglot.parse_one(gold_sql, read="sqlite")
    except sqlglot.errors.SqlglotError:
        return None
    order = tree.args.get("order")
    if order is None:
        return None
    lowered = [c.lower() for c in colnames]
    indexes: list[int] = []
    for ordered in order.expressions:
        e = ordered.this
        if isinstance(e, exp.Literal) and e.is_int:
            i = int(e.name) - 1
            if not 0 <= i < len(colnames):
                return None
            indexes.append(i)
        elif isinstance(e, exp.Column):
            name = e.name.lower()
            if name not in lowered:
                return None
            indexes.append(lowered.index(name))
        else:
            return None
    return indexes or None


def _tie_groups(rows: list[tuple], key_idx: list[int]) -> list[tuple[tuple, Counter]]:
    """按排序键把行切成连续分组，组内用多重集合表示（顺序无关）。"""
    groups: list[tuple[tuple, Counter]] = []
    for row in rows:
        key = tuple(row[i] for i in key_idx)
        if groups and groups[-1][0] == key:
            groups[-1][1][row] += 1
        else:
            groups.append((key, Counter([row])))
    return groups


def execution_match(db_path: str | Path, pred_sql: str | None, gold_sql: str) -> ExecutionScore:
    if not pred_sql:
        return ExecutionScore(match=False, reason="无预测 SQL")
    try:
        gold_cols, gold_rows = _run(db_path, gold_sql)
    except sqlite3.Error as e:
        # gold 本身跑不通是评测集的 bug，必须显式暴露而不是判 agent 错
        raise ValueError(f"gold SQL 执行失败（评测集数据问题）: {e}\n{gold_sql}") from e
    try:
        pred_cols, pred_rows = _run(db_path, pred_sql)
    except sqlite3.Error as e:
        return ExecutionScore(match=False, reason=f"预测 SQL 执行失败: {e}", gold_rows=len(gold_rows))

    score = ExecutionScore(match=False, pred_rows=len(pred_rows), gold_rows=len(gold_rows))
    if len(pred_cols) != len(gold_cols):
        score.reason = f"列数不一致: pred={len(pred_cols)}, gold={len(gold_cols)}"
        return score
    if gold_order_matters(gold_sql):
        score.match = pred_rows == gold_rows
        if not score.match and len(pred_rows) == len(gold_rows):
            # 并列容忍：排序键相同的连续分组内顺序无关
            key_idx = _order_key_indexes(gold_sql, gold_cols)
            if key_idx is not None:
                score.match = _tie_groups(pred_rows, key_idx) == _tie_groups(gold_rows, key_idx)
        score.reason = "" if score.match else "行内容或顺序不一致"
    else:
        score.match = Counter(pred_rows) == Counter(gold_rows)
        score.reason = "" if score.match else "结果多重集不一致"
    return score
