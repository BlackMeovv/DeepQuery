"""分析模式：把"为什么下降""哪些因素影响"这类问题拆成几步查询，查完再写带出处的结论。

    plan ──Send×N──▶ run_step（并行，每步复用单问管线：守卫、修复、取值核对都在）
          ──▶ review（看完结果决定是否下钻，最多一轮）──Send×M──▶ run_step
          ──▶ report（每句结论标注出自第几步，逐句核对数字出自所引用的那一步）

可靠性放在编排层：步数上限、下钻只一轮、整次分析共用一个预算与取消开关；
模型只负责"拆问题"和"写结论"，数据全部来自真实执行的 SQL。
"""

from __future__ import annotations

import operator
import re
import time
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..budget import BudgetExceeded, RunHandle, UsageMeter
from ..guard import tables_in_sql
from ..llm import LLMError
from ..verify import check_cited, cited_number_count
from . import prompts
from .graph import _CODE_BLOCK, extract_thought, plain_answer

_PLAN_LINE = re.compile(r"^\s*(\d{1,2})\s*[.、)）]\s*(.+?)\s*$")


def parse_plan(text: str, start: int = 1, limit: int = 4) -> list[dict]:
    """解析 ```plan 代码块："序号. 子问题 | 目的"。编号由系统重排，从 start 开始。"""
    body = next((b for lang, b in _CODE_BLOCK.findall(text or "") if lang.lower() == "plan"), "")
    steps: list[dict] = []
    for line in body.splitlines():
        m = _PLAN_LINE.match(line)
        if not m:
            continue
        question, _, purpose = m.group(2).partition("|")
        question = question.strip().strip("`")
        if question and len(steps) < limit:
            steps.append({"no": start + len(steps), "question": question[:200], "purpose": purpose.strip()[:80]})
    return steps


def is_done(text: str) -> bool:
    return any(lang.lower() == "done" for lang, _ in _CODE_BLOCK.findall(text or ""))


@dataclass
class AnalysisOutcome:
    question: str
    status: str  # ok / failed / budget_exceeded
    answer: str = ""
    steps: list[dict] = field(default_factory=list)  # 按步骤号排好；每步含 result（QueryResult）
    plan_thought: str = ""
    review_note: str = ""
    numbers_verified: int = 0
    hallucination_blocked: bool = False
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0

    @property
    def succeeded(self) -> bool:
        return self.status == "ok"


class _AState(TypedDict, total=False):
    question: str
    user_id: str
    conversation: str
    context: str
    meter: Any
    on_delta: Any
    steps: list[dict]  # 计划（含下钻追加的）
    results: Annotated[list[dict], operator.add]  # 各步结果，并行写入
    pending: list[dict]  # 本轮要执行的步骤
    reviewed: bool
    plan_thought: str
    review_note: str
    status: str
    answer: str
    numbers_verified: int
    hallucination_blocked: bool


