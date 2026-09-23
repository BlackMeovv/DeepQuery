"""多数据库引擎支持：SQLite 文件 / MySQL / PostgreSQL 连接串即插即用。

    DB_PATH=data/demo/ecommerce.sqlite                    # SQLite 文件
    DB_PATH=mysql://readonly:pwd@localhost:3306/deepquery   # MySQL（uv sync --extra mysql）
    DB_PATH=postgres://readonly:pwd@localhost:5432/deepquery  # PostgreSQL（--extra postgres）

只读纵深在服务器引擎上的形态与 SQLite 不同：
1. 第一道仍是 sqlglot AST 守卫（只放行单条 SELECT，按方言解析）；
2. 第二道是会话级只读（MySQL: SET SESSION TRANSACTION READ ONLY /
   PG: default_transaction_read_only=on）+ 语句级超时；
3. 真正的硬边界是【数据库账号本身只授 SELECT 权限】——生产接入必须用只读账号，
   这是部署要求而非代码可以替你兜住的事，README 有强调。

所有后端实现同一鸭子类型接口：dialect / table_names / schema_by_table /
schema_text / run_query，agent 对引擎无感知。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .contract import QueryResult, plain_value
from .database import ReadOnlyDatabase

_MYSQL_PREFIXES = ("mysql://", "mysql+pymysql://")
_POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def is_server_dsn(target: str) -> bool:
    return str(target).startswith(_MYSQL_PREFIXES + _POSTGRES_PREFIXES)


def open_database(target: str | Path, timeout_seconds: float = 15, max_rows: int = 200):
    """按目标形态选择引擎：URL → MySQL/PostgreSQL，其余按 SQLite 文件路径。"""
    target = str(target)
    if target.startswith(_MYSQL_PREFIXES):
        return MySQLDatabase(target, timeout_seconds=timeout_seconds, max_rows=max_rows)
    if target.startswith(_POSTGRES_PREFIXES):
        return PostgresDatabase(target, timeout_seconds=timeout_seconds, max_rows=max_rows)
    return ReadOnlyDatabase(target, timeout_seconds=timeout_seconds, max_rows=max_rows)


def _truncate_cell(value, max_cell: int = 60) -> str:
    text = "NULL" if value is None else str(value)
    return text[:max_cell] + "…" if len(text) > max_cell else text


class _ServerDatabase:
    """MySQL/PG 共用骨架。connect_fn 可注入（单元测试不需要真实服务器）。"""

    dialect = "base"

    def __init__(
        self,
        url: str,
        timeout_seconds: float = 15,
        max_rows: int = 200,
        connect_fn: Callable | None = None,
    ):
        self.url = url
        parsed = urlparse(url)
        self.host = parsed.hostname or "localhost"
        self.port = parsed.port or self._default_port
        self.user = parsed.username or ""
        self.password = parsed.password or ""
        self.database = (parsed.path or "/").lstrip("/")
        if not self.database:
            raise ValueError(f"连接串缺少数据库名: {url}")
        self.timeout_seconds = timeout_seconds
        self.max_rows = max_rows
        self._connect_fn = connect_fn or self._default_connect

    # 子类提供
    _default_port: int = 0

    def _default_connect(self):
        raise NotImplementedError

    def _session_setup_statements(self) -> list[str]:
        raise NotImplementedError

    def _classify_error(self, message: str) -> str:
        raise NotImplementedError

    def _connect(self):
        conn = self._connect_fn()
        cursor = conn.cursor()
        try:
            for statement in self._session_setup_statements():
                cursor.execute(statement)
        finally:
            cursor.close()
        return conn

    # 服务器引擎的指纹检查要扫 information_schema，最多每隔这么久查一次
    fingerprint_ttl = 5.0

    def run_query(self, sql: str) -> QueryResult:
        start = time.monotonic()
        try:
            conn = self._connect()
        except Exception as e:
            return QueryResult(
                ok=False,
                error_kind="execution_error",
                error_message=f"连接失败: {e}",
                latency_ms=int((time.monotonic() - start) * 1000),
            )
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                columns = [d[0] for d in cursor.description] if cursor.description else []
                rows = cursor.fetchmany(self.max_rows + 1)
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
            finally:
                cursor.close()
        except Exception as e:
            return QueryResult(
                ok=False,
                latency_ms=int((time.monotonic() - start) * 1000),
                error_kind=self._classify_error(str(e)),
                error_message=str(e)[:500],
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _fetch_all(self, sql: str) -> list[tuple]:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                return cursor.fetchall()
            finally:
                cursor.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def schema_text(self, sample_rows: int = 3) -> str:
        return "\n\n".join(self.schema_by_table(sample_rows).values())

    _columns_schema_filter = ""  # 子类提供 information_schema 的库过滤条件

    def schema_fingerprint(self) -> str:
        """schema 版本指纹：对 information_schema 的表/列/类型清单做哈希。

        一次毫秒级查询；表结构不变则指纹稳定，数据增删不影响。"""
        import hashlib
        import json

        cols = self.table_columns()
        payload = json.dumps(cols, ensure_ascii=False, sort_keys=True)
        return f"{self.dialect}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"

    def table_columns(self) -> dict[str, list[dict]]:
        rows = self._fetch_all(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            f"WHERE {self._columns_schema_filter} ORDER BY table_name, ordinal_position"
        )
        out: dict[str, list[dict]] = {}
        for table, column, data_type in rows:
            out.setdefault(table, []).append({"name": column, "type": data_type or ""})
        return out

    def _sample_block(self, table: str, sample_rows: int) -> str:
        if sample_rows <= 0:
            return ""
        try:
            rows = self._fetch_all(f"SELECT * FROM {self._quote(table)} LIMIT {int(sample_rows)}")
        except Exception:
            return ""
        if not rows:
            return ""
        lines = [", ".join(_truncate_cell(v) for v in row) for row in rows]
        body = "\n--   ".join(lines)
        return f"-- {table} 样例行:\n--   {body}"

    def _quote(self, identifier: str) -> str:
        raise NotImplementedError


class MySQLDatabase(_ServerDatabase):
    dialect = "mysql"
    _default_port = 3306
    _columns_schema_filter = "table_schema = DATABASE()"

    def _default_connect(self):
        try:
            import pymysql
        except ImportError as e:
            raise RuntimeError("接入 MySQL 需要安装驱动：uv sync --extra mysql") from e
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
            charset="utf8mb4",
            connect_timeout=10,
        )

    def _session_setup_statements(self) -> list[str]:
        return [
            "SET SESSION TRANSACTION READ ONLY",
            # 仅对 SELECT 生效的语句级超时（毫秒）
            f"SET SESSION max_execution_time = {int(self.timeout_seconds * 1000)}",
        ]

    def _classify_error(self, message: str) -> str:
        msg = message.lower()
        if "max_execution_time" in msg or "query execution was interrupted" in msg:
            return "timeout"
        if "doesn't exist" in msg and "table" in msg:
            return "no_such_table"
        if "unknown column" in msg:
            return "no_such_column"
        if "syntax" in msg and "error" in msg:
            return "syntax_error"
        if "read only" in msg or "read-only" in msg:
            return "guard_rejected"
        return "execution_error"

    def _quote(self, identifier: str) -> str:
        return f"`{identifier}`"

    def table_names(self) -> list[str]:
        rows = self._fetch_all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() ORDER BY table_name"
        )
        return [r[0] for r in rows]

    def schema_by_table(self, sample_rows: int = 3) -> dict[str, str]:
        out: dict[str, str] = {}
        for table in self.table_names():
            # SHOW CREATE TABLE 自带 COMMENT 子句——注释是检索选表的重要信号
            ddl_rows = self._fetch_all(f"SHOW CREATE TABLE {self._quote(table)}")
            ddl = ddl_rows[0][1] if ddl_rows else f"-- {table}"
            parts = [f"{ddl};"]
            sample = self._sample_block(table, sample_rows)
            if sample:
                parts.append(sample)
            out[table] = "\n".join(parts)
        return out


class PostgresDatabase(_ServerDatabase):
    dialect = "postgres"
    _default_port = 5432
    _columns_schema_filter = "table_schema = 'public'"

    def _default_connect(self):
        try:
            import psycopg
        except ImportError as e:
            raise RuntimeError("接入 PostgreSQL 需要安装驱动：uv sync --extra postgres") from e
        return psycopg.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=self.database,
            connect_timeout=10,
            autocommit=True,
        )

    def _session_setup_statements(self) -> list[str]:
        return [
            "SET default_transaction_read_only = on",
            f"SET statement_timeout = {int(self.timeout_seconds * 1000)}",
        ]

    def _classify_error(self, message: str) -> str:
        msg = message.lower()
        if "statement timeout" in msg or "canceling statement" in msg:
            return "timeout"
        if "does not exist" in msg and "relation" in msg:
            return "no_such_table"
        if "does not exist" in msg and "column" in msg:
            return "no_such_column"
        if "syntax error" in msg:
            return "syntax_error"
        if "read-only" in msg:
            return "guard_rejected"
        return "execution_error"

    def _quote(self, identifier: str) -> str:
        return f'"{identifier}"'

    def table_names(self) -> list[str]:
        rows = self._fetch_all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
        )
        return [r[0] for r in rows]

    def schema_by_table(self, sample_rows: int = 3) -> dict[str, str]:
        """PG 没有 SHOW CREATE TABLE：用 information_schema + 注释拼一份伪 DDL。"""
        out: dict[str, str] = {}
        for table in self.table_names():
            columns = self._fetch_all(
                "SELECT c.column_name, c.data_type, "
                "  col_description(format('%I.%I', c.table_schema, c.table_name)::regclass, c.ordinal_position) "
                "FROM information_schema.columns c "
                f"WHERE c.table_schema = 'public' AND c.table_name = '{table}' "
                "ORDER BY c.ordinal_position"
            )
            table_comment_rows = self._fetch_all(
                f"SELECT obj_description('public.\"{table}\"'::regclass)"
            )
            table_comment = table_comment_rows[0][0] if table_comment_rows else None
            lines = [f"CREATE TABLE {table} (" + (f"  -- {table_comment}" if table_comment else "")]
            for name, data_type, comment in columns:
                suffix = f"  -- {comment}" if comment else ""
                lines.append(f"    {name} {data_type},{suffix}")
            lines.append(");")
            parts = ["\n".join(lines)]
            sample = self._sample_block(table, sample_rows)
            if sample:
                parts.append(sample)
            out[table] = "\n".join(parts)
        return out
