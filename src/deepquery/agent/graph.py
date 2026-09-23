"""Agent 编排：LangGraph 外层状态机 + 手写的修复内循环。

外层（LangGraph 负责确定性流转）：
    generate_sql → execute →（成功）→ answer
                          →（失败且还有额度）→ repair → execute ...
                          →（轮次/预算耗尽）→ fallback

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
from langgraph.graph import END, StateGraph

from ..budget import BudgetExceeded, RunHandle, UsageMeter
from ..config import Settings
from ..datasets import knowledge_paths
from ..guard import validate
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
    status: str  # ok / ok_empty / failed / budget_exceeded / needs_clarification
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
    result: QueryResult | None = None
    attempts: list[Attempt] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0

    @property
    def succeeded(self) -> bool:
        return self.status in ("ok", "ok_empty")


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
        self._graph = self._build_graph()

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
        return _SchemaSnapshot(
            fingerprint=fingerprint,
            allowed_tables=allowed,
            table_docs=docs,
            full_schema="\n\n".join(docs.values()),
            retriever=SchemaRetriever(docs, embedder=build_embedder(self.settings)),
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

    def _build_schema_context(
        self, question: str, snap: _SchemaSnapshot, user_id: str = "default"
    ) -> tuple[str, list[str] | None, dict]:
        """按问题组装 schema 上下文（基于调用方传入的同一份快照）。

        大库不能全量塞 prompt（贵且触发 Lost in the Middle）——auto 模式按
        全量 schema 体积决定是否检索选表。装得下就直供：BIRD 消融中两者 EX
        无统计差异，而检索只省约 3% token、却多一处召回失败点；命中的业务字典
        与相似例句始终附加。
        """
        mode = self.settings.schema_rag
        k = self.settings.schema_rag_top_k
        full_chars = sum(len(d) for d in snap.table_docs.values())
        use_rag = mode == "on" or (
            mode == "auto"
            and len(snap.table_docs) > k
            and full_chars > self.settings.schema_rag_auto_max_chars
        )
        if use_rag:
            selected = snap.retriever.top_tables(question, k)
            context = "\n\n".join(snap.table_docs[t] for t in selected)
        else:
            selected = None
            context = snap.full_schema

        top_n = self.settings.knowledge_top_n
        glossary_hits = self._glossary.top(question, top_n)
        if glossary_hits:
            context += "\n\n业务字典（口径定义）：\n" + "\n".join(e.body for e in glossary_hits)
        example_hits = self._examples.top(question, top_n)
        if example_hits:
            context += "\n\n相似问题参考：\n" + "\n\n".join(e.body for e in example_hits)
        memory_hits: list[str] = []
        if self.memory is not None:
            memory_hits = self.memory.recall(user_id, question, top_n)
            if memory_hits:
                context += "\n\n该用户的口径偏好（跨会话记忆，优先遵循）：\n" + "\n".join(
                    f"- {m}" for m in memory_hits
                )
        # 本次实际注入的上下文明细（UI 的"上下文"面板与可解释性用）
        context_used = {
            "glossary": [e.key for e in glossary_hits],
            "examples": [e.key for e in example_hits],
            "memories": memory_hits,
        }
        return context, selected, context_used

    # ---------- public ----------

    def ask(
        self,
        question: str,
        generate_answer: bool = True,
        generate_chart: bool = False,
        user_id: str = "default",
        allow_clarify: bool = False,
        interactive: bool = False,
    ) -> RunOutcome:
        """回答一个自然语言问题。generate_answer=False 时跳过总结节点（评测省成本）；
        generate_chart=True 时对成功结果生成图表（模型写代码 → 沙箱执行）；
        allow_clarify=True 时问题有歧义或数据缺失会返回 needs_clarification（交互场景用，
        评测保持关闭，提示词与历史评测一致）。"""
        start = time.monotonic()
        state, meter, trace, selected_tables, context_used = self._prepare_run(
            question, generate_answer, generate_chart, user_id, allow_clarify, interactive
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
    ):
        """逐节点流式执行（服务端 SSE 用）。

        依次 yield ("node", 节点名, 增量状态)，最后 yield ("final", RunOutcome, None)。
        on_answer_delta：回答文本的逐字增量回调（在 answer 节点的 LLM 调用中触发）。
        handle：调用方的控制句柄。handle.cancel() 后，下一次 LLM 调用前（或流式输出途中）
        抛出 RunCancelled 结束运行；handle.meter 始终指向本次的用量，供调用方记账。
        """
        start = time.monotonic()
        state, meter, trace, selected_tables, context_used = self._prepare_run(
            question, generate_answer, generate_chart, user_id, allow_clarify, interactive
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
    ):
        self.maybe_refresh_schema()  # 建/改表后无需重启即生效（CLI/MCP/服务共用此入口）
        snap = self._snap  # 本次运行全程只用这一份快照
        meter = UsageMeter(
            price_input_per_m=self.settings.llm_price_input_per_m,
            price_output_per_m=self.settings.llm_price_output_per_m,
            max_tokens=self.settings.agent_max_tokens_per_run,
            max_cost=self.settings.agent_max_cost_per_run,
        )
        trace = self.tracer.start_run(question)
        schema_context, selected_tables, context_used = self._build_schema_context(
            question, snap, user_id=user_id
        )
        # context_used 含用户私有记忆原文：只能随本次运行传递，绝不能挂在共享的
        # agent 实例上——并发请求会互相覆盖，把 A 的记忆吐给 B 并写进缓存
        if selected_tables is not None:
            trace.span("schema_rag", metadata={"selected_tables": selected_tables})
        state: _State = {
            "question": question,
            "schema_context": schema_context,
            "attempts": [],
            "generate_answer": generate_answer,
            "generate_chart": generate_chart,
            "meter": meter,
            "trace": trace,
            "allowed_tables": snap.allowed_tables,
            "allow_clarify": allow_clarify,
            "interactive": interactive,
        }
        return state, meter, trace, selected_tables, context_used

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
        g.add_node("generate_sql", self._node_generate_sql)
        g.add_node("execute", self._node_execute)
        g.add_node("repair", self._node_repair)
        g.add_node("chart", self._node_chart)
        g.add_node("summarize", self._node_answer)
        g.add_node("fallback", self._node_fallback)
        g.add_node("clarify", self._node_clarify)

        g.set_entry_point("generate_sql")
        g.add_conditional_edges(
            "generate_sql",
            lambda s: (
                "clarify"
                if s.get("status") == "needs_clarification"
                else "fallback" if s.get("status") in ("budget_exceeded", "failed") else "execute"
            ),
            {"execute": "execute", "fallback": "fallback", "clarify": "clarify"},
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

    def _node_generate_sql(self, state: _State) -> _State:
        system = self._sql_system(state)
        if state.get("allow_clarify"):
            system += prompts.CLARIFY_RULES
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": prompts.SQL_USER_TEMPLATE.format(
                    schema=state["schema_context"], question=state["question"]
                ),
            },
        ]
        try:
            reply = self.llm.chat(messages, state["meter"], tag="generate_sql")
        except BudgetExceeded:
            return {"status": "budget_exceeded", "give_up_reason": "预算超限"}
        except LLMError as e:
            return {"status": "failed", "give_up_reason": f"LLM 调用失败: {e}"}
        self._record_generation(state, "generate_sql", messages, reply)
        if state.get("allow_clarify"):
            clarification = extract_clarification(reply.text)
            if clarification:
                # 有歧义或数据缺失：不猜，把问题交还给用户
                return {
                    "status": "needs_clarification",
                    "clarification": clarification,
                    "answer": clarification["question"],
                    "thought": extract_thought(reply.text),
                }
        return {"candidate_sql": extract_sql(reply.text), "thought": extract_thought(reply.text)}

    def _node_clarify(self, state: _State) -> _State:
        self._trace(state).span("clarify", metadata=state.get("clarification") or {})
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
        """手写修复内循环：观察历史 → 生成修正 SQL → 重复检测（最多提醒一次）。"""
        attempts = state["attempts"]
        seen = {normalize_sql(a.sql_raw) for a in attempts}
        last = attempts[-1]
        history = "\n\n".join(a.describe(i + 1) for i, a in enumerate(attempts))
        messages = [
            {"role": "system", "content": self._sql_system(state)},
            {
                "role": "user",
                "content": prompts.SQL_USER_TEMPLATE.format(
                    schema=state["schema_context"], question=state["question"]
                ),
            },
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
