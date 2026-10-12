"""数据库建档与记忆同步：把"表卡片 + 依赖索引"维护成和业务库一致的状态。

    接入时      读结构 → 推断关联 → 逐表建档（只读统计）→ 补中文说明 → 登记例句 / 口径 / 记忆的依赖
    结构变了    按表级指纹找出新增、删除、变更的表，只重建这几张；
                依赖这些表的条目复核：例句试编译（加 LIMIT 0，不取数），口径和记忆看引用的表列还在不在；
                复核不过的标"待复核"、不再召回，之后结构恢复或试编译通过就自动恢复
    数据变了    指纹不变。取值档案按周期重新采集；查询时查到的真实取值也顺手补进档案；
                条目依赖的具体取值（status = 'paid'）在库里消失时，同样标待复核
    人工        改说明（人工优先，建档和模型不会覆盖）、确认待复核条目

每次提问不碰业务表：只比一次结构指纹，然后读档案。建档和同步在后台线程里跑，不阻塞提问——
还没建完档的表照常用建表语句和样例行回答。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..budget import BudgetExceeded, UsageMeter
from ..llm import LLMError
from .deps import SchemaIndex, missing_refs, sql_deps, text_deps
from .enrich import describe_table
from .profiler import Profiler, Relation, TableProfile, TableStructure, read_structures, short_value
from .store import CatalogStore, Item, StoredTable

logger = logging.getLogger("deepquery.catalog")

_MAX_BLOCK_LINES = 30  # 一张表的档案最多写几行，宽表不至于撑爆上下文
_TICK_SECONDS = 60.0  # 多久检查一次有没有到期要刷新的取值档案
_POLL_SECONDS = 1.0  # 多久看一次档案库的版本号（别的进程改过档案）
_STRUCTURE_CHECK_SECONDS = 600.0  # 常驻服务多久做一次完整的结构检查（补上提问指纹看不到的主外键、注释变化）
_MAX_VALUE_DEPS = 20  # 一条条目最多跟踪几个取值依赖（防止一条记忆塞进上千个取值）
_LEASE_SECONDS = 300.0  # 跨进程同步锁的有效期（每建完一张表续一次）
_FAIL_BACKOFF_BASE = 600.0  # 一张表建档失败后，隔多久再试（之后每次翻倍）
_FAIL_BACKOFF_MAX = 86400.0


def db_identity(db) -> str:
    """库的身份（不含密码）：SQLite 用文件绝对路径，服务器库用 方言://主机:端口/库名。"""
    path = getattr(db, "db_path", None)
    if path is not None:
        ident = f"sqlite:{Path(path).resolve()}"
    else:
        ident = f"{getattr(db, 'dialect', 'db')}://{getattr(db, 'host', '')}:{getattr(db, 'port', '')}/{getattr(db, 'database', '')}"
    return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:16]


@dataclass
class TableChange:
    added_columns: list[str] = field(default_factory=list)
    dropped_columns: list[str] = field(default_factory=list)
    retyped: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)  # 疑似改名（同表同类型一删一增）
    keys_changed: bool = False

    def text(self, table: str) -> str:
        parts = []
        if self.added_columns:
            parts.append(f"新增列 {'、'.join(self.added_columns)}")
        if self.dropped_columns:
            parts.append(f"删除列 {'、'.join(self.dropped_columns)}")
        if self.retyped:
            parts.append(f"改类型 {'、'.join(self.retyped)}")
        if self.renamed:
            parts.append("疑似改名 " + "、".join(f"{a}→{b}" for a, b in self.renamed))
        if self.keys_changed:
            parts.append("主外键变化")
        return f"{table}：" + ("；".join(parts) or "结构变化")


@dataclass
class SyncReport:
    skipped: bool = False  # 别的进程正在同步，这次没做
    first: bool = False  # 这个库第一次建档
    added: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    changed: dict[str, TableChange] = field(default_factory=dict)
    profiled: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    described: int = 0
    describe_failed: int = 0
    seconds: float = 0.0

    @property
    def structural(self) -> bool:
        return bool(self.added or self.dropped or self.changed)

    def lines(self) -> list[str]:
        if self.skipped:
            return ["另一个进程正在同步档案，这次跳过"]
        out = []
        if self.first:
            out.append(f"首次接入：共 {len(self.added)} 张表")
        elif self.added:
            out.append(f"新增表：{'、'.join(self.added)}")
        if self.dropped:
            out.append(f"删除表：{'、'.join(self.dropped)}")
        out += [c.text(t) for t, c in self.changed.items()]
        if self.profiled:
            out.append(f"建档 {len(self.profiled)} 张表：{'、'.join(self.profiled)}")
        if self.described or self.describe_failed:
            out.append(f"模型补写说明 {self.described} 条" + (f"，{self.describe_failed} 张表失败" if self.describe_failed else ""))
        out += [f"待复核：{x}" for x in self.flagged]
        out += [f"已恢复：{x}" for x in self.restored]
        return out or ["没有变化"]


