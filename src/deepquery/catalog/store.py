"""档案库：表卡片、表 / 列说明、依赖索引、变更记录，存在 DeepQuery 自己的 SQLite 文件里。

业务库只读，档案写在这里：接入的库不会因为建档多出任何表或注释。
同一个档案库可以存多个业务库的档案，按 db_key（库的身份，不含密码）区分。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .profiler import Relation, TableProfile, TableStructure

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_tables (
    db_key TEXT NOT NULL,
    name TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    structure TEXT NOT NULL,
    profile TEXT,
    relations TEXT NOT NULL DEFAULT '[]',
    profiled_at REAL,
    synced_at REAL NOT NULL,
    PRIMARY KEY (db_key, name)
);
CREATE TABLE IF NOT EXISTS catalog_descriptions (
    db_key TEXT NOT NULL,
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (db_key, table_name, column_name)
);
CREATE TABLE IF NOT EXISTS catalog_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    db_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    sql TEXT NOT NULL DEFAULT '',
    refs TEXT NOT NULL DEFAULT '[]',
    vals TEXT NOT NULL DEFAULT '[]',
    cand TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    reason TEXT NOT NULL DEFAULT '',
    reason_kind TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    UNIQUE (db_key, kind, key)
);
CREATE TABLE IF NOT EXISTS catalog_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    db_key TEXT NOT NULL,
    at REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS catalog_events_db ON catalog_events (db_key, id);
CREATE TABLE IF NOT EXISTS catalog_meta (
    db_key TEXT NOT NULL,
    k TEXT NOT NULL,
    v TEXT NOT NULL,
    PRIMARY KEY (db_key, k)
);
"""

# 说明的来源优先级：人工 > 建表注释 > 模型生成。低优先级的来源不能覆盖高优先级的
SOURCE_RANK = {"llm": 0, "ddl": 1, "human": 2}


@dataclass
class StoredTable:
    name: str
    fingerprint: str
    structure: TableStructure
    profile: TableProfile | None
    relations: list[Relation]
    profiled_at: float | None


@dataclass
class Item:
    """依赖索引里的一条：例句（example）、业务口径（glossary）或用户记忆（memory）。"""

    kind: str
    key: str
    text: str
    sql: str = ""
    user_id: str = ""
    refs: list[str] = field(default_factory=list)
    vals: list[list[str]] = field(default_factory=list)
    cand: list[list[str]] = field(default_factory=list)  # 还没核对过"库里是否存在"的取值依赖（提问时登记的记忆）
    status: str = "active"  # active / review
    reason: str = ""
    reason_kind: str = ""  # schema（表或列没了）/ value（取值没了）/ sql（试编译失败）
    id: int = 0
    updated_at: float = 0.0


class CatalogStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # WAL：后台同步在写的时候，提问路径上的读不会被锁住
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(catalog_items)").fetchall()}
            if "cand" not in cols:  # 早期版本建的档案库
                conn.execute("ALTER TABLE catalog_items ADD COLUMN cand TEXT NOT NULL DEFAULT '[]'")

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---------- 表卡片 ----------

    def tables(self, db_key: str) -> dict[str, StoredTable]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, fingerprint, structure, profile, relations, profiled_at "
                "FROM catalog_tables WHERE db_key = ? ORDER BY name",
                (db_key,),
            ).fetchall()
        out = {}
        for name, fp, structure, profile, relations, profiled_at in rows:
            out[name] = StoredTable(
                name=name,
                fingerprint=fp,
                structure=TableStructure.from_json(structure),
                profile=TableProfile.from_json(profile) if profile else None,
                relations=[Relation(**r) for r in json.loads(relations or "[]")],
                profiled_at=profiled_at,
            )
        return out

    def fingerprint(self, db_key: str, table: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fingerprint FROM catalog_tables WHERE db_key = ? AND name = ?", (db_key, table)
            ).fetchone()
        return row[0] if row else None

    def save_table(
        self,
        db_key: str,
        structure: TableStructure,
        profile: TableProfile | None,
        relations: list[Relation],
    ) -> None:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO catalog_tables (db_key, name, fingerprint, structure, profile, relations, profiled_at, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (db_key, name) DO UPDATE SET fingerprint = excluded.fingerprint, "
                "structure = excluded.structure, profile = excluded.profile, relations = excluded.relations, "
                "profiled_at = excluded.profiled_at, synced_at = excluded.synced_at",
                (
                    db_key,
                    structure.name,
                    structure.fingerprint(),
                    structure.to_json(),
                    profile.to_json() if profile else None,
                    _relations_json(relations),
                    now if profile else None,
                    now,
                ),
            )

    def update_profile(self, db_key: str, table: str, profile: TableProfile, touch: bool = True) -> None:
        """touch=False：只补了一列的取值（查询时顺手查到的），不重置整张表的刷新时钟。"""
        with self._connect() as conn:
            if touch:
                conn.execute(
                    "UPDATE catalog_tables SET profile = ?, profiled_at = ? WHERE db_key = ? AND name = ?",
                    (profile.to_json(), time.time(), db_key, table),
                )
            else:
                conn.execute(
                    "UPDATE catalog_tables SET profile = ? WHERE db_key = ? AND name = ?",
                    (profile.to_json(), db_key, table),
                )

    def set_relations(self, db_key: str, table: str, relations: list[Relation]) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE catalog_tables SET relations = ? WHERE db_key = ? AND name = ?",
                (_relations_json(relations), db_key, table),
            )

    def delete_table(self, db_key: str, table: str) -> None:
        """删表时删掉档案和自动来源的说明；人工说明保留（ETL 先删后建很常见，表回来时说明还在）。"""
        with self._connect() as conn:
            conn.execute("DELETE FROM catalog_tables WHERE db_key = ? AND name = ?", (db_key, table))
            conn.execute(
                "DELETE FROM catalog_descriptions WHERE db_key = ? AND table_name = ? AND source != 'human'",
                (db_key, table),
            )

    # ---------- 说明 ----------

    def descriptions(self, db_key: str) -> dict[tuple[str, str], tuple[str, str]]:
        """{(表, 列或空串): (说明, 来源)}"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT table_name, column_name, text, source FROM catalog_descriptions WHERE db_key = ?",
                (db_key,),
            ).fetchall()
        return {(t, c): (text, src) for t, c, text, src in rows}

    def set_description(self, db_key: str, table: str, column: str, text: str, source: str) -> bool:
        """写入说明；已有更高优先级来源的说明时不覆盖（人工改过的，建档和模型都不会再动）。"""
        text = (text or "").strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT source FROM catalog_descriptions WHERE db_key = ? AND table_name = ? AND column_name = ?",
                (db_key, table, column or ""),
            ).fetchone()
            if row and SOURCE_RANK.get(row[0], 0) > SOURCE_RANK.get(source, 0):
                return False
            if not text:
                conn.execute(
                    "DELETE FROM catalog_descriptions WHERE db_key = ? AND table_name = ? AND column_name = ?",
                    (db_key, table, column or ""),
                )
                return True
            conn.execute(
                "INSERT INTO catalog_descriptions (db_key, table_name, column_name, text, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (db_key, table_name, column_name) DO UPDATE SET "
                "text = excluded.text, source = excluded.source, updated_at = excluded.updated_at",
                (db_key, table, column or "", text, source, time.time()),
            )
        return True

    def delete_description(self, db_key: str, table: str, column: str, keep_human: bool = False) -> None:
        sql = "DELETE FROM catalog_descriptions WHERE db_key = ? AND table_name = ? AND column_name = ?"
        if keep_human:
            sql += " AND source != 'human'"
        with self._connect() as conn:
            conn.execute(sql, (db_key, table, column or ""))

    # ---------- 依赖索引 ----------

    def items(self, db_key: str, kind: str | None = None, user_id: str | None = None) -> list[Item]:
        sql = (
            "SELECT id, kind, key, user_id, text, sql, refs, vals, cand, status, reason, reason_kind, updated_at "
            "FROM catalog_items WHERE db_key = ?"
        )
        args: list = [db_key]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY id", args).fetchall()
        return [
            Item(
                id=r[0], kind=r[1], key=r[2], user_id=r[3], text=r[4], sql=r[5],
                refs=json.loads(r[6] or "[]"), vals=json.loads(r[7] or "[]"), cand=json.loads(r[8] or "[]"),
                status=r[9], reason=r[10], reason_kind=r[11], updated_at=r[12],
            )
            for r in rows
        ]

    def item(self, db_key: str, item_id: int) -> Item | None:
        return next((i for i in self.items(db_key) if i.id == item_id), None)

    def upsert_item(self, db_key: str, item: Item) -> None:
        self.upsert_items(db_key, [item])

    def upsert_items(self, db_key: str, items: list[Item]) -> None:
        """批量写入（一个事务）：首次建档要登记上万条记忆时，逐条开连接太慢。"""
        if not items:
            return
        now = time.time()
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO catalog_items (db_key, kind, key, user_id, text, sql, refs, vals, cand, status, reason, reason_kind, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (db_key, kind, key) DO UPDATE SET "
                "user_id = excluded.user_id, text = excluded.text, sql = excluded.sql, refs = excluded.refs, "
                "vals = excluded.vals, cand = excluded.cand, status = excluded.status, reason = excluded.reason, "
                "reason_kind = excluded.reason_kind, updated_at = excluded.updated_at",
                [
                    (
                        db_key, item.kind, item.key, item.user_id, item.text, item.sql,
                        json.dumps(sorted(item.refs), ensure_ascii=False),
                        json.dumps(sorted(item.vals), ensure_ascii=False),
                        json.dumps(sorted(item.cand), ensure_ascii=False),
                        item.status, item.reason, item.reason_kind, now,
                    )
                    for item in items
                ],
            )

    _ITEM_FIELDS = {"refs", "vals", "cand", "status", "reason", "reason_kind"}

    def update_item(self, db_key: str, kind: str, key: str, **fields) -> None:
        """只改指定字段（不整行覆盖）：后台复核和提问时的登记可能同时在写同一条。"""
        bad = set(fields) - self._ITEM_FIELDS
        if bad:
            raise ValueError(f"不能更新的字段：{bad}")
        if not fields:
            return
        sets, args = [], []
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            args.append(json.dumps(sorted(v), ensure_ascii=False) if k in ("refs", "vals", "cand") else v)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE catalog_items SET {', '.join(sets)}, updated_at = ? WHERE db_key = ? AND kind = ? AND key = ?",
                (*args, time.time(), db_key, kind, key),
            )

    def set_status(self, db_key: str, kind: str, key: str, status: str, reason: str = "", reason_kind: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE catalog_items SET status = ?, reason = ?, reason_kind = ?, updated_at = ? "
                "WHERE db_key = ? AND kind = ? AND key = ?",
                (status, reason, reason_kind, time.time(), db_key, kind, key),
            )

    def delete_items(self, db_key: str, kind: str, keys: list[str]) -> None:
        if not keys:
            return
        with self._connect() as conn:
            conn.executemany(
                "DELETE FROM catalog_items WHERE db_key = ? AND kind = ? AND key = ?",
                [(db_key, kind, k) for k in keys],
            )

    def flagged(self, db_key: str) -> list[tuple[str, str, str]]:
        """待复核的条目：[(kind, key, user_id)]。"""
        with self._connect() as conn:
            return conn.execute(
                "SELECT kind, key, user_id FROM catalog_items WHERE db_key = ? AND status = 'review'",
                (db_key,),
            ).fetchall()

    # ---------- 版本号与待办（持久化：多个进程共用一个档案库时靠它互相看到对方的修改）----------

    def bump(self, db_key: str, name: str) -> int:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO catalog_meta (db_key, k, v) VALUES (?, ?, '1') "
                "ON CONFLICT (db_key, k) DO UPDATE SET v = CAST(CAST(v AS INTEGER) + 1 AS TEXT)",
                (db_key, f"version:{name}"),
            )
            row = conn.execute(
                "SELECT v FROM catalog_meta WHERE db_key = ? AND k = ?", (db_key, f"version:{name}")
            ).fetchone()
        return int(row[0])

    def versions(self, db_key: str) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT k, v FROM catalog_meta WHERE db_key = ? AND k LIKE 'version:%'", (db_key,)
            ).fetchall()
        return {k.split(":", 1)[1]: int(v) for k, v in rows}

    def acquire_lease(self, db_key: str, owner: str, ttl: float) -> bool:
        """跨进程的同步锁：多个进程（多个 worker、命令行）共用一个档案库时，同一时刻只让一个在同步。
        带过期时间：持有者被杀掉也不会永久卡住。同一持有者再次获取即续期。"""
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT v FROM catalog_meta WHERE db_key = ? AND k = 'sync_lease'", (db_key,)
            ).fetchone()
            if row:
                holder, _, expires = row[0].rpartition("|")
                if holder != owner and float(expires or 0) > now:
                    return False
            conn.execute(
                "INSERT INTO catalog_meta (db_key, k, v) VALUES (?, 'sync_lease', ?) "
                "ON CONFLICT (db_key, k) DO UPDATE SET v = excluded.v",
                (db_key, f"{owner}|{now + ttl}"),
            )
        return True

    def release_lease(self, db_key: str, owner: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM catalog_meta WHERE db_key = ? AND k = 'sync_lease' AND v LIKE ?",
                (db_key, f"{owner}|%"),
            )

    def touch_profiled(self, db_key: str, table: str, at: float) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE catalog_tables SET profiled_at = ? WHERE db_key = ? AND name = ?", (at, db_key, table)
            )

    def get_meta(self, db_key: str, k: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT v FROM catalog_meta WHERE db_key = ? AND k = ?", (db_key, k)).fetchone()
        return row[0] if row else default

    def meta_prefix(self, db_key: str, prefix: str) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT k, v FROM catalog_meta WHERE db_key = ? AND substr(k, 1, ?) = ?",
                (db_key, len(prefix), prefix),
            ).fetchall()
        return {k[len(prefix):]: v for k, v in rows}

    def delete_meta(self, db_key: str, k: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM catalog_meta WHERE db_key = ? AND k = ?", (db_key, k))

    def set_meta(self, db_key: str, k: str, v: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO catalog_meta (db_key, k, v) VALUES (?, ?, ?) "
                "ON CONFLICT (db_key, k) DO UPDATE SET v = excluded.v",
                (db_key, k, v),
            )

    # ---------- 变更记录 ----------

    def add_event(self, db_key: str, kind: str, detail: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO catalog_events (db_key, at, kind, detail) VALUES (?, ?, ?, ?)",
                (db_key, time.time(), kind, detail),
            )
            # 只保留最近 2000 条
            conn.execute(
                "DELETE FROM catalog_events WHERE db_key = ? AND id <= "
                "(SELECT id FROM catalog_events WHERE db_key = ? ORDER BY id DESC LIMIT 1 OFFSET 2000)",
                (db_key, db_key),
            )

    def events(self, db_key: str, limit: int = 20) -> list[tuple[float, str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT at, kind, detail FROM catalog_events WHERE db_key = ? ORDER BY id DESC LIMIT ?",
                (db_key, int(limit)),
            ).fetchall()
        return [(at, kind, detail) for at, kind, detail in rows]


def _relations_json(relations: list[Relation]) -> str:
    return json.dumps(
        [
            {"column": r.column, "ref_table": r.ref_table, "ref_column": r.ref_column, "source": r.source, "match": r.match}
            for r in relations
        ],
        ensure_ascii=False,
    )
