"""只读数据库工具：第二道防线。

三重保护，任何一层失效都不可写：
1. URI mode=ro 打开（文件级只读）
2. PRAGMA query_only=ON
3. sqlite authorizer 只放行 SELECT/READ/FUNCTION

超时：sqlite 没有单查询超时，用 progress_handler 在截止时间后中断，
表现为 OperationalError("interrupted")，归类为 timeout。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .contract import QueryResult, plain_value

MAX_VALUE_BYTES = 1_000_000  # 单个值（字符串/BLOB）上限，远大于任何正常的分析结果

_ALLOWED_AUTH_OPS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    # 子查询/CTE 会临时物化，需要允许（仍受 mode=ro 与 query_only 约束）
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


class ReadOnlyDatabase:
    dialect = "sqlite"

    def __init__(self, db_path: str | Path, timeout_seconds: float = 15, max_rows: int = 200):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"数据库不存在: {self.db_path}（演示库请先运行 `make demo-db` 生成）"
            )
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        # 行数有上限但单元格没有：SELECT hex(randomblob(1e8)) 能一条查询吃掉几百 MB 内存。
        # 限制单个字符串/BLOB 的长度，超限时报 "string or blob too big"
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)

        def authorizer(action, arg1, arg2, db_name, trigger):
            if action in _ALLOWED_AUTH_OPS:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        return conn

    # ---------- schema ----------

    def schema_fingerprint(self) -> str:
        """schema 版本指纹：任何 DDL（建/删/改表）都会使其变化，数据增删不会。

        Agent 每次提问前比对指纹，变了才重建 schema 上下文——PRAGMA 一次
        微秒级，重建（含样例行采样/检索索引）只在真正变更时发生。
        """
        conn = self._connect()
        try:
            conn.set_authorizer(None)  # PRAGMA 自省是自家代码路径，非模型输入
            version = conn.execute("PRAGMA schema_version").fetchone()[0]
        finally:
            conn.close()
        return f"sqlite:{version}"

    def table_names(self) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    def table_columns(self) -> dict[str, list[dict]]:
        """结构化的表→列清单（schema 浏览器用）。"""
        out: dict[str, list[dict]] = {}
        conn = self._connect()
        try:
            conn.set_authorizer(None)  # PRAGMA 自省是自家代码路径，非模型输入
            for name in [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall()
            ]:
                rows = conn.execute(f'PRAGMA table_info("{name}")').fetchall()
                out[name] = [{"name": r[1], "type": r[2] or ""} for r in rows]
        finally:
            conn.close()
        return out

    def schema_by_table(self, sample_rows: int = 3, max_cell: int = 60) -> dict[str, str]:
        """逐表的 schema 上下文：建表 DDL + 少量样例行（值格式很关键）。

        按表拆开是 Schema RAG 的基础——大库场景下只把检索命中的表喂给模型。
        样例单元格截断，防止 BIRD 这类库里的长文本撑爆上下文。
        """
        out: dict[str, str] = {}
        conn = self._connect()
        try:
            ddls = conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            for name, ddl in ddls:
                parts = [f"{ddl.strip()};"]
                if sample_rows > 0:
                    cur = conn.execute(f'SELECT * FROM "{name}" LIMIT {int(sample_rows)}')
                    cols = [d[0] for d in cur.description]
                    lines = [", ".join(cols)]
                    for row in cur.fetchall():
                        cells = []
                        for v in row:
                            text = "NULL" if v is None else str(v)
                            cells.append(text[:max_cell] + "…" if len(text) > max_cell else text)
                        lines.append(", ".join(cells))
                    sample = "\n--   ".join(lines)
                    parts.append(f"-- {name} 样例行:\n--   {sample}")
                out[name] = "\n".join(parts)
        finally:
            conn.close()
        return out

    def schema_text(self, sample_rows: int = 3) -> str:
        """全量 schema 上下文（小库直接全喂；大库走 Schema RAG 选表）。"""
        return "\n\n".join(self.schema_by_table(sample_rows).values())

    # ---------- query ----------

    def run_query(self, sql: str) -> QueryResult:
        """执行已通过守卫的 SQL。错误结构化分类，永不抛业务异常。"""
        start = time.monotonic()
        deadline = start + self.timeout_seconds
        conn = self._connect()
        try:
            # progress handler：每执行一批 VM 指令检查一次截止时间
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            cur = conn.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(self.max_rows + 1)
            truncated = len(rows) > self.max_rows
            rows = rows[: self.max_rows]
            latency_ms = int((time.monotonic() - start) * 1000)
            if not rows:
                return QueryResult(
                    ok=False,
                    columns=columns,
                    latency_ms=latency_ms,
                    error_kind="empty_result",
                    error_message="查询执行成功但返回 0 行",
                )
            return QueryResult(
                ok=True,
                columns=columns,
                rows=[tuple(plain_value(v) for v in r) for r in rows],
                row_count=len(rows),
                truncated=truncated,
                latency_ms=latency_ms,
            )
        except sqlite3.Error as e:
            latency_ms = int((time.monotonic() - start) * 1000)
            return QueryResult(
                ok=False,
                latency_ms=latency_ms,
                error_kind=_classify_sqlite_error(e),
                error_message=str(e),
            )
        finally:
            conn.close()


def _classify_sqlite_error(e: sqlite3.Error) -> str:
    msg = str(e).lower()
    if "interrupted" in msg:
        return "timeout"
    if "no such table" in msg:
        return "no_such_table"
    if "no such column" in msg:
        return "no_such_column"
    if "syntax error" in msg:
        return "syntax_error"
    if "not authorized" in msg or "readonly" in msg or "read-only" in msg or "query_only" in msg:
        return "guard_rejected"
    return "execution_error"