def diff_structure(old: TableStructure, new: TableStructure) -> TableChange:
    old_cols = {c.lower(): (c, t) for c, t in old.columns}
    new_cols = {c.lower(): (c, t) for c, t in new.columns}
    change = TableChange(
        added_columns=[new_cols[k][0] for k in new_cols if k not in old_cols],
        dropped_columns=[old_cols[k][0] for k in old_cols if k not in new_cols],
        retyped=[
            new_cols[k][0] for k in new_cols
            if k in old_cols and old_cols[k][1].strip().lower() != new_cols[k][1].strip().lower()
        ],
    )
    # 指纹分不清"改名"和"删一列加一列"：同类型的一删一增标成疑似改名，依赖仍按删除处理
    added_types = {c: t.strip().lower() for c, t in new.columns if c in change.added_columns}
    for dropped in change.dropped_columns:
        dtype = old.column_type(dropped).strip().lower()
        match = [c for c, t in added_types.items() if t == dtype and c not in {b for _a, b in change.renamed}]
        if len(change.dropped_columns) == len(change.added_columns) and match:
            change.renamed.append((dropped, match[0]))
    change.keys_changed = (
        sorted(p.lower() for p in old.primary_key) != sorted(p.lower() for p in new.primary_key)
        or sorted(map(str, old.foreign_keys)) != sorted(map(str, new.foreign_keys))
    )
    return change


def _norm_columns(columns) -> list[tuple[str, str]]:
    return [(str(c).lower(), str(t or "").strip().lower()) for c, t in columns]


@dataclass(frozen=True)
class CatalogView:
    """某一时刻的档案只读视图（Agent 组装提示词时用）。"""

    version: int
    items_version: int
    tables: dict
    descriptions: dict
    flagged: frozenset  # {(kind, key)}

    def description(self, table: str, column: str = "") -> tuple[str, str]:
        return self.descriptions.get((table, column), ("", ""))

    def table_note(self, table: str) -> str:
        """表目录里那一句说明：人工 > 建表注释 > 模型生成（标"待确认"）。"""
        text, source = self.description(table)
        if source == "human":
            return text
        card: StoredTable | None = self.tables.get(table)
        if card and card.structure.note:
            return card.structure.note
        if source == "llm" and text:
            return f"{text}（待确认）"
        return text

    def annotate(self, docs: dict[str, str], current_columns: dict | None = None) -> dict[str, str]:
        """在建表语句后附上档案。current_columns（表 → [(列, 类型)]）：当前的真实结构——
        档案是按旧结构建的（结构刚变、后台还没重建完）就先不附，免得新建表语句后面挂着已经不存在的列。"""
        out = {}
        for table, doc in docs.items():
            card: StoredTable | None = self.tables.get(table)
            stale = (
                card is not None
                and current_columns is not None
                and table in current_columns
                and _norm_columns(card.structure.columns) != _norm_columns(current_columns[table])
            )
            block = "" if stale else self.render(table)
            out[table] = f"{doc}\n{block}" if block else doc
        return out

    def render(self, table: str) -> str:
        card: StoredTable | None = self.tables.get(table)
        if card is None or card.profile is None:
            return ""
        s, prof = card.structure, card.profile
        rels = {r.column.lower(): r for r in card.relations}
        lines: list[str] = []
        ttext, tsource = self.description(table)
        if ttext and (tsource == "human" or (tsource == "llm" and not s.note)):
            lines.append(f"表说明：{ttext}" + ("（待确认）" if tsource == "llm" else ""))
        if prof.row_count == 0:
            lines.append("空表，没有数据")
        for name in s.column_names:
            parts = []
            ctext, csource = self.description(table, name)
            if ctext and (csource == "human" or (csource == "llm" and name not in s.column_notes)):
                parts.append(ctext + ("（待确认）" if csource == "llm" else ""))
            rel = rels.get(name.lower())
            if rel:
                parts.append(rel.text())
            col = prof.columns.get(name)
            if col is not None and prof.row_count:
                if col.kind == "enum" and col.values:
                    tail = "" if col.complete else " 等（抽样所见，可能不全）"
                    parts.append("取值 " + " / ".join(col.values) + tail)
                elif col.kind == "date":
                    span = f"，范围 {col.date_min} ~ {col.date_max}" if col.date_min else ""
                    parts.append(f"日期，格式 {col.date_format}{span}")
                elif col.kind == "text" and col.examples:
                    parts.append("样例 " + " / ".join(f"'{e}'" for e in col.examples))
                elif col.kind == "empty":
                    parts.append("全部为空")
                if col.kind != "empty" and col.null_ratio > 0.5:
                    parts.append("多数为空")
            if parts:
                lines.append(f"{name}：{'；'.join(parts)}")
        if not lines:
            return ""
        extra = len(lines) - _MAX_BLOCK_LINES
        lines = lines[:_MAX_BLOCK_LINES] + ([f"……另有 {extra} 列的档案未列出"] if extra > 0 else [])
        day = time.strftime("%Y-%m-%d", time.localtime(card.profiled_at or time.time()))
        head = f"-- {table} 档案（{day} 只读统计；标“待确认”的说明由模型生成）："
        return head + "\n" + "\n".join(f"--   {line}" for line in lines)


