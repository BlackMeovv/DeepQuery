"""Agent 编排：LangGraph 外层状态机 + 手写的修复内循环。

外层（LangGraph 负责确定性流转）：
    generate_sql → execute →（成功）→ answer
                          →（失败且还有额度）→ repair → execute ...
                          →（轮次/预算耗尽）→ fallback
    generate_sql →（交互模式：需要确认）→ clarify
                 →（交互模式：问口径/表结构，不用查数据）→ explain
                 →（交互模式：闲聊）→ reply
    整句寒暄（你好 / 你是谁 / 谢谢）不调用模型，直接进 reply

渐进式披露（schema 装不下时）：先走 browse_schema——模型看表目录选表、申请查看列取值，
系统展开选中表的完整定义后再进 generate_sql；修复前自动补展开 SQL 里用到但还没展开的表。

内层（repair 节点内部是手写的 Reason-Act-Observe 循环）：
    观察全部历史尝试与结构化错误 → 生成修正 SQL
    → 重复 SQL 检测：与历史重复时注入"换思路"提示再试一次
    → 特例：上一轮是 empty_result 且模型原样重发，视为"确认数据为空"，接受为最终结果

可靠性设计都放在编排层（重试上限、预算熔断、终止条件），不依赖模型自觉。
"""

from __future__ import annotations

import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph

from .. import disclosure, scope, smalltalk
from ..budget import BudgetExceeded, RunHandle, UsageMeter
from ..config import Settings
from ..datasets import for_db, knowledge_paths
from ..guard import tables_in_sql, validate
from ..llm import BaseLLM, LLMError
from ..retrieval import SchemaRetriever, build_embedder, load_examples, load_glossary
from ..sandbox import build_sandbox
from ..tools.contract import QueryResult
from ..tracing import NOOP_TRACER, RunTrace, Tracer
from ..verify import check_answer, checked_number_count
from . import prompts


@dataclass
class Attempt:
    sql_raw: str  # 模型输出的 SQL（守卫改写前）
    sql_final: str | None  # 守卫放行并改写后的 SQL；被拒时为 None
    ok: bool
    error_kind: str | None = None
    error_message: str | None = None
    result: QueryResult | None = None

    def describe(self, idx: int) -> str:
        status = "成功" if self.ok else f"[{self.error_kind}] {self.error_message}"
        return f"尝试 {idx}:\n```sql\n{self.sql_raw}\n```\n结果: {status}"


@dataclass
class RunOutcome:
    question: str
    # ok / ok_empty / ok_meta（问口径、表结构，依据 schema 直接回答、没有查数据）
    # / ok_chat（打招呼、问你是谁这类闲聊，直接回应）/ failed / budget_exceeded / needs_clarification
    status: str
    answer: str = ""
    final_sql: str | None = None  # 实际执行的 SQL（守卫改写后，含注入的 LIMIT）
    predicted_sql: str | None = None  # 模型原始 SQL（评测打分用）
    selected_tables: list[str] | None = None  # Schema RAG 选中的表（未启用时为 None）
    context_used: dict | None = None  # 本次注入的上下文明细：glossary/examples/memories
    hallucination_blocked: bool = False  # 回答因数字无出处被拦截降级
    numbers_verified: int = 0  # 回答中通过出处校验的数字个数（校验关闭或未生成回答时为 0）
    chart_path: str | None = None  # 沙箱生成的图表文件（未请求/失败时为 None）
    chart_error: str | None = None
    clarification: dict | None = None  # 需要向用户确认时：{question, term, options}
    sql_summary: list[str] = field(default_factory=list)  # 口径说明：筛选 / 分组 / 排序 / 条数
    result: QueryResult | None = None
    attempts: list[Attempt] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0

    @property
    def succeeded(self) -> bool:
        return self.status in ("ok", "ok_empty", "ok_meta", "ok_chat")


class _State(TypedDict, total=False):
    question: str
    schema_context: str
    candidate_sql: str  # 待执行的 SQL（generate/repair 产出）
    generate_answer: bool  # 是否生成总结回答（评测时关掉省成本）
    generate_chart: bool  # 是否生成图表（沙箱执行）
    chart_path: str | None
    chart_error: str | None
    attempts: list[Attempt]
    accept_empty: bool  # repair 确认空结果为最终答案
    thought: str  # 模型在代码块外写的"一句话思路"（UI 运行面板展示）
    give_up_reason: str
    status: str
    answer: str
    hallucination_blocked: bool
    numbers_verified: int
    meter: Any  # UsageMeter（对象通道，就地累加）
    trace: Any  # RunTrace（追踪句柄，未启用时为 no-op）
    on_answer_delta: Any  # 可选回调：回答生成的流式增量（SSE 逐字输出用）
    allowed_tables: Any  # 本次运行开始时的表白名单快照（运行中 schema 刷新不影响本次）
    allow_clarify: bool  # 交互模式：允许模型先向用户确认（评测时关闭）
    interactive: bool  # 在线问答：结果给人看（名单默认前 10、匿名 ID 带辨认列）；评测时关闭
    clarification: dict  # 模型提出的澄清问题
    conversation: str  # 同一会话里之前几轮的问题 / SQL / 回答（追问时理解指代用；评测为空）
    schema_mode: str  # full（全量直供）/ retrieve（检索选表）/ disclose（渐进式披露）
    schema_snap: Any  # 本次运行使用的 schema 快照
    knowledge_context: str  # 业务口径 / 相似例句 / 用户记忆（渐进式披露时与表结构分开拼）
    lookup: str  # 检索口径、例句用的文本（追问时带上上一问）
    expanded_tables: list[str]  # 渐进式披露：已展开完整定义的表
    value_notes: list[str]  # 已查出的列取值说明（拼在表结构之后）
    probed: list[str]  # 已查过取值的 表.列，避免重复查
    step_detail: str  # 本节点对外展示的补充说明（如"展开了哪些表"），只随本节点的事件发出
    small_talk: str | None  # 整句寒暄的类型（intro / thanks）：不调用模型直接回复
    allow_meta: bool  # 允许不查数据直接回答（口径 / 表结构 / 闲聊）；分析模式的子查询关闭


_CODE_BLOCK = re.compile(r"```([a-zA-Z0-9_-]*)[ \t]*\n?(.*?)```", re.DOTALL)
_SQL_HEAD = re.compile(r"^\s*(select|with)\b", re.IGNORECASE | re.DOTALL)