class Analyst:
    def __init__(self, agent):
        self.agent = agent
        self.settings = agent.settings
        self._graph = self._build()

    # ---------- public ----------

    def analyze_stream(
        self,
        question: str,
        user_id: str = "default",
        history: list[dict] | None = None,
        on_answer_delta=None,
        handle: RunHandle | None = None,
    ):
        """逐节点 yield ("node", 节点名, 增量)，最后 yield ("final", AnalysisOutcome, None)。"""
        start = time.monotonic()
        state, meter = self._prepare(question, user_id, history, on_answer_delta)
        if handle is not None:
            meter.cancel_event = handle.cancelled
            handle.meter = meter
        final: dict = dict(state)
        for update in self._graph.stream(state, config={"recursion_limit": 40}, stream_mode="updates"):
            for node, delta in update.items():
                delta = delta or {}
                for key, value in delta.items():
                    final[key] = final.get(key, []) + value if key == "results" else value
                yield ("node", node, delta)
        yield ("final", self._finish(question, final, meter, start), None)

    def analyze(self, question: str, user_id: str = "default", history: list[dict] | None = None) -> AnalysisOutcome:
        outcome = None
        for kind, item, _ in self.analyze_stream(question, user_id=user_id, history=history):
            if kind == "final":
                outcome = item
        return outcome

    # ---------- plumbing ----------

    def _prepare(self, question, user_id, history, on_delta):
        agent = self.agent
        agent.maybe_refresh_schema()
        snap = agent._snap
        turns = [t for t in (history or []) if t.get("question")]
        lookup = f"{turns[-1]['question']} {question}" if turns else question
        schema, knowledge, _selected, _used, mode = agent._build_schema_context(lookup, snap, user_id=user_id)
        if mode == "disclose":  # 大库：规划只需要知道有哪些表和列
            schema = "表目录（只有说明和列名）：\n" + "\n".join(snap.catalog.values())
        meter = agent.new_meter(self.settings.analysis_budget_factor)
        conversation = prompts.format_history(turns)
        state: _AState = {
            "question": question,
            "user_id": user_id,
            "conversation": conversation,
            "context": schema + knowledge,
            "meter": meter,
            "on_delta": on_delta,
            "steps": [],
            "results": [],
            "pending": [],
            "reviewed": False,
        }
        return state, meter

    def _finish(self, question: str, final: dict, meter: UsageMeter, start: float) -> AnalysisOutcome:
        by_no = {r["no"]: r for r in final.get("results", [])}
        steps = [by_no.get(s["no"], {**s, "status": "skipped", "ok": False, "error": "没有执行"}) for s in final.get("steps", [])]
        return AnalysisOutcome(
            question=question,
            status=final.get("status", "failed"),
            answer=final.get("answer", ""),
            steps=steps,
            plan_thought=final.get("plan_thought", ""),
            review_note=final.get("review_note", ""),
            numbers_verified=final.get("numbers_verified", 0),
            hallucination_blocked=final.get("hallucination_blocked", False),
            usage=meter.snapshot(),
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    def _build(self):
        g = StateGraph(_AState)
        g.add_node("plan", self._node_plan)
        g.add_node("run_step", self._node_run_step)
        g.add_node("review", self._node_review)
        g.add_node("report", self._node_report)
        g.add_edge(START, "plan")
        g.add_conditional_edges("plan", self._fan_out, ["run_step", "report"])
        g.add_edge("run_step", "review")
        g.add_conditional_edges("review", self._fan_out, ["run_step", "report"])
        g.add_edge("report", END)
        return g.compile()

    def _fan_out(self, state: _AState):
        """把本轮要执行的步骤并行派发出去；没有要执行的就写结论。"""
        pending = state.get("pending") or []
        if not pending or state.get("status") == "budget_exceeded":
            return "report"
        shared = {"question": state["question"], "user_id": state["user_id"], "meter": state["meter"]}
        return [Send("run_step", {**shared, "step": step}) for step in pending]

    # ---------- nodes ----------

    def _history_block(self, state: _AState) -> str:
        conv = state.get("conversation")
        return f"之前的对话（最近的在最后）：\n{conv}\n\n" if conv else ""

    def _node_plan(self, state: _AState) -> dict:
        limit = self.settings.analysis_plan_steps
        messages = [
            {"role": "system", "content": prompts.PLAN_SYSTEM.format(max_steps=limit)},
            {
                "role": "user",
                "content": prompts.PLAN_USER_TEMPLATE.format(
                    schema=state["context"], history=self._history_block(state), question=state["question"]
                ),
            },
        ]
        try:
            reply = self.agent.llm.chat(messages, state["meter"], tag="plan")
        except BudgetExceeded:
            return {"status": "budget_exceeded", "answer": "预算不足，没能开始分析。", "pending": []}
        except LLMError:
            reply = None
        steps = parse_plan(reply.text, limit=limit) if reply else []
        if not steps:
            # 拆不出子问题：退化成直接查原问题，至少给出一个有出处的答案
            steps = [{"no": 1, "question": state["question"], "purpose": "直接查询原问题"}]
        thought = extract_thought(reply.text) if reply else "没能拆分问题，直接查询原问题"
        return {"steps": steps, "pending": steps, "plan_thought": thought}

    def _node_run_step(self, payload: dict) -> dict:
        step, meter = payload["step"], payload["meter"]
        if meter.exceeded():
            return {"results": [{**step, "status": "skipped", "ok": False, "error": "预算用完，没有执行"}]}
        outcome = self.agent.ask(
            step["question"],
            generate_answer=False,
            user_id=payload["user_id"],
            interactive=True,
            allow_meta=False,
            meter=meter,
        )
        result = outcome.result
        ok = outcome.status == "ok"
        error = None
        if not ok:
            error = "查询结果为空" if outcome.status == "ok_empty" else (outcome.answer or outcome.status)[:300]
        return {
            "results": [{
                **step,
                "status": outcome.status,
                "ok": ok,
                "sql": outcome.final_sql,
                "predicted_sql": outcome.predicted_sql,
                "summary": outcome.sql_summary,
                "attempts": len(outcome.attempts),
                "error": error,
                "result": result if ok else None,
            }]
        }

    @staticmethod
    def _step_block(r: dict, max_rows: int) -> str:
        head = f"[{r['no']}] 子问题：{r['question']}" + (f"（目的：{r['purpose']}）" if r.get("purpose") else "")
        if r.get("result") is None:
            return f"{head}\n（这一步没有可用的结果：{r.get('error') or '未执行'}）"
        sql = r.get("predicted_sql") or r.get("sql") or ""
        return f"{head}\n执行的 SQL：\n```sql\n{sql}\n```\n结果：\n{r['result'].preview(max_rows=max_rows, max_cell=60)}"

    def _node_review(self, state: _AState) -> dict:
        """看完这一轮的结果，决定要不要追加下钻步骤（只一轮）。"""
        steps = state.get("steps") or []
        room = self.settings.analysis_max_steps - len(steps)
        meter: UsageMeter = state["meter"]
        if state.get("reviewed") or room <= 0 or meter.exceeded():
            return {"pending": [], "reviewed": True}
        results = sorted(state.get("results") or [], key=lambda r: r["no"])
        if not any(r.get("result") is not None for r in results):
            return {"pending": [], "reviewed": True}  # 一步都没查到，下钻没有意义
        blocks = "\n\n".join(self._step_block(r, 10) for r in results)
        messages = [
            {"role": "system", "content": prompts.REVIEW_SYSTEM.format(max_new=min(2, room))},
            {"role": "user", "content": f"要回答的问题：{state['question']}\n\n已完成的查询：\n\n{blocks}"},
        ]
        try:
            reply = self.agent.llm.chat(messages, meter, tag="review")
        except (BudgetExceeded, LLMError):
            return {"pending": [], "reviewed": True}
        if is_done(reply.text):
            return {"pending": [], "reviewed": True, "review_note": extract_thought(reply.text)}
        added = parse_plan(reply.text, start=len(steps) + 1, limit=min(2, room))
        return {
            "steps": steps + added,
            "pending": added,
            "reviewed": True,
            "review_note": extract_thought(reply.text),
        }

    def _node_report(self, state: _AState) -> dict:
        if state.get("status") == "budget_exceeded" and not state.get("results"):
            return {}
        results = sorted(state.get("results") or [], key=lambda r: r["no"])
        usable = [r for r in results if r.get("result") is not None]
        if not usable:
            lines = [f"[{r['no']}] {r['question']}：{r.get('error') or '没有结果'}" for r in results]
            return {"status": "failed", "answer": "各步查询都没有得到可用的结果，没法下结论。\n" + "\n".join(lines)}
        cite_map = {r["no"]: (r.get("result"), r.get("predicted_sql") or r.get("sql") or "") for r in results}
        messages = [
            {"role": "system", "content": prompts.REPORT_SYSTEM},
            {
                "role": "user",
                "content": prompts.REPORT_USER_TEMPLATE.format(
                    question=state["question"], steps="\n\n".join(self._step_block(r, 15) for r in results)
                ),
            },
        ]
        on_delta = state.get("on_delta")
        meter: UsageMeter = state["meter"]
        try:
            reply = self.agent.llm.chat(messages, meter, tag="report", on_delta=on_delta)
        except (BudgetExceeded, LLMError):
            return {"status": "ok", "answer": self._listing(usable, "（没能生成结论，下面是各步的查询结果）")}
        text = plain_answer(reply.text)
        problems = check_cited(text, cite_map, state["question"])
        if problems:
            retry = messages + [
                {"role": "assistant", "content": reply.text},
                {"role": "user", "content": prompts.REPORT_RETRY_TEMPLATE.format(problems="；".join(problems[:8]))},
            ]
            try:
                again = self.agent.llm.chat(retry, meter, tag="report_retry", on_delta=on_delta)
                text2 = plain_answer(again.text)
                problems2 = check_cited(text2, cite_map, state["question"])
                if not problems2:
                    text, problems = text2, []
            except (BudgetExceeded, LLMError):
                pass
        if problems:
            # 重写一次仍对不上出处：不出稿，改为确定性地列出各步结果
            return {
                "status": "ok",
                "answer": self._listing(usable, "（结论里有数字对不上所标注步骤的结果，已改为直接列出各步查询结果）"),
                "hallucination_blocked": True,
            }
        return {"status": "ok", "answer": text, "numbers_verified": cited_number_count(text)}

    @staticmethod
    def _listing(usable: list[dict], note: str) -> str:
        lines = [note]
        for r in usable:
            res = r["result"]
            first = "；".join(
                "，".join(f"{c}={v}" for c, v in zip(res.columns, row)) for row in res.rows[:3]
            )
            more = f"（共 {res.row_count} 行）" if res.row_count > 3 else ""
            lines.append(f"- {r['question']}：{first}{more} [{r['no']}]")
        return "\n".join(lines)


def source_tables(steps: list[dict]) -> list[str]:
    tables: set[str] = set()
    for s in steps:
        if s.get("sql"):
            tables |= tables_in_sql(s["sql"])
    return sorted(tables)