class Catalog:
    def __init__(
        self,
        store: CatalogStore,
        db,
        *,
        llm=None,
        sample_rows: int = 20000,
        enum_max: int = 30,
        refresh_hours: float = 24.0,
        describe_meter_factory=None,
        observe: bool = True,
        auto_background: bool = True,
    ):
        """observe：查询时查到的真实取值是否补进档案；auto_background：提问时是否自动起后台任务
        （定期结构检查、取值刷新、记忆取值核对）。命令行一问一答的进程和评测关掉它，改为同步执行。"""
        self.store = store
        self.db = db
        self.key = db_identity(db)
        self.dialect = getattr(db, "dialect", "sqlite")
        self.profiler = Profiler(db, sample_rows=sample_rows, enum_max=enum_max)
        self.llm = llm
        self.refresh_seconds = max(0.0, float(refresh_hours)) * 3600
        self.observe_enabled = observe
        self.auto_background = auto_background
        self._meter_factory = describe_meter_factory or (lambda: UsageMeter(max_tokens=200_000))
        self._allowed = None  # () -> set[str]：表级权限，只给能看见的表建档
        self._memory = None
        self._sources: dict[str, list[tuple[str, str, str]]] = {}  # kind -> [(key, text, sql)]
        self._lock = threading.RLock()  # 同一时刻只有一个同步在跑
        self._state = threading.Lock()
        self._worker: threading.Thread | None = None
        self._pending: list[str] = []
        self._view: CatalogView | None = None
        self._last_tick = 0.0
        self._last_poll = time.monotonic()
        self._last_sync_at = 0.0
        self.last_error = ""
        self._owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"  # 跨进程同步锁的持有者标识
        # 版本号存在档案库里：多个进程（多个 worker、命令行改说明）共用一个档案库时互相看得见
        versions = self.store.versions(self.key)
        self.cards_version = versions.get("cards", 0)  # 档案内容（附在表定义后面的部分）
        self.items_version = versions.get("items", 0)  # 依赖索引里条目的状态
        self.structure_version = versions.get("structure", 0)  # 同步发现过结构变化：提问用的建表语句要重读

    @classmethod
    def from_settings(cls, settings, db, llm=None, **kw) -> "Catalog":
        def meter() -> UsageMeter:
            return UsageMeter(
                price_input_per_m=settings.llm_price_input_per_m,
                price_output_per_m=settings.llm_price_output_per_m,
                max_tokens=settings.agent_max_tokens_per_run,
                max_cost=settings.agent_max_cost_per_run,
            )

        return cls(
            CatalogStore(settings.catalog_db_path),
            db,
            llm=llm if settings.catalog_describe else None,
            sample_rows=settings.catalog_sample_rows,
            enum_max=settings.catalog_enum_max,
            refresh_hours=settings.catalog_refresh_hours,
            describe_meter_factory=meter,
            **kw,
        )

    # ---------- 版本 ----------

    def _bump(self, name: str) -> None:
        setattr(self, f"{name}_version", self.store.bump(self.key, name))
        self._view = None

    def _bump_cards(self) -> None:
        self._bump("cards")

    def _bump_items(self) -> None:
        self._bump("items")

    def poll(self, force: bool = False) -> None:
        """看一眼档案库的版本号：别的进程改过档案（同步、人工改说明、确认条目）就让缓存失效。
        节流到最多每秒一次，读的是档案库里的一行，不碰业务库。"""
        now = time.monotonic()
        if not force and now - self._last_poll < _POLL_SECONDS:
            return
        self._last_poll = now
        versions = self.store.versions(self.key)
        cards, items = versions.get("cards", 0), versions.get("items", 0)
        self.structure_version = max(self.structure_version, versions.get("structure", 0))
        if cards != self.cards_version or items != self.items_version:
            self.cards_version, self.items_version = cards, items
            self._view = None

    def _note_error(self, what: str, e: Exception) -> None:
        self.last_error = f"{what}：{type(e).__name__}: {e}"[:300]
        logger.warning("档案%s失败", what, exc_info=True)

    # ---------- 接线 ----------

    def attach(self, *, memory=None, allowed=None, glossary=None, examples=None) -> None:
        """memory：MemoryStore；allowed：返回可见表集合的函数；glossary / examples：TextIndex。"""
        if memory is not None:
            self._memory = memory
        if allowed is not None:
            self._allowed = allowed
        if glossary is not None:
            self._sources["glossary"] = [(e.key, e.body, "") for e in glossary.entries]
        if examples is not None:
            self._sources["example"] = [(e.key, e.body, getattr(e, "sql", "") or "") for e in examples.entries]

    # ---------- 读 ----------

    def view(self) -> CatalogView:
        cards, items = self.cards_version, self.items_version
        v = self._view
        if v is not None and v.version == cards and v.items_version == items:
            return v
        v = CatalogView(
            version=cards,
            items_version=items,
            tables=self.store.tables(self.key),
            descriptions=self.store.descriptions(self.key),
            flagged=frozenset((k, key) for k, key, _u in self.store.flagged(self.key)),
        )
        # 读的过程中别的线程改过档案：这份视图可能新旧混杂，用这一次，但不缓存
        if cards == self.cards_version and items == self.items_version:
            self._view = v
        return v

    def safe_view(self) -> CatalogView | None:
        """提问路径用：档案库出问题（被锁、损坏、磁盘满）时返回 None，退回只用建表语句。"""
        try:
            return self.view()
        except Exception as e:  # noqa: BLE001
            self._note_error("读取", e)
            return None

    def excluded(self, kind: str) -> set[str]:
        """待复核、暂不召回的条目 key。档案库读不了时返回空集合（退回不过滤）。"""
        view = self.safe_view()
        return {key for k, key in view.flagged if k == kind} if view else set()

    @property
    def version_token(self) -> str:
        return f"c{self.cards_version}.i{self.items_version}"

    def shared_flag_token(self) -> str:
        """缓存 key 用：待复核的口径和例句（所有人共用的那部分）的指纹。
        用户记忆的状态各归各的，由服务端按用户拼进缓存 key——某个访客存删记忆不会让所有人的缓存失效。"""
        view = self.safe_view()
        if view is None:
            return "-"
        shared = sorted(f"{k}:{key}" for k, key in view.flagged if k != "memory")
        return hashlib.sha256("\n".join(shared).encode("utf-8")).hexdigest()[:8] if shared else "0"

    # ---------- 同步 ----------

    def sync(self, force: bool = False, wait_lock: float = 0.0) -> SyncReport:
        """阻塞地同步一次：结构变化 → 增量重建 → 依赖复核。force=True 时所有表重新建档。

        多个进程共用档案库时，同一时刻只让一个进程同步（档案库里的租约锁）：拿不到锁就跳过
        （后台）或等一会儿（命令行 wait_lock 秒），免得慢的那个用旧结构覆盖快的那个的结果。"""
        with self._lock:
            deadline = time.monotonic() + max(0.0, wait_lock)
            while not self.store.acquire_lease(self.key, self._owner, _LEASE_SECONDS):
                if time.monotonic() >= deadline:
                    self.poll(force=True)
                    return SyncReport(skipped=True)
                time.sleep(0.5)
            try:
                return self._sync(force)
            finally:
                self.store.release_lease(self.key, self._owner)

    def _sync(self, force: bool) -> SyncReport:
        started = time.monotonic()
        report = SyncReport()
        allowed = self._allowed() if self._allowed else None
        current = read_structures(self.db, set(allowed) if allowed is not None else None)
        stored = self.store.tables(self.key)
        baseline = {t: st.structure for t, st in stored.items()} or current
        # 先按"上一次见到的结构"登记新条目：条目是照着旧结构写的，依赖要按旧结构认
        items_changed = self._register_sources(baseline, report)

        failures = {t: json.loads(v) for t, v in self.store.meta_prefix(self.key, "fail:").items()}
        report.first = not stored and not failures
        added = sorted(t for t in current if t not in stored)
        report.added = [t for t in added if t not in failures]  # 一直建不了档的表不重复报"新增"
        report.dropped = sorted(t for t in stored if t not in current)
        report.changed = {
            t: diff_structure(stored[t].structure, current[t])
            for t in sorted(current)
            if t in stored and stored[t].fingerprint != current[t].fingerprint()
        }
        unfinished = {t for t in current if t in stored and stored[t].profile is None}
        rebuild = set(added) | set(report.changed) | unfinished
        if force:
            rebuild = set(current)
        now = time.time()
        backoff = {t for t in rebuild if t in failures and failures[t].get("next", 0) > now and not force}
        rebuild -= backoff  # 之前建档失败、还在退避期的表这次不碰

        # 新指纹存下之前，先把"这次变了哪些表"记进档案库：同步中途被打断（重启、Ctrl-C），
        # 下次同步看到的指纹已经一致，但这些表仍然会被复核，不会就此漏掉
        touched = set(report.dropped) | set(report.changed) | set(added)
        pending = set(json.loads(self.store.get_meta(self.key, "pending_review", "[]") or "[]"))
        if touched - pending:
            self.store.set_meta(self.key, "pending_review", json.dumps(sorted(touched | pending), ensure_ascii=False))
        touched |= pending

        for t in report.dropped:
            self.store.delete_table(self.key, t)
            self.store.add_event(self.key, "table_dropped", f"删除表 {t}")
        for t in set(failures) - set(current):
            self.store.delete_meta(self.key, f"fail:{t}")
        if report.first and report.added:
            self.store.add_event(self.key, "first_sync", f"首次建档：{len(report.added)} 张表")
        else:
            for t in report.added:
                self.store.add_event(self.key, "table_added", f"新增表 {t}（{len(current[t].columns)} 列）")
        for t, change in report.changed.items():
            self.store.add_event(self.key, "table_changed", change.text(t))
            for col in change.dropped_columns:
                self.store.delete_description(self.key, t, col, keep_human=True)

        relations = self._relations(current, stored, rebuild, set(report.dropped))
        for t in sorted(rebuild):
            keys = {r.column for r in relations.get(t, [])}
            try:
                profile = self.profiler.profile_table(current[t], keys)
            except Exception as e:  # noqa: BLE001 —— 一张表建档失败不拖累其他表；没存档，退避后重试
                n = int(failures.get(t, {}).get("n", 0)) + 1
                wait = min(_FAIL_BACKOFF_MAX, _FAIL_BACKOFF_BASE * 2 ** (n - 1))
                self.store.set_meta(self.key, f"fail:{t}", json.dumps({"n": n, "next": time.time() + wait}))
                if n == 1:  # 只在第一次失败时记事件，免得重试把变更记录刷掉
                    self.store.add_event(self.key, "error", f"{t} 建档失败：{type(e).__name__}: {e}"[:300])
                logger.warning("建档失败（第 %d 次）：%s", n, t, exc_info=True)
                continue
            # 租约过期（单张表建档超过 5 分钟）时别的进程可能已经按更新的结构存过档：不拿旧结果覆盖它
            if self.store.fingerprint(self.key, t) != (stored[t].fingerprint if t in stored else None):
                self.store.add_event(self.key, "skipped", f"{t} 的档案已被另一个进程更新，这次的结果不保存")
                continue
            self.store.save_table(self.key, current[t], profile, relations.get(t, []))
            if t in failures:
                self.store.delete_meta(self.key, f"fail:{t}")
            report.profiled.append(t)
            self.store.acquire_lease(self.key, self._owner, _LEASE_SECONDS)  # 大库建档时间长：每张表续一次租约
        for t in current:
            if t in rebuild or t not in stored:
                continue
            if _rel_key(stored[t].relations) != _rel_key(relations.get(t, [])):
                self.store.set_relations(self.key, t, relations.get(t, []))

        notes_changed = self._sync_ddl_notes(current)
        if self.llm is not None and report.profiled:
            self._describe(report.profiled, current, report)  # 只给这次建成档的表补说明，每张表一次

        items_changed |= self._resolve_candidates(current)
        self._review(current, touched, report)
        value_tables = set(report.profiled) | (touched & set(current))
        if value_tables:
            self._check_values(current, value_tables, report)
        self.store.set_meta(self.key, "pending_review", "[]")  # 复核做完了

        if report.profiled or report.dropped or report.changed or report.described or notes_changed or force:
            self._bump("cards")
        if report.structural:
            self._bump("structure")  # 提问用的建表语句也要重读（MySQL/PG 只改主外键、注释时提问指纹看不到）
        if items_changed or report.flagged or report.restored:
            self._bump("items")
        self.poll(force=True)  # 别的进程（另一个 worker、命令行）可能已经同步过：对齐版本号
        self._last_sync_at = time.monotonic()
        report.seconds = round(time.monotonic() - started, 3)
        if report.structural or report.flagged or report.restored:
            logger.info("档案同步：%s", "；".join(report.lines()))
        return report

    def _relations(
        self,
        current: dict[str, TableStructure],
        stored: dict[str, StoredTable],
        rebuild: set[str],
        dropped: set[str],
    ) -> dict[str, list[Relation]]:
        """重建的表全部重算；没变的表保留原有关联，只补上"指向重建表"的那部分。
        原来指向的表被删了或重建了的（孤儿关联），这张表整体重算——比如主表 schools 删了，
        satscores.cds 还能改指向仍然对得上的 frpm。"""
        orphaned = {
            t for t in current
            if t in stored and t not in rebuild
            and any(r.ref_table in dropped or r.ref_table in rebuild for r in stored[t].relations)
        }
        targets = rebuild | dropped | orphaned
        fresh = self.profiler.infer_relations(current, targets=targets) if targets else {}
        out: dict[str, list[Relation]] = {}
        for t in current:
            if t in rebuild or t not in stored or t in orphaned:
                out[t] = fresh.get(t, [])
                continue
            kept = [
                r for r in stored[t].relations
                if r.ref_table in current and r.ref_table not in rebuild
                and current[r.ref_table].has_column(r.ref_column) and current[t].has_column(r.column)
            ]
            added = [r for r in fresh.get(t, []) if r.ref_table in rebuild]
            out[t] = kept + [r for r in added if r.column.lower() not in {k.column.lower() for k in kept}]
        return out

    def _sync_ddl_notes(self, current: dict[str, TableStructure]) -> bool:
        """建表注释同步进说明表（来源 ddl）：注释没了就删掉对应的 ddl 说明，人工说明不受影响。
        只解析已经读到的建表语句，不查业务数据。返回是否有变化。"""
        existing = self.store.descriptions(self.key)
        changed = False
        for t, s in current.items():
            want = {("", s.note)} if s.note else set()
            want |= {(c, n) for c, n in s.column_notes.items()}
            for col, note in want:
                text, source = existing.get((t, col), ("", ""))
                if source in ("", "llm", "ddl") and text != note:
                    changed |= self.store.set_description(self.key, t, col, note, "ddl")
            have_ddl = {c for (tt, c), (_x, src) in existing.items() if tt == t and src == "ddl"}
            for col in have_ddl - {c for c, _n in want}:
                self.store.delete_description(self.key, t, col)
                changed = True
        return changed

    def _describe(self, tables: list[str], current: dict[str, TableStructure], report: SyncReport) -> None:
        existing = self.store.descriptions(self.key)
        cards = self.store.tables(self.key)
        for t in tables:
            s = current[t]
            need_table = not s.note and (t, "") not in existing
            # 主键、外键和推断出的关联列不用模型解释：档案里的"关联 xx.id"已经说清楚了
            keys = {c.lower() for c in s.primary_key} | {c.lower() for c, _t, _r in s.foreign_keys}
            if t in cards:
                keys |= {r.column.lower() for r in cards[t].relations}
            columns = [
                c for c in s.column_names
                if c.lower() not in keys and c not in s.column_notes and (t, c) not in existing
            ]
            if not need_table and not columns:
                continue
            try:
                table_desc, col_desc = describe_table(
                    self.llm, s, cards[t].profile if t in cards else None, columns, need_table, self._meter_factory()
                )
            except (LLMError, BudgetExceeded) as e:
                report.describe_failed += 1
                self.store.add_event(self.key, "describe_failed", f"{t} 的说明没生成：{e}"[:200])
                continue
            if table_desc and need_table:
                report.described += int(self.store.set_description(self.key, t, "", table_desc, "llm"))
            for col, desc in col_desc.items():
                report.described += int(self.store.set_description(self.key, t, col, desc, "llm"))

    # ---------- 依赖索引 ----------

    def _register_sources(self, structures: dict[str, TableStructure], report: SyncReport | None = None) -> bool:
        changed = False
        for kind, entries in self._sources.items():
            changed |= self._register(kind, [(k, text, sql, "") for k, text, sql in entries], structures, report=report)[0]
        if self._memory is not None and hasattr(self._memory, "all_notes"):
            notes = [(str(i), note, "", user) for i, user, note in self._memory.all_notes()]
            changed |= self._register("memory", notes, structures, report=report)[0]
        return changed

    def register_memories(self, user_id: str, notes) -> None:
        """提问时登记这个用户的记忆（新增 / 改过的才重新算依赖）。notes：MemoryStore.notes() 的结果。

        这一步在提问路径上，不查业务库：引用的表和列按档案里的结构判断，
        引用的具体取值先记为"待核对"，交给后台（或下一次同步）去查库里有没有。"""
        try:
            view = self.view()
            structures = {t: st.structure for t, st in view.tables.items()}
            if not structures:
                return
            entries = [(str(i), note, "", user_id) for i, note, _ts in notes]
            changed, pending = self._register("memory", entries, structures, user_id=user_id, resolve=False)
            if changed:
                self._bump_items()
            if pending and self.auto_background:
                self._kick("resolve")
        except Exception as e:  # noqa: BLE001 —— 登记失败不影响这次提问
            self._note_error("登记记忆", e)

    def _register(
        self,
        kind: str,
        entries,
        structures,
        user_id: str | None = None,
        report: SyncReport | None = None,
        resolve: bool = True,
    ) -> tuple[bool, bool]:
        """登记（或更新）条目及其依赖；源里已经没有的条目从索引里删掉。
        返回 (待复核集合可能变了, 有没有留下待核对的取值)。resolve=False 时不查业务库。"""
        existing = {i.key: i for i in self.store.items(self.key, kind, user_id)}
        seen, changed, pending = set(), False, False
        batch: list[Item] = []
        for key, text, sql, uid in entries:
            seen.add(key)
            old = existing.get(key)
            if old is not None and old.text == text and old.sql == sql:
                continue
            deps = sql_deps(sql, structures, self.dialect) if sql else text_deps(text, structures)
            refs = deps.to_lists()[0]
            cand = [[t, c, v] for t, c, v in sorted(deps.values)][:_MAX_VALUE_DEPS]
            item = Item(kind=kind, key=key, text=text, sql=sql, user_id=uid, refs=refs)
            # 取值依赖只记"登记时库里确实有"的取值：本来就没有的取值谈不上"消失"
            if resolve:
                item.vals = self._existing_values(cand, structures)
            else:
                item.cand = cand
                pending |= bool(cand)
            missing = missing_refs(refs, structures)
            if missing:
                item.status, item.reason_kind = "review", "schema"
                item.reason = f"引用的 {'、'.join(missing)} 在库里不存在"
                changed = True
                self.store.add_event(self.key, "item_review", f"{_label(item)}：{item.reason}")
                if report is not None:
                    report.flagged.append(f"{_label(item)}：{item.reason}")
            elif old is not None and old.status == "review":
                changed = True  # 改过内容的条目重新登记为有效
            batch.append(item)
        self.store.upsert_items(self.key, batch)
        stale = [k for k in existing if k not in seen]
        self.store.delete_items(self.key, kind, stale)
        return changed or bool(stale), pending

    def _existing_values(self, cand, structures) -> list[list[str]]:
        out = []
        for t, c, v in cand:
            s = structures.get(t)
            if s is not None and s.has_column(c) and self.profiler.value_exists(t, c, v) is True:
                out.append([t, c, v])
        return out

    def _resolve_candidates(self, structures: dict[str, TableStructure]) -> bool:
        """把提问时登记、还没核对的取值依赖查一遍（后台 / 同步时做）。"""
        changed = False
        for item in self.store.items(self.key):
            if not item.cand:
                continue
            found = self._existing_values(item.cand, structures)
            vals = sorted({tuple(v) for v in item.vals} | {tuple(v) for v in found})
            self.store.update_item(self.key, item.kind, item.key, vals=[list(v) for v in vals], cand=[])
            changed = True
        return changed

    def _review(self, current: dict[str, TableStructure], touched: set[str], report: SyncReport) -> None:
        """复核依赖：受影响的表涉及的条目、已经待复核的条目，以及（不查库的）所有引用了不存在表列的条目。"""
        allowed = set(current)
        index = SchemaIndex(current)  # 条目可能上万：结构索引只建一次
        for item in self.store.items(self.key):
            item_tables = {r.split(".", 1)[0] for r in item.refs}
            missing = missing_refs(item.refs, index=index)
            label = _label(item)
            if item.sql:
                if not (item_tables & touched) and item.status != "review" and not missing:
                    continue
                ok, err = self.profiler.dry_run(item.sql, allowed)
                if ok is None:
                    continue  # 超时、连接失败这类临时故障：不改状态，下次再看
                if ok:
                    refs = sql_deps(item.sql, current, self.dialect).to_lists()[0]
                    if item.status == "review" and item.reason_kind in ("schema", "sql"):
                        self.store.update_item(self.key, item.kind, item.key, refs=refs, status="active", reason="", reason_kind="")
                        report.restored.append(label)
                        self.store.add_event(self.key, "item_restored", f"{label} 试编译通过，已恢复")
                    elif refs != sorted(item.refs):
                        self.store.update_item(self.key, item.kind, item.key, refs=refs)
                elif item.status == "active" or item.reason_kind != "sql":
                    reason = f"按当前表结构试编译失败（{err}）"
                    self.store.set_status(self.key, item.kind, item.key, "review", reason, "sql")
                    report.flagged.append(f"{label}：{reason}")
                    self.store.add_event(self.key, "item_review", f"{label}：{reason}")
                continue
            if missing and (item.status == "active" or item.reason_kind != "schema"):
                # 原来因为取值待复核的，现在连表列都没了：原因升级成 schema，免得之后被"取值又出现"误恢复
                reason = f"引用的 {'、'.join(missing)} 已不存在"
                self.store.set_status(self.key, item.kind, item.key, "review", reason, "schema")
                report.flagged.append(f"{label}：{reason}")
                self.store.add_event(self.key, "item_review", f"{label}：{reason}")
            elif not missing and item.status == "review" and item.reason_kind == "schema":
                self.store.set_status(self.key, item.kind, item.key, "active")
                report.restored.append(label)
                self.store.add_event(self.key, "item_restored", f"{label} 引用的表和列都已恢复")

    def _check_values(
        self, current: dict[str, TableStructure], tables: set[str], report: SyncReport | None = None
    ) -> bool:
        """条目依赖的具体取值还在不在（精确查询，后台执行）。返回是否有状态变化。

        有效的条目只查涉及这几张表的取值，有一个没了就标待复核；
        因取值待复核的条目要把它的全部取值都确认还在才恢复，查不了的算"不确定"，不恢复。"""
        changed = False
        index = SchemaIndex(current)
        for item in self.store.items(self.key):
            if not item.vals or item.reason_kind in ("schema", "sql"):
                continue
            if not any(v[0] in tables for v in item.vals) or missing_refs(item.refs, index=index):
                continue  # 表列没了的交给 _review
            gone, unsure = [], False
            for t, c, v in item.vals:
                s = current.get(t)
                if s is None or not s.has_column(c):
                    unsure = True
                    continue
                if item.status == "active" and t not in tables:
                    continue
                exists = self.profiler.value_exists(t, c, v)
                if exists is None:
                    unsure = True
                elif not exists:
                    gone.append(f"'{v}'（{t}.{c}）")
            label = _label(item)
            if gone and item.status == "active":
                reason = f"依赖的取值 {'、'.join(gone)} 在库里已经没有了"
                self.store.set_status(self.key, item.kind, item.key, "review", reason, "value")
                self.store.add_event(self.key, "item_review", f"{label}：{reason}")
                if report is not None:
                    report.flagged.append(f"{label}：{reason}")
                changed = True
            elif not gone and not unsure and item.status == "review" and item.reason_kind == "value":
                self.store.set_status(self.key, item.kind, item.key, "active")
                self.store.add_event(self.key, "item_restored", f"{label} 依赖的取值重新出现，已恢复")
                if report is not None:
                    report.restored.append(label)
                changed = True
        return changed

    # ---------- 数据变化：取值档案刷新 ----------

    def due_tables(self) -> list[str]:
        if not self.refresh_seconds:
            return []
        now = time.time()
        view = self.view()
        return [
            t for t, st in view.tables.items()
            if st.profile is not None and (st.profiled_at or 0) + self.refresh_seconds < now
        ]

    def refresh_values(self, tables: list[str] | None = None) -> list[str]:
        """重新采集取值档案（结构不变）。返回发现变化的说明。"""
        with self._lock:
            stored = self.store.tables(self.key)
            targets = [t for t in (tables if tables is not None else self.due_tables()) if t in stored]
            if targets:
                # 结构在两次同步之间变过：按旧结构统计会查不存在的列，改走一次完整同步
                now = read_structures(self.db, set(targets))
                if any(t not in now or now[t].fingerprint() != stored[t].fingerprint for t in targets):
                    report = self.sync()
                    return report.lines() if report.structural else []
            notes: list[str] = []
            refreshed = []
            for t in targets:
                st = stored[t]
                keys = {r.column for r in st.relations}
                try:
                    profile = self.profiler.profile_table(st.structure, keys)
                except Exception as e:  # noqa: BLE001 —— 这次读不了就保留旧档案，一小时后再试（不每分钟重试刷屏）
                    self.store.add_event(self.key, "error", f"{t} 取值刷新失败：{type(e).__name__}: {e}"[:300])
                    self.store.touch_profiled(self.key, t, time.time() - self.refresh_seconds + 3600)
                    continue
                notes += _value_changes(t, st.profile, profile)
                self.store.update_profile(self.key, t, profile)
                refreshed.append(t)
            for note in notes:
                self.store.add_event(self.key, "values_changed", note)
            current = {t: st.structure for t, st in stored.items()}
            if refreshed and self._check_values(current, set(refreshed)):
                self._bump_items()
            if refreshed:
                self._bump_cards()
            return notes

    def observe_values(self, table: str, column: str, rows: list[tuple], limit: int) -> None:
        """查询时查到了一列最常见的取值（disclosure.probe_values）：新出现的取值补进档案。

        只做"补"：取值是否消失要用精确查询判断（截断的长字符串、Decimal、不区分大小写的排序规则
        都会让"这里没看到"不等于"库里没有"），交给后台的取值刷新。
        rows 少于 limit 说明这就是全部取值。同步正在跑时直接跳过——它会产出更新的档案。"""
        if not self.observe_enabled or not self._lock.acquire(blocking=False):
            return
        try:
            card = self.store.tables(self.key).get(table)
            if card is None or card.profile is None:
                return
            col = card.profile.columns.get(column)
            values = [short_value(v) for v, *_ in rows if v is not None]
            if col is None or col.kind != "enum" or not values:
                return
            new = [v for v in values if v not in col.values]
            if not new:
                return
            complete = len(rows) < limit
            col.values = values if complete else (col.values + new)[: self.profiler.enum_max]
            col.complete = complete  # 取值探针是全表 GROUP BY，不受抽样影响
            self.store.update_profile(self.key, table, card.profile, touch=False)
            self.store.add_event(self.key, "values_changed", f"{table}.{column} 出现新取值 {'、'.join(new)}")
            self._bump_cards()
        except Exception as e:  # noqa: BLE001 —— 补档案失败不影响这次查询
            self._note_error("补取值", e)
        finally:
            self._lock.release()

    # ---------- 后台 ----------

    def sync_in_background(self) -> None:
        self._kick("sync")

    def tick(self) -> None:
        """每次提问前调用：看别的进程有没有改过档案；到期的取值刷新、定期的结构检查交给后台。
        自身节流，平时只是一次时间比较；任何异常都不影响提问。"""
        try:
            self.poll()
            if not self.auto_background:
                return
            now = time.monotonic()
            if now - self._last_tick < _TICK_SECONDS:
                return
            self._last_tick = now
            # MySQL / PG 的提问指纹只看列名和类型：主外键、注释的变化靠这里定期发现
            if now - self._last_sync_at > _STRUCTURE_CHECK_SECONDS:
                self._kick("sync")
            if self.due_tables():
                self._kick("values")
        except Exception as e:  # noqa: BLE001
            self._note_error("检查", e)

    def _kick(self, job: str) -> None:
        with self._state:
            if job not in self._pending:
                self._pending.append(job)
            if self._worker is not None:
                return
            self._worker = threading.Thread(target=self._drain, name="deepquery-catalog", daemon=True)
            self._worker.start()

    def _drain(self) -> None:
        me = threading.current_thread()
        try:
            while True:
                with self._state:
                    if not self._pending:
                        self._worker = None
                        return
                    job = self._pending.pop(0)
                try:
                    if job == "sync":
                        self.sync()
                    elif job == "values":
                        self.refresh_values()
                    elif job == "resolve":
                        with self._lock:
                            structures = {t: st.structure for t, st in self.store.tables(self.key).items()}
                            self._resolve_candidates(structures)
                    self.last_error = ""
                except Exception as e:  # noqa: BLE001 —— 建档失败不能影响提问，记下来等下次
                    self._note_error(f"后台任务 {job}", e)
                    try:
                        self.store.add_event(self.key, "error", f"{job} 失败：{type(e).__name__}: {e}"[:300])
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            with self._state:
                if self._worker is me:  # 异常退出（BaseException）时也要复位，否则后台任务再也起不来
                    self._worker = None

    def wait(self, timeout: float = 60.0) -> None:
        """等后台任务跑完（命令行和测试用）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._state:
                worker = self._worker
            if worker is None:
                return
            worker.join(max(0.0, deadline - time.monotonic()))

    # ---------- 人工 ----------

    def set_description(self, table: str, column: str, text: str) -> None:
        with self._lock:
            self.store.set_description(self.key, table, column or "", text, "human")
            self.store.add_event(
                self.key, "described", f"人工{'清除' if not text.strip() else '修改'}了 {table}{'.' + column if column else ''} 的说明"
            )
            self._bump_cards()

    def confirm(self, item_id: int) -> Item | None:
        """人工确认待复核条目仍然有效：恢复召回，并按当前结构重算依赖。"""
        with self._lock:
            item = self.store.item(self.key, item_id)
            if item is None:
                return None
            structures = {t: st.structure for t, st in self.store.tables(self.key).items()}
            deps = sql_deps(item.sql, structures, self.dialect) if item.sql else text_deps(item.text, structures)
            refs = deps.to_lists()[0]
            # 人工确认过：现在就不存在的引用不再算依赖，否则下次这张表一变又会被标出来
            gone = set(missing_refs(refs, structures))
            item.refs = [r for r in refs if r not in gone]
            item.vals = self._existing_values([[t, c, v] for t, c, v in sorted(deps.values)], structures)
            item.cand = []
            item.status, item.reason, item.reason_kind = "active", "", ""
            self.store.update_item(
                self.key, item.kind, item.key, refs=item.refs, vals=item.vals, cand=[], status="active", reason="", reason_kind=""
            )
            self.store.add_event(self.key, "item_confirmed", f"人工确认 {_label(item)} 仍然有效")
            self._bump_items()
            return item

    def review_items(self) -> list[Item]:
        return [i for i in self.store.items(self.key) if i.status == "review"]

    def status(self) -> dict:
        view = self.view()
        ready = [t for t, st in view.tables.items() if st.profile is not None]
        items = self.store.items(self.key)
        return {
            "tables": len(view.tables),
            "profiled": len(ready),
            "last_profiled_at": max((st.profiled_at or 0 for st in view.tables.values()), default=0) or None,
            "descriptions": {
                src: sum(1 for _k, (_t, s) in view.descriptions.items() if s == src) for src in ("ddl", "llm", "human")
            },
            "items": {
                kind: {
                    "active": sum(1 for i in items if i.kind == kind and i.status == "active"),
                    "review": sum(1 for i in items if i.kind == kind and i.status == "review"),
                }
                for kind in ("glossary", "example", "memory")
            },
            "syncing": self._worker is not None,
            "last_error": self.last_error,
            "version": self.version_token,
        }


def _label(item: Item) -> str:
    """事件和报告里怎么称呼一个条目。用户记忆是私人内容：只写编号，不写原文，也不写是谁的。"""
    if item.kind == "memory":
        return f"用户记忆 #{item.key}"
    name = {"example": "例句", "glossary": "口径"}.get(item.kind, item.kind)
    text = item.key if len(item.key) <= 30 else item.key[:29] + "…"
    return f"{name}「{text}」"


def _rel_key(rels: list[Relation]) -> list[tuple]:
    return sorted((r.column, r.ref_table, r.ref_column, r.source) for r in rels)


def _value_changes(table: str, old: TableProfile | None, new: TableProfile) -> list[str]:
    if old is None:
        return []
    out = []
    for name, col in new.columns.items():
        before = old.columns.get(name)
        if before is None or col.kind != "enum" or before.kind != "enum":
            continue
        added = [v for v in col.values if v not in before.values]
        gone = [v for v in before.values if v not in col.values] if col.complete and before.complete else []
        if added:
            out.append(f"{table}.{name} 出现新取值 {'、'.join(added)}")
        if gone:
            out.append(f"{table}.{name} 不再出现取值 {'、'.join(gone)}")
    return out