def extract_sql(text: str) -> str:
    """从模型回复中提取 SQL。

    模型输出形态多样（多个代码块、```sqlite/无语言标签围栏、混入 json 块），
    按优先级取：sql/sqlite 标签的块 → 以 SELECT/WITH 开头的块 → 第一个块 → 整段文本。
    """
    blocks = [(lang.lower(), body.strip()) for lang, body in _CODE_BLOCK.findall(text or "")]
    candidate = next((body for lang, body in blocks if lang in ("sql", "sqlite") and body), None)
    if candidate is None:
        candidate = next((body for _lang, body in blocks if _SQL_HEAD.match(body)), None)
    if candidate is None and blocks:
        candidate = blocks[0][1]
    if candidate is None:
        candidate = text or ""
    return candidate.strip().rstrip(";").strip()


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", (sql or "").strip().rstrip(";")).lower()


def extract_thought(text: str) -> str:
    """提取代码块之外的散文（模型的"一句话思路"），供 UI 的运行过程面板展示。"""
    prose = _CODE_BLOCK.sub(" ", text or "")
    prose = re.sub(r"\s+", " ", prose).strip()
    return prose[:200]


_CLARIFY_OPTION = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s*")
# 模型常照抄"选项一："这类前缀，展示和拼回问题时都去掉
_OPTION_PREFIX = re.compile(r"^(?:选项|方案)\s*[一二三四五六七八九十\d]+\s*[:：、.]\s*")


def extract_clarification(text: str) -> dict | None:
    """解析模型的 ```clarify 代码块：{question, term, options}；没有或无效时返回 None。"""
    for lang, body in _CODE_BLOCK.findall(text or ""):
        if lang.lower() != "clarify":
            continue
        question, term, options = "", "", []
        for raw in body.splitlines():
            line = raw.strip()
            if not line:
                continue
            if _CLARIFY_OPTION.match(line):
                opt = _OPTION_PREFIX.sub("", _CLARIFY_OPTION.sub("", line)).strip()
                if opt and len(options) < 4:
                    options.append(opt[:60])
            elif re.match(r"^问题\s*[:：]", line):
                question = re.sub(r"^问题\s*[:：]\s*", "", line)
            elif re.match(r"^口径词\s*[:：]", line):
                term = re.sub(r"^口径词\s*[:：]\s*", "", line).strip("「」\"“”'")
            elif not question:
                question = line
        if question:
            return {"question": question[:120], "term": term[:30], "options": options}
    return None


# 闲聊的字面信号：模型用 answer 块回应闲聊时，问题里得有这类说法（同样是为了挡住偷懒的数据回答）
_CHAT_CUE = re.compile(
    r"你是谁|你是什么|你叫什么|介绍一下你|介绍你|自我介绍|你能做什么|你能干什么|你可以做什么|你会做什么|"
    r"你会什么|能做什么|怎么用你|如何使用|什么模型|哪个模型|你好|您好|谢谢|天气|笑话",
    re.IGNORECASE,
)

# "问口径 / 表结构"的字面信号。不查数据直接回答，只在问题里出现这类说法时才接受：
# 否则"销售额最高的品类是哪个"这种数据问题，模型偷懒报个品类名（不带数字）也能通过数字校验
_META_CUE = re.compile(
    r"口径|定义|含义|意思|区别|怎么算|如何算|怎么计算|如何计算|计算方[法式]|算法|公式|怎么来|怎么得|"
    r"这么算|怎么查|如何查|查询逻辑|哪些表|哪张表|哪个表|什么表|字段|表结构|数据结构|schema|sql|"
    r"依据|来源|出处|哪来|从哪",
    re.IGNORECASE,
)


def extract_meta_answer(text: str) -> str | None:
    """解析模型的 ```answer 代码块（问口径 / 表结构时不查数据、直接回答）。

    同时给了 SQL 的一律按查数据处理：能查就查，结果比口头解释可靠。
    """
    blocks = [(lang.lower(), body.strip()) for lang, body in _CODE_BLOCK.findall(text or "")]
    if any(lang in ("sql", "sqlite") and body for lang, body in blocks):
        return None
    body = next((body for lang, body in blocks if lang == "answer" and body), None)
    return plain_answer(body) if body else None


def plain_answer(text: str) -> str:
    """回答只保留普通句子：去掉 Markdown 表格行、加粗和标题符号。

    查询结果本来就以表格单独展示；提示词已要求不用 Markdown，这里再兜一次底。
    在数字校验之前做：展示给用户的、被核对的、被计数的是同一段文字。
    """
    all_lines = (text or "").splitlines()
    lines = [line for line in all_lines if not re.match(r"^\s*\|.*\|\s*$", line)]
    out = "\n".join(lines)
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)
    out = re.sub(r"^#{1,6}\s+", "", out, flags=re.M).strip()
    if len(lines) < len(all_lines):  # 去掉了表格："……是：" 这类引子改成指向下方的结果表
        out = re.sub(r"(如下|是|为)?\s*[:：]$", "见下方结果表。", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _expand_detail(tables: list[str], probed: list[str]) -> str:
    text = f"展开 {'、'.join(tables)} 的完整定义" if tables else "没有选出表"
    return text + (f"；查看 {'、'.join(probed)} 的真实取值" if probed else "")


def _repair_detail(new_tables: list[str], probed: list[str]) -> str:
    parts = []
    if probed:
        parts.append(f"查看了 {'、'.join(probed)} 的真实取值")
    if new_tables:
        parts.append(f"补充展开 {'、'.join(new_tables)} 的完整定义")
    return "；".join(parts)


def extract_code(text: str, langs: tuple[str, ...] = ("python", "py")) -> str:
    """提取代码块：优先匹配语言标签，其次第一个非空块，最后整段文本。"""
    blocks = [(lang.lower(), body.strip()) for lang, body in _CODE_BLOCK.findall(text or "")]
    for lang, body in blocks:
        if lang in langs and body:
            return body
    for _lang, body in blocks:
        if body:
            return body
    return (text or "").strip()


# 图表代码静态拒绝清单：真正的隔离靠沙箱，这是廉价的第一道筛
_CHART_CODE_DENY = re.compile(
    r"\b(subprocess|socket|urllib|requests|http\.client|ftplib|ctypes|importlib|"
    r"__import__|eval\s*\(|exec\s*\(|os\.(system|popen|exec\w*|spawn\w*|remove|unlink|rmdir|symlink|link)|"
    r"(symlink_to|hardlink_to)\s*\(|environ|fork\s*\(|multiprocessing|os\.kill|signal\.)|/proc/"
)


@dataclass(frozen=True)
class _SchemaSnapshot:
    """一次 schema 自省的完整结果。

    服务端只有一个 agent 实例被并发请求共享：schema 刷新必须整体替换这个引用，
    而不是逐个改属性——否则并发请求可能读到"新指纹 + 旧表结构"的混合状态，
    并把这个错配的答案按新指纹写进缓存。
    """

    fingerprint: str
    allowed_tables: frozenset
    table_docs: dict
    full_schema: str
    retriever: Any
    columns: dict  # 表 → 列名清单（渐进式披露的目录、查列取值时核对列名）
    catalog: dict  # 表 → 一行目录（表名 + 说明 + 列名）
    labels: dict  # (表, 列) → 建表注释里的中文名与枚举值中文（口径说明用）


def resolve_allowed_tables(settings: Settings, db) -> set[str]:
    """表级权限：ALLOWED_TABLES 与库中实际表求交集（大小写不敏感）；不配置=全部可见。"""
    all_tables = set(db.table_names())
    configured = {t.strip() for t in settings.allowed_tables.split(",") if t.strip()}
    if not configured:
        return all_tables
    wanted = {t.lower() for t in configured}
    visible = {t for t in all_tables if t.lower() in wanted}
    missing = wanted - {t.lower() for t in visible}
    if missing:
        print(f"[deepquery] ALLOWED_TABLES 中不存在的表已忽略: {', '.join(sorted(missing))}", file=sys.stderr)
    return visible


class DeepQuery:
    def __init__(
        self,
        settings: Settings,
        db,  # ReadOnlyDatabase / MySQLDatabase / PostgresDatabase（同鸭子类型接口）
        llm: BaseLLM,
        tracer: Tracer | None = None,
        memory=None,  # MemoryStore | None：跨会话用户口径记忆
    ):
        self.memory = memory
        self.settings = settings
        self.db = db
        self.llm = llm
        self.tracer = tracer or NOOP_TRACER
        self._refresh_lock = threading.Lock()
        self._fp_checked_at = time.monotonic()
        self._snap: _SchemaSnapshot = self._load_schema()
        glossary_path, examples_path = knowledge_paths(settings)  # 内置数据集自带各自的口径
        self._glossary = load_glossary(glossary_path)
        self._examples = load_examples(examples_path)
        self._sandbox = None  # 图表沙箱按需构建
        self._analyst = None  # 分析模式按需构建
        self._graph = self._build_graph()

    @property
    def analyst(self):
        """分析模式（多步查询 + 带出处的结论），首次用到时构建。"""
        if self._analyst is None:
            from .analyst import Analyst

            self._analyst = Analyst(self)
        return self._analyst

    @property
    def allowed_tables(self) -> set[str]:
        """当前实例可见/可查询的表集合（供 server 与 MCP 工具复用同一权限口径）。"""
        return set(self._snap.allowed_tables)

    @property
    def full_schema(self) -> str:
        """权限过滤后的全量 schema 文本。"""
        return self._snap.full_schema

    @property
    def schema_fingerprint(self) -> str:
        """当前已加载 schema 的版本指纹（服务端把它编入缓存 key）。"""
        return self._snap.fingerprint

    def _load_schema(self) -> _SchemaSnapshot:
        """自省数据库，构建一份完整的 schema 快照（不修改实例状态）。

        表级权限：白名单、schema 注入、RAG 语料同源过滤——模型看不见的表既不会
        出现在 prompt 里，也过不了守卫（纵深的应用层；硬边界在 DB 只读账号）。
        """
        fp = getattr(self.db, "schema_fingerprint", None)
        fingerprint = fp() if callable(fp) else ""
        allowed = frozenset(resolve_allowed_tables(self.settings, self.db))
        docs = {t: doc for t, doc in self.db.schema_by_table().items() if t in allowed}
        list_columns = getattr(self.db, "table_columns", None)
        raw = list_columns() if callable(list_columns) else {}
        columns = {t: [c["name"] for c in raw.get(t, [])] for t in docs}
        return _SchemaSnapshot(
            fingerprint=fingerprint,
            allowed_tables=allowed,
            table_docs=docs,
            full_schema="\n\n".join(docs.values()),
            retriever=SchemaRetriever(docs, embedder=build_embedder(self.settings)),
            columns=columns,
            catalog=disclosure.build_catalog(docs, columns),
            labels=scope.column_labels(docs),
        )

    def maybe_refresh_schema(self) -> str:
        """指纹变了才重建（每次提问前调用）。

        检查是一次微秒/毫秒级查询；建表/改表后无需重启服务即可被看见。
        不支持指纹的引擎返回空串，保持「启动时加载一次」的旧行为。
        """
        fp = getattr(self.db, "schema_fingerprint", None)
        if not callable(fp):
            return self._snap.fingerprint
        # SQLite 查指纹是微秒级，每次都查；MySQL/PG 要扫 information_schema，按引擎给的间隔节流
        ttl = getattr(self.db, "fingerprint_ttl", 0)
        if ttl and time.monotonic() - self._fp_checked_at < ttl:
            return self._snap.fingerprint
        with self._refresh_lock:  # 表结构变化时，并发请求只重建一次
            current = fp()
            self._fp_checked_at = time.monotonic()
            if current != self._snap.fingerprint:
                self._snap = self._load_schema()  # 单次引用赋值：读者要么见旧快照，要么见新快照
        return self._snap.fingerprint

    def _schema_mode(self, snap: _SchemaSnapshot) -> str:
        """决定这次怎么把表结构交给模型：full / retrieve / disclose。

        大库不能全量塞 prompt（贵且触发 Lost in the Middle）。auto 按全量体积判断：
        装得下就直供——BIRD 消融中直供不低于检索选表，而检索只省约 3% token、多一处召回失败点；
        装不下走渐进式披露：目录里每张表都看得见，漏选的表在修复时还能补上。
        """
        mode = self.settings.schema_rag
        if mode == "on":
            return "retrieve"
        if mode == "disclose":
            return "disclose"
        full_chars = sum(len(d) for d in snap.table_docs.values())
        too_big = (
            len(snap.table_docs) > self.settings.schema_rag_top_k
            and full_chars > self.settings.schema_rag_auto_max_chars
        )
        return "disclose" if mode == "auto" and too_big else "full"

    def _build_schema_context(
        self, question: str, snap: _SchemaSnapshot, user_id: str = "default"
    ) -> tuple[str, str, list[str] | None, dict, str]:
        """按问题组装上下文（基于调用方传入的同一份快照）。

        返回 (表结构, 业务知识, 检索选中的表, 注入明细, 模式)。业务口径、相似例句、用户记忆
        始终按问题检索后附加；渐进式披露时表结构由 browse_schema 节点再决定展开哪些。
        """
        mode = self._schema_mode(snap)
        selected: list[str] | None = None
        if mode == "retrieve":
            selected = snap.retriever.top_tables(question, self.settings.schema_rag_top_k)
            schema = "\n\n".join(snap.table_docs[t] for t in selected)
        elif mode == "full":
            schema = snap.full_schema
        else:
            schema = ""

        knowledge = ""
        top_n = self.settings.knowledge_top_n
        glossary_hits = self._glossary.top(question, top_n)
        if glossary_hits:
            knowledge += "\n\n业务字典（口径定义）：\n" + "\n".join(e.body for e in glossary_hits)
        example_hits = self._examples.top(question, top_n)
        if example_hits:
            knowledge += "\n\n相似问题参考：\n" + "\n\n".join(e.body for e in example_hits)
        memory_hits: list[str] = []
        if self.memory is not None:
            memory_hits = self.memory.recall(user_id, question, top_n)
            if memory_hits:
                knowledge += "\n\n该用户的口径偏好（跨会话记忆，优先遵循）：\n" + "\n".join(
                    f"- {m}" for m in memory_hits
                )
        # 本次实际注入的上下文明细（UI 的"上下文"面板与可解释性用）
        context_used = {
            "glossary": [e.key for e in glossary_hits],
            "examples": [e.key for e in example_hits],
            "memories": memory_hits,
        }
        return schema, knowledge, selected, context_used, mode

    # ---------- public ----------

    def ask(
        self,
        question: str,
        generate_answer: bool = True,
        generate_chart: bool = False,
        user_id: str = "default",
        allow_clarify: bool = False,
        interactive: bool = False,
        history: list[dict] | None = None,
        meter: UsageMeter | None = None,
        allow_meta: bool = True,
    ) -> RunOutcome:
        """回答一个自然语言问题。generate_answer=False 时跳过总结节点（评测省成本）；
        generate_chart=True 时对成功结果生成图表（模型写代码 → 沙箱执行）；
        allow_clarify=True 时问题有歧义或数据缺失会返回 needs_clarification（交互场景用，
        评测保持关闭，提示词与历史评测一致）。
        history：同一会话之前几轮的 [{question, sql, answer}]（旧的在前），支持"那…呢"这类追问。
        meter：传入时和调用方共用预算与取消开关（分析模式的各个子查询共用一个）。
        allow_meta=False：必须查数据，不走"依据表结构直接回答"和寒暄（分析模式的子查询）。"""
        start = time.monotonic()
        state, meter, trace, selected_tables, context_used = self._prepare_run(
            question, generate_answer, generate_chart, user_id, allow_clarify, interactive, history,
            meter=meter, allow_meta=allow_meta,
        )
        try:
            final: dict = self._graph.invoke(state, config=self._run_config())
        except GraphRecursionError:
            final = {"attempts": [], "status": "failed", "answer": "内部编排步数超限，已终止。"}
        return self._finish_run(
            question, final, meter, trace, selected_tables, context_used, start
        )

    def ask_stream(
        self,
        question: str,
        generate_answer: bool = True,
        generate_chart: bool = False,
        user_id: str = "default",
        on_answer_delta=None,
        allow_clarify: bool = False,
        handle: RunHandle | None = None,
        interactive: bool = False,
        history: list[dict] | None = None,
    ):
        """逐节点流式执行（服务端 SSE 用）。

        依次 yield ("node", 节点名, 增量状态)，最后 yield ("final", RunOutcome, None)。
        on_answer_delta：回答文本的逐字增量回调（在 answer 节点的 LLM 调用中触发）。
        handle：调用方的控制句柄。handle.cancel() 后，下一次 LLM 调用前（或流式输出途中）
        抛出 RunCancelled 结束运行；handle.meter 始终指向本次的用量，供调用方记账。
        """
        start = time.monotonic()
        state, meter, trace, selected_tables, context_used = self._prepare_run(
            question, generate_answer, generate_chart, user_id, allow_clarify, interactive, history
        )
        if handle is not None:
            meter.cancel_event = handle.cancelled
            handle.meter = meter
        if on_answer_delta is not None:
            state["on_answer_delta"] = on_answer_delta
        final_state: dict = dict(state)
        try:
            for update in self._graph.stream(state, config=self._run_config(), stream_mode="updates"):
                for node, delta in update.items():
                    if delta:
                        final_state.update(delta)
                    yield ("node", node, delta or {})
        except GraphRecursionError:
            final_state.update({"status": "failed", "answer": "内部编排步数超限，已终止。"})
        outcome = self._finish_run(
            question, final_state, meter, trace, selected_tables, context_used, start
        )
        yield ("final", outcome, None)

    # ---------- run plumbing ----------

    def _run_config(self) -> dict:
        # 每轮修复消耗 repair+execute 两个 superstep，上限必须跟随配置放大
        return {"recursion_limit": 2 * self.settings.agent_max_repair_rounds + 12}

    def _prepare_run(
        self,
        question: str,
        generate_answer: bool,
        generate_chart: bool,
        user_id: str = "default",
        allow_clarify: bool = False,
        interactive: bool = False,
        history: list[dict] | None = None,
        meter: UsageMeter | None = None,
        allow_meta: bool = True,
    ):
        self.maybe_refresh_schema()  # 建/改表后无需重启即生效（CLI/MCP/服务共用此入口）
        snap = self._snap  # 本次运行全程只用这一份快照
        meter = meter or self.new_meter()
        trace = self.tracer.start_run(question)
        # 追问常常省略主语（"那按月呢"）：检索口径、例句和记忆时带上上一轮的问题
        turns = [t for t in (history or []) if t.get("question")]
        lookup = f"{turns[-1]['question']} {question}" if turns else question
        schema, knowledge, selected_tables, context_used, mode = self._build_schema_context(
            lookup, snap, user_id=user_id
        )
        # context_used 含用户私有记忆原文：只能随本次运行传递，绝不能挂在共享的
        # agent 实例上——并发请求会互相覆盖，把 A 的记忆吐给 B 并写进缓存
        if selected_tables is not None:
            trace.span("schema_rag", metadata={"selected_tables": selected_tables})
        state: _State = {
            "question": question,
            "schema_context": schema + knowledge,
            "schema_mode": mode,
            "schema_snap": snap,
            "knowledge_context": knowledge,
            "lookup": lookup,
            "expanded_tables": [],
            "value_notes": [],
            "probed": [],
            "small_talk": smalltalk.kind(question) if interactive and allow_meta else None,
            "allow_meta": allow_meta,
            "attempts": [],
            "generate_answer": generate_answer,
            "generate_chart": generate_chart,
            "meter": meter,
            "trace": trace,
            "allowed_tables": snap.allowed_tables,
            "allow_clarify": allow_clarify,
            "interactive": interactive,
            "conversation": prompts.format_history(turns),
        }
        return state, meter, trace, selected_tables, context_used

    def new_meter(self, factor: float = 1.0) -> UsageMeter:
        """一次运行的计量器；factor 放大预算上限（分析模式要跑好几条查询）。"""
        return UsageMeter(
            price_input_per_m=self.settings.llm_price_input_per_m,
            price_output_per_m=self.settings.llm_price_output_per_m,
            max_tokens=int(self.settings.agent_max_tokens_per_run * factor),
            max_cost=self.settings.agent_max_cost_per_run * factor,
        )

    def _finish_run(
        self,
        question: str,
        final: dict,
        meter: UsageMeter,
        trace: RunTrace,
        selected_tables: list[str] | None,
        context_used: dict | None,
        start: float,
    ) -> RunOutcome:
        attempts: list[Attempt] = final.get("attempts", [])
        last_ok = next((a for a in reversed(attempts) if a.ok), None)
        executed_sql, raw_sql = self._pick_final(final, attempts, last_ok)
        if final.get("schema_mode") == "disclose":
            selected_tables = list(final.get("expanded_tables") or [])  # 渐进式披露：实际展开的表
        snap = final.get("schema_snap") or self._snap
        # 口径说明用模型原始 SQL：守卫注入的 LIMIT 200 不是用户要的"取前 200 条"
        summary = scope.describe(raw_sql, self.db.dialect, snap.labels) if raw_sql else []
        outcome = RunOutcome(
            question=question,
            status=final.get("status", "failed"),
            answer=final.get("answer", ""),
            final_sql=executed_sql,
            predicted_sql=raw_sql,
            selected_tables=selected_tables,
            context_used=context_used,
            hallucination_blocked=final.get("hallucination_blocked", False),
            numbers_verified=final.get("numbers_verified", 0),
            chart_path=final.get("chart_path"),
            chart_error=final.get("chart_error"),
            clarification=final.get("clarification"),
            sql_summary=summary,
            result=last_ok.result if last_ok else None,
            attempts=attempts,
            usage=meter.snapshot(),
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        trace.end(status=outcome.status, output=outcome.answer, usage=outcome.usage)
        return outcome

    @staticmethod
    def _pick_final(
        final: _State, attempts: list[Attempt], last_ok: Attempt | None
    ) -> tuple[str | None, str | None]:
        """返回 (实际执行的守卫改写版 SQL, 模型原始 SQL)。评测打分用后者——
        守卫注入的 LIMIT 是生产安全措施，不应影响 EX 判定。"""
        if last_ok:
            return last_ok.sql_final, last_ok.sql_raw
        if final.get("accept_empty"):
            # 空结果被确认为最终答案：取最后一次守卫放行的 SQL
            for a in reversed(attempts):
                if a.sql_final:
                    return a.sql_final, a.sql_raw
        return None, None

    # ---------- graph ----------

    def _build_graph(self):
        g = StateGraph(_State)
        g.add_node("browse_schema", self._node_browse_schema)
        g.add_node("generate_sql", self._node_generate_sql)
        g.add_node("execute", self._node_execute)
        g.add_node("repair", self._node_repair)
        g.add_node("chart", self._node_chart)
        g.add_node("summarize", self._node_answer)
        g.add_node("fallback", self._node_fallback)
        g.add_node("clarify", self._node_clarify)
        g.add_node("explain", self._node_explain)
        g.add_node("reply", self._node_reply)

        g.add_conditional_edges(
            START,
            lambda s: (
                "reply" if s.get("small_talk")
                else "browse_schema" if s.get("schema_mode") == "disclose" else "generate_sql"
            ),
            {"reply": "reply", "browse_schema": "browse_schema", "generate_sql": "generate_sql"},
        )
        g.add_conditional_edges(
            "browse_schema",
            lambda s: "fallback" if s.get("status") in ("budget_exceeded", "failed") else "generate_sql",
            {"generate_sql": "generate_sql", "fallback": "fallback"},
        )
        g.add_conditional_edges(
            "generate_sql",
            self._route_after_generate,
            {
                "execute": "execute", "fallback": "fallback", "clarify": "clarify",
                "explain": "explain", "reply": "reply",
            },
        )
        g.add_conditional_edges(
            "execute",
            self._route_after_execute,
            {"answer": "summarize", "chart": "chart", "repair": "repair", "fallback": "fallback"},
        )
        g.add_edge("chart", "summarize")
        g.add_conditional_edges(
            "repair",
            self._route_after_repair,
            {"execute": "execute", "answer": "summarize", "fallback": "fallback"},
        )
        g.add_edge("summarize", END)
        g.add_edge("fallback", END)
        g.add_edge("clarify", END)
        g.add_edge("explain", END)
        g.add_edge("reply", END)
        return g.compile()

    # ---------- nodes ----------

    def _trace(self, state: _State) -> RunTrace:
        return state.get("trace") or RunTrace()

    def _record_generation(self, state: _State, tag: str, messages: list[dict], reply) -> None:
        self._trace(state).generation(
            tag,
            messages,
            reply.text,
            self.llm.model_name,
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
        )

    def _sql_system(self, state: _State) -> str:
        system = prompts.sql_system(self.db.dialect)
        if state.get("interactive"):
            system += prompts.INTERACTIVE_RULES
        return system

    def _context(self, state: _State) -> str:
        """写 SQL 用的上下文：表结构 + 业务知识 +（查过的话）列的真实取值。"""
        if state.get("schema_mode") == "disclose":
            snap: _SchemaSnapshot = state["schema_snap"]
            text = disclosure.compose(snap.table_docs, snap.catalog, state.get("expanded_tables") or [])
            text += state.get("knowledge_context", "")
        else:
            text = state["schema_context"]
        notes = state.get("value_notes")
        if notes:
            text += "\n\n列的真实取值（按出现次数从多到少，核对过滤条件的写法用）：\n" + "\n".join(notes)
        return text

    def _sql_user(self, state: _State) -> str:
        """写 SQL 的用户消息：schema 上下文 +（有的话）对话上下文 + 本轮问题。"""
        if state.get("conversation"):
            return prompts.SQL_USER_WITH_HISTORY_TEMPLATE.format(
                schema=self._context(state),
                history=state["conversation"],
                question=state["question"],
            )
        return prompts.SQL_USER_TEMPLATE.format(schema=self._context(state), question=state["question"])

    def _probe(self, state: _State, pairs: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
        """查列的真实取值（同一列一次运行只查一次）。返回 (取值说明, 查过的 表.列)。"""
        done = set(state.get("probed") or [])
        notes, keys = [], []
        for table, column in pairs:
            key = f"{table}.{column}"
            if key in done:
                continue
            done.add(key)
            keys.append(key)
            note = disclosure.probe_values(self.db, table, column)
            if note:
                notes.append(note)
        if keys:
            self._trace(state).span("value_probe", metadata={"columns": keys})
        return notes, keys

    def _node_browse_schema(self, state: _State) -> _State:
        """渐进式披露第一步：模型只看表目录，选出要展开的表、要查取值的列。"""
        snap: _SchemaSnapshot = state["schema_snap"]
        history = state.get("conversation")
        messages = [
            {"role": "system", "content": prompts.BROWSE_SYSTEM},
            {
                "role": "user",
                "content": prompts.BROWSE_USER_TEMPLATE.format(
                    catalog="\n".join(snap.catalog.values()),
                    knowledge=state.get("knowledge_context", ""),
                    history=f"之前的对话（最近的在最后）：\n{history}\n\n" if history else "",
                    question=state["question"],
                ),
            },
        ]
        top_k = self.settings.schema_rag_top_k
        lookup = state.get("lookup") or state["question"]
        try:
            reply = self.llm.chat(messages, state["meter"], tag="browse_schema")
        except BudgetExceeded:
            return {"status": "budget_exceeded", "give_up_reason": "预算超限"}
        except LLMError:
            # 选表失败不致命：退回检索选表，照常往下走
            tables = snap.retriever.top_tables(lookup, top_k)
            return {"expanded_tables": tables, "thought": "浏览表目录失败，改用检索选表", "step_detail": _expand_detail(tables, [])}
        self._record_generation(state, "browse_schema", messages, reply)
        tables, probes = disclosure.parse_request(reply.text, set(snap.table_docs), snap.columns)
        if not tables:
            # 没按格式给出表：直接写了 SQL 就用它引用的表，否则退回检索选表
            used = tables_in_sql(extract_sql(reply.text), self.db.dialect)
            tables = [t for t in snap.table_docs if t.lower() in used] or snap.retriever.top_tables(lookup, top_k)
        tables = (tables + [t for t, _c in probes if t not in tables])[: self.settings.schema_disclose_max_tables]
        notes, probed = self._probe(state, [(t, c) for t, c in probes if t in tables])
        self._trace(state).span("browse_schema", metadata={"tables": tables, "probed": probed})
        return {
            "expanded_tables": tables,
            "value_notes": (state.get("value_notes") or []) + notes,
            "probed": (state.get("probed") or []) + probed,
            "thought": extract_thought(reply.text),
            "step_detail": _expand_detail(tables, probed),
        }

    def _node_generate_sql(self, state: _State) -> _State:
        system = self._sql_system(state)
        if state.get("allow_clarify"):
            system += prompts.CLARIFY_RULES
        allow_meta = state.get("interactive") and state.get("allow_meta", True)
        if allow_meta:
            system += prompts.META_RULES
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._sql_user(state)},
        ]
        for round_ in range(2):  # 第二轮只在"口头回答里出现了无出处的数字"时发生
            try:
                reply = self.llm.chat(messages, state["meter"], tag="generate_sql")
            except BudgetExceeded:
                return {"status": "budget_exceeded", "give_up_reason": "预算超限"}
            except LLMError as e:
                return {"status": "failed", "give_up_reason": f"LLM 调用失败: {e}"}
            self._record_generation(state, "generate_sql", messages, reply)
            thought = extract_thought(reply.text)
            if state.get("allow_clarify"):
                clarification = extract_clarification(reply.text)
                if clarification:
                    # 有歧义或数据缺失：不猜，把问题交还给用户
                    return {
                        "status": "needs_clarification",
                        "clarification": clarification,
                        "answer": clarification["question"],
                        "thought": thought,
                    }
            meta = extract_meta_answer(reply.text) if allow_meta and round_ == 0 else None
            if meta is None:
                return {"candidate_sql": extract_sql(reply.text), "thought": thought}
            # 不查数据的回答有两道检查，不过就退回去让模型写 SQL 查：
            # 1. 问题本身得是在问口径 / 表结构，数据问题不能凭表结构作答；
            # 2. 出现的数字必须能在 schema / 口径 / 对话上下文里找到，否则就是没查数据却在报数
            about_meta = _META_CUE.search(state["question"])
            if not (about_meta or _CHAT_CUE.search(state["question"])):
                nudge = prompts.META_NEEDS_DATA
                self._trace(state).span("meta_answer_check", metadata={"rejected": "needs_data"})
            else:
                sources = "\n".join((self._context(state), state.get("conversation", "")))
                violations = check_answer(meta, None, state["question"], sources)
                self._trace(state).span("meta_answer_check", metadata={"violations": violations})
                if not violations:
                    return {"status": "ok_meta" if about_meta else "ok_chat", "answer": meta, "thought": thought}
                nudge = prompts.META_NUDGE.format(violations="、".join(violations))
            messages = messages + [
                {"role": "assistant", "content": reply.text},
                {"role": "user", "content": nudge},
            ]
        return {"candidate_sql": extract_sql(reply.text), "thought": thought}

    @staticmethod
    def _route_after_generate(state: _State) -> str:
        status = state.get("status")
        if status == "needs_clarification":
            return "clarify"
        if status == "ok_meta":
            return "explain"
        if status == "ok_chat":
            return "reply"
        if status in ("budget_exceeded", "failed"):
            return "fallback"
        return "execute"

    def _node_clarify(self, state: _State) -> _State:
        self._trace(state).span("clarify", metadata=state.get("clarification") or {})
        return {}

    def _node_reply(self, state: _State) -> _State:
        """闲聊：模型已经回应过（ok_chat）就直接结束；整句寒暄不调用模型，用固定的自我介绍回复。"""
        if state.get("status") == "ok_chat":
            return {}
        ds = for_db(self.settings.db_path)
        note = self.settings.dataset_note or (ds.description if ds else "")
        samples = [s["q"] for s in (ds.samples if ds else ()) if not s.get("tag")]
        text = smalltalk.reply(state.get("small_talk") or "intro", note, samples, sorted(self._snap.table_docs))
        self._trace(state).span("small_talk", metadata={"kind": state.get("small_talk")})
        return {"status": "ok_chat", "answer": text}

    def _node_explain(self, state: _State) -> _State:
        """问口径 / 表结构：回答已在 generate_sql 里给出，这里只留一条追踪记录。"""
        self._trace(state).span("explain", metadata={"answer_chars": len(state.get("answer", ""))})
        return {}

    def _node_execute(self, state: _State) -> _State:
        sql_raw = state.get("candidate_sql", "")
        verdict = validate(
            sql_raw,
            allowed_tables=state.get("allowed_tables", self._snap.allowed_tables),
            max_rows=self.settings.sql_max_rows,
            dialect=self.db.dialect,
        )
        if not verdict.allowed:
            attempt = Attempt(
                sql_raw=sql_raw,
                sql_final=None,
                ok=False,
                error_kind=verdict.error_kind,
                error_message=verdict.reason,
            )
        else:
            result = self.db.run_query(verdict.sql)
            attempt = Attempt(
                sql_raw=sql_raw,
                sql_final=verdict.sql,
                ok=result.ok,
                error_kind=result.error_kind,
                error_message=result.error_message,
                result=result,
            )
            missing = self._impossible_filters(state, sql_raw, result)
            if missing:
                # COUNT/SUM 得 0 不会报错，最容易被当成真实答案；过滤值在库里根本不存在时，
                # 几乎一定是写法不对（中英文、大小写、缩写）——按空结果处理，交给修复轮核对真实取值。
                # 模型核对后原样重发同一条 SQL，表示确认结果确实为空
                attempt.ok = False
                attempt.error_kind = "empty_result"
                attempt.error_message = f"查询结果为 0 或空值，并且过滤值{'；'.join(missing)}，写法可能不对"
        self._trace(state).span(
            "execute",
            metadata={
                "sql": attempt.sql_final or attempt.sql_raw,
                "ok": attempt.ok,
                "error_kind": attempt.error_kind,
                "error_message": attempt.error_message,
                "row_count": attempt.result.row_count if attempt.result else None,
                "latency_ms": attempt.result.latency_ms if attempt.result else None,
            },
        )
        return {"attempts": state["attempts"] + [attempt]}

    def _impossible_filters(self, state: _State, sql: str, result: QueryResult) -> list[str]:
        """结果看起来是"没查到"（单行且全为 0 / 空）时，找出在库里一次都没出现过的精确过滤值。"""
        if not self.settings.repair_value_probe or not disclosure.looks_empty(result):
            return []
        snap: _SchemaSnapshot = state.get("schema_snap") or self._snap
        filters = disclosure.text_filters(sql, snap.columns, self.db.dialect)
        return disclosure.missing_values(self.db, filters) if filters else []

    def _route_after_execute(self, state: _State) -> str:
        if state.get("status") in ("budget_exceeded", "failed"):
            return "fallback"
        last = state["attempts"][-1]
        if last.ok:
            return "chart" if state.get("generate_chart") else "answer"
        meter: UsageMeter = state["meter"]
        if meter.exceeded():
            return "fallback"
        # 首次生成占 1 次，之后每轮 repair 占 1 次
        if len(state["attempts"]) > self.settings.agent_max_repair_rounds:
            return "fallback"
        return "repair"

    def _node_repair(self, state: _State) -> _State:
        """修复前先补充观察（确定性、不调用模型），再进入修复内循环：
        - 渐进式披露：SQL 里用到但还没展开的表，补上完整定义；
        - 空结果：查出过滤列真实出现过的取值（最常见的空结果原因是取值写法不对）。"""
        attempts = state["attempts"]
        snap: _SchemaSnapshot = state.get("schema_snap") or self._snap
        updates: dict = {}
        new_tables: list[str] = []
        if state.get("schema_mode") == "disclose":
            expanded = list(state.get("expanded_tables") or [])
            by_lower = {t.lower(): t for t in snap.table_docs}
            for a in attempts:
                for name in sorted(tables_in_sql(a.sql_raw, self.db.dialect)):
                    real = by_lower.get(name)
                    if real and real not in expanded:
                        expanded.append(real)
                        new_tables.append(real)
            if new_tables:
                updates["expanded_tables"] = expanded
        probed: list[str] = []
        last = attempts[-1]
        if last.error_kind == "empty_result" and self.settings.repair_value_probe:
            pairs = disclosure.filtered_columns(last.sql_raw, snap.columns, self.db.dialect)
            notes, probed = self._probe(state, pairs)
            if probed:
                updates["value_notes"] = (state.get("value_notes") or []) + notes
                updates["probed"] = (state.get("probed") or []) + probed
        if new_tables or probed:
            updates["step_detail"] = _repair_detail(new_tables, probed)
        return {**updates, **self._repair_loop({**state, **updates})}

    def _repair_loop(self, state: _State) -> _State:
        """手写修复内循环：观察历史 → 生成修正 SQL → 重复检测（最多提醒一次）。"""
        attempts = state["attempts"]
        seen = {normalize_sql(a.sql_raw) for a in attempts}
        last = attempts[-1]
        history = "\n\n".join(a.describe(i + 1) for i, a in enumerate(attempts))
        messages = [
            {"role": "system", "content": self._sql_system(state)},
            {"role": "user", "content": self._sql_user(state)},
            {
                "role": "user",
                "content": prompts.REPAIR_USER_TEMPLATE.format(
                    attempts=history,
                    hint=prompts.REPAIR_HINTS.get(last.error_kind or "", ""),
                ),
            },
        ]
        for inner_round in range(2):  # 第二轮带"换思路"提醒
            try:
                reply = self.llm.chat(messages, state["meter"], tag="repair")
            except BudgetExceeded:
                return {"status": "budget_exceeded", "give_up_reason": "预算超限"}
            except LLMError as e:
                return {"status": "failed", "give_up_reason": f"LLM 调用失败: {e}"}
            self._record_generation(state, "repair", messages, reply)
            candidate = extract_sql(reply.text)
            resent_last = normalize_sql(candidate) == normalize_sql(last.sql_raw)
            if resent_last and last.error_kind == "empty_result" and last.sql_final:
                # 模型【原样重发上一条】空结果 SQL：确认数据确实为空，接受为最终答案。
                # 注意必须严格等于上一条——重发更早的失败 SQL 是模型混乱，不是确认。
                return {"accept_empty": True, "thought": extract_thought(reply.text)}
            if normalize_sql(candidate) not in seen:
                return {"candidate_sql": candidate, "thought": extract_thought(reply.text)}
            messages = messages + [
                {"role": "assistant", "content": reply.text},
                {"role": "user", "content": prompts.REPAIR_NUDGE},
            ]
        return {"give_up_reason": "模型反复生成相同的失败 SQL，停止修复"}

    def _route_after_repair(self, state: _State) -> str:
        if state.get("status") in ("budget_exceeded", "failed"):
            return "fallback"
        if state.get("accept_empty"):
            return "answer"
        if state.get("give_up_reason"):
            return "fallback"
        return "execute"

    def _node_chart(self, state: _State) -> _State:
        """图表节点：模型写 matplotlib 代码 → 静态拒绝清单初筛 → 沙箱执行。
        失败只记录 chart_error，不影响查询与回答。"""
        attempts = state["attempts"]
        last_ok = next((a for a in reversed(attempts) if a.ok), None)
        if last_ok is None or last_ok.result is None:
            return {"chart_error": "没有可用的查询结果"}
        result = last_ok.result

        messages = [
            {"role": "system", "content": prompts.CHART_SYSTEM},
            {
                "role": "user",
                "content": prompts.CHART_USER_TEMPLATE.format(
                    question=state["question"],
                    columns=result.columns,
                    rows_preview=result.preview(max_rows=8),
                    row_count=result.row_count,
                ),
            },
        ]
        try:
            reply = self.llm.chat(messages, state["meter"], tag="chart")
        except (BudgetExceeded, LLMError) as e:
            return {"chart_error": f"图表代码生成失败: {e}"}
        self._record_generation(state, "chart", messages, reply)

        code = extract_code(reply.text)
        denied = _CHART_CODE_DENY.search(code)
        if denied:
            self._trace(state).span("chart_code_rejected", metadata={"pattern": denied.group(0)})
            return {"chart_error": f"图表代码包含被禁止的调用（{denied.group(0)}），已拒绝执行"}

        if self._sandbox is None:
            self._sandbox = build_sandbox(self.settings)
        data = {"columns": result.columns, "rows": [list(row) for row in result.rows]}
        try:
            sandbox_result = self._sandbox.run(code, data, self.settings.chart_out_dir)
        except Exception as e:  # noqa: BLE001 —— 图表是锦上添花，失败不能中断回答
            return {"chart_error": f"图表执行失败：{type(e).__name__}: {e}"}
        self._trace(state).span(
            "chart_sandbox",
            metadata={
                "executor": self._sandbox.name,
                "ok": sandbox_result.ok,
                "error": sandbox_result.error,
            },
        )
        if not sandbox_result.ok:
            return {"chart_error": sandbox_result.error}
        return {"chart_path": sandbox_result.chart_path}

    def _node_answer(self, state: _State) -> _State:
        attempts = state["attempts"]
        generate_answer = state.get("generate_answer", True)
        last_ok = next((a for a in reversed(attempts) if a.ok), None)
        if state.get("accept_empty") and last_ok is None:
            answer = "查询执行成功，但没有符合条件的数据。" if generate_answer else ""
            return {"status": "ok_empty", "answer": answer}

        assert last_ok is not None and last_ok.result is not None
        if not generate_answer:
            return {"status": "ok", "answer": ""}
        messages = [
            {"role": "system", "content": prompts.ANSWER_SYSTEM},
            {
                "role": "user",
                "content": prompts.ANSWER_USER_TEMPLATE.format(
                    question=state["question"],
                    sql=last_ok.sql_final,
                    result=last_ok.result.preview(max_rows=20),
                ),
            },
        ]
        on_delta = state.get("on_answer_delta")
        try:
            reply = self.llm.chat(messages, state["meter"], tag="answer", on_delta=on_delta)
        except (BudgetExceeded, LLMError):
            # 总结失败不影响查询本身的成功：降级为直接给数据预览
            return {"status": "ok", "answer": f"查询成功，结果如下：\n{last_ok.result.preview()}"}
        self._record_generation(state, "answer", messages, reply)
        answer_text = plain_answer(reply.text)

        if not self.settings.answer_verify:
            return {"status": "ok", "answer": answer_text}

        # 防幻觉校验：回答里的数字必须在查询结果/问题/SQL 里有出处
        violations = check_answer(
            answer_text, last_ok.result, state["question"], last_ok.sql_final or ""
        )
        if violations:
            retry_messages = messages + [
                {"role": "assistant", "content": reply.text},
                {
                    "role": "user",
                    "content": prompts.ANSWER_RETRY_TEMPLATE.format(violations="、".join(violations)),
                },
            ]
            try:
                # 重写也走流式：前端以"当前调用的累积文本"整体替换，草稿自然被覆盖
                retry_reply = self.llm.chat(
                    retry_messages, state["meter"], tag="answer_retry", on_delta=on_delta
                )
                self._record_generation(state, "answer_retry", retry_messages, retry_reply)
                retry_text = plain_answer(retry_reply.text)
                violations = check_answer(
                    retry_text, last_ok.result, state["question"], last_ok.sql_final or ""
                )
                if not violations:
                    answer_text = retry_text
            except (BudgetExceeded, LLMError):
                pass  # 重写失败按仍有违规处理，走降级
        self._trace(state).span("hallucination_check", metadata={"violations": violations})
        if violations:
            # 一次重写仍有无出处数字：拒绝出稿，降级为确定性的结果预览
            return {
                "status": "ok",
                "answer": (
                    "（回答中存在无出处的数字，已自动降级为原始查询结果）\n"
                    + last_ok.result.preview(max_rows=20)
                ),
                "hallucination_blocked": True,
            }
        return {"status": "ok", "answer": answer_text, "numbers_verified": checked_number_count(answer_text)}

    def _node_fallback(self, state: _State) -> _State:
        """无 LLM 降级收尾：把已知信息如实交代，绝不编造。"""
        attempts = state["attempts"]
        status = state.get("status") or "failed"
        reason = state.get("give_up_reason", "自动修复轮次已用尽")
        # 路由函数不能写状态：经 _route_after_execute 因预算超限进来时在此落真实原因
        meter: UsageMeter | None = state.get("meter")
        if status == "failed" and not state.get("give_up_reason") and meter and meter.exceeded():
            status, reason = "budget_exceeded", "预算超限"
        lines = [f"未能得到可靠的查询结果（{reason}）。"]
        if attempts:
            last = attempts[-1]
            lines.append(f"最后一次尝试的 SQL：\n{last.sql_raw}")
            if last.error_message:
                lines.append(f"错误信息：[{last.error_kind}] {last.error_message}")
        return {"status": status if status != "ok" else "failed", "answer": "\n".join(lines)}
