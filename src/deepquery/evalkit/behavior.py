"""行为评测与分析评测：测"该不该写 SQL"和"多步分析的结论靠不靠得住"。

    uv run python -m deepquery.evalkit.behavior --db data/olist/olist.sqlite --label baseline
    uv run python -m deepquery.evalkit.behavior --db data/olist/olist.sqlite --analysis --label baseline
    uv run python -m deepquery.evalkit.behavior --db data/olist/olist.sqlite --gold-check   # 不调用模型，只检查标准 SQL

行为评测（eval/cases/olist-behavior.jsonl）：
- 路径准确率：该问的问、该直接回答的直接回答、该查的查；
- 反问的精确率 / 召回率，以及"不该问却问了"的比例（问多了用户会烦）；
- 普通问题和追问的执行准确率（有标准 SQL 的题）。
分析评测（eval/cases/olist-analysis.jsonl）：没有标准答案，统计结论能否逐句核对出处、各步成功率、步数、耗时、花费。
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from ..config import get_settings
from .runner import load_cases
from .scorer import execution_match
from .stats import wilson_interval

console = Console()

ROUTES = {"needs_clarification": "clarify", "ok_meta": "meta", "ok_chat": "chat"}


def route_of(status: str) -> str:
    """运行状态 → 走了哪条路：反问 / 直接回答口径 / 寒暄 / 查数据（成功、空结果、失败都算查了）。"""
    return ROUTES.get(status, "sql")


def _rate(ok: int, n: int) -> dict:
    low, high = wilson_interval(ok, n) if n else (0.0, 0.0)
    return {"ok": ok, "n": n, "rate": round(ok / n, 4) if n else None, "ci": [round(low, 4), round(high, 4)]}


def run_behavior(agent, cases: list[dict], db_path: str, repeats: int = 1) -> dict:
    results = []
    for case in cases:
        for _ in range(repeats):
            started = time.monotonic()
            outcome = agent.ask(
                case["question"],
                generate_answer=False,
                interactive=True,
                allow_clarify=True,
                history=case.get("history") or [],
            )
            route = route_of(outcome.status)
            gold = case.get("gold_sql")
            ex = None
            if gold:
                ex = route == "sql" and execution_match(db_path, outcome.predicted_sql, gold).match
            results.append({
                "id": case["id"],
                "category": case["category"],
                "question": case["question"],
                "expect": case["expect"],
                "route": route,
                "route_ok": route in case["expect"],
                "ex": ex,
                "status": outcome.status,
                "predicted_sql": outcome.predicted_sql,
                "clarification": outcome.clarification,
                "answer": (outcome.answer or "")[:300],
                "cost": outcome.usage.get("cost", 0.0),
                "latency_ms": int((time.monotonic() - started) * 1000),
            })
    return {"results": results, "summary": summarize_behavior(results)}


def summarize_behavior(results: list[dict]) -> dict:
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    should_ask = [r for r in results if "clarify" in r["expect"]]
    asked = [r for r in results if r["route"] == "clarify"]
    must_not_ask = [r for r in results if "clarify" not in r["expect"]]
    with_gold = [r for r in results if r["ex"] is not None]
    confusion: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        confusion[r["expect"][0]][r["route"]] += 1
    return {
        "trials": len(results),
        "route_accuracy": _rate(sum(r["route_ok"] for r in results), len(results)),
        "by_category": {
            cat: {
                "route": _rate(sum(r["route_ok"] for r in rs), len(rs)),
                "ex": _rate(sum(bool(r["ex"]) for r in rs if r["ex"] is not None), sum(r["ex"] is not None for r in rs)),
            }
            for cat, rs in sorted(by_cat.items())
        },
        # 反问：精确率 = 问了的里面该问的比例；召回率 = 该问的里面问了的比例
        "clarify_precision": _rate(sum("clarify" in r["expect"] for r in asked), len(asked)),
        "clarify_recall": _rate(sum(r["route"] == "clarify" for r in should_ask), len(should_ask)),
        "over_clarify": _rate(sum(r["route"] == "clarify" for r in must_not_ask), len(must_not_ask)),
        "ex_accuracy": _rate(sum(bool(r["ex"]) for r in with_gold), len(with_gold)),
        "confusion": {k: dict(v) for k, v in confusion.items()},
        "total_cost": round(sum(r["cost"] for r in results), 6),
        "avg_latency_ms": int(sum(r["latency_ms"] for r in results) / len(results)) if results else 0,
    }


def run_analysis(agent, cases: list[dict]) -> dict:
    results = []
    for case in cases:
        outcome = agent.analyst.analyze(case["question"])
        steps = outcome.steps
        results.append({
            "id": case["id"],
            "question": case["question"],
            "status": outcome.status,
            "steps": len(steps),
            "steps_ok": sum(1 for s in steps if s.get("ok")),
            "drilled": len(steps) > outcome.planned,  # 看完结果后追加了下钻步骤
            "blocked": outcome.hallucination_blocked,
            "numbers_verified": outcome.numbers_verified,
            "answer": outcome.answer,
            "sqls": [s.get("predicted_sql") for s in steps],
            "cost": outcome.usage.get("cost", 0.0),
            "llm_calls": outcome.usage.get("llm_calls", 0),
            "latency_ms": outcome.latency_ms,
        })
    n = len(results)
    total_steps = sum(r["steps"] for r in results)
    summary = {
        "questions": n,
        "completed": _rate(sum(r["status"] == "ok" for r in results), n),
        # 结论通过逐句溯源（没有被拦下改成列结果）的比例
        "cited_ok": _rate(sum(r["status"] == "ok" and not r["blocked"] for r in results), n),
        "drilled": _rate(sum(r["drilled"] for r in results), n),
        "step_success": _rate(sum(r["steps_ok"] for r in results), total_steps),
        "avg_steps": round(total_steps / n, 2) if n else 0,
        "avg_numbers_verified": round(sum(r["numbers_verified"] for r in results) / n, 2) if n else 0,
        "avg_llm_calls": round(sum(r["llm_calls"] for r in results) / n, 1) if n else 0,
        "avg_latency_ms": int(sum(r["latency_ms"] for r in results) / n) if n else 0,
        "total_cost": round(sum(r["cost"] for r in results), 6),
    }
    return {"results": results, "summary": summary}


def gold_check(cases: list[dict], db_path: str) -> list[str]:
    """不调用模型：标准 SQL 和追问的上一轮 SQL 都要能跑通且有数据。返回问题列表。"""
    import sqlite3

    problems = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        for case in cases:
            sqls = [("gold", case.get("gold_sql"))] + [("history", h.get("sql")) for h in case.get("history") or []]
            for kind, sql in sqls:
                if not sql:
                    continue
                try:
                    rows = conn.execute(sql).fetchall()
                except sqlite3.Error as e:
                    problems.append(f"{case['id']} {kind}: {e}")
                    continue
                if not rows:
                    problems.append(f"{case['id']} {kind}: 没有数据")
    finally:
        conn.close()
    return problems


def _pct(r: dict) -> str:
    if r["rate"] is None:
        return "—"
    return f"{r['rate']:.0%}（{r['ok']}/{r['n']}，95% CI {r['ci'][0]:.0%}–{r['ci'][1]:.0%}）"


def _print_behavior(summary: dict, label: str, out: Path) -> None:
    table = Table(title=f"行为评测 · {label}")
    table.add_column("指标")
    table.add_column("值", justify="right")
    table.add_row("路径准确率", _pct(summary["route_accuracy"]))
    table.add_row("反问精确率（问了的里面该问的）", _pct(summary["clarify_precision"]))
    table.add_row("反问召回率（该问的里面问了的）", _pct(summary["clarify_recall"]))
    table.add_row("不该问却问了", _pct(summary["over_clarify"]))
    table.add_row("执行准确率（有标准 SQL 的题）", _pct(summary["ex_accuracy"]))
    for cat, v in summary["by_category"].items():
        ex = f"，EX {_pct(v['ex'])}" if v["ex"]["n"] else ""
        table.add_row(f"  {cat}", f"路径 {_pct(v['route'])}{ex}")
    table.add_row("总花费 / 平均延迟", f"{summary['total_cost']:.4f} / {summary['avg_latency_ms']} ms")
    table.add_row("报告文件", str(out))
    console.print(table)


def _print_analysis(summary: dict, label: str, out: Path) -> None:
    table = Table(title=f"分析评测 · {label}")
    table.add_column("指标")
    table.add_column("值", justify="right")
    table.add_row("完成（给出结论）", _pct(summary["completed"]))
    table.add_row("结论通过逐句溯源", _pct(summary["cited_ok"]))
    table.add_row("各步查询成功率", _pct(summary["step_success"]))
    table.add_row("平均步数 / 平均核对数字", f"{summary['avg_steps']} / {summary['avg_numbers_verified']}")
    table.add_row("平均模型调用 / 平均耗时", f"{summary['avg_llm_calls']} 次 / {summary['avg_latency_ms']} ms")
    table.add_row("总花费", f"{summary['total_cost']:.4f}")
    table.add_row("报告文件", str(out))
    console.print(table)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="行为评测 / 分析评测")
    parser.add_argument("--cases", default=None, help="默认：行为评测用 olist-behavior，分析评测用 olist-analysis")
    parser.add_argument("--db", default=None, help="评测用的库（默认 DB_PATH）")
    parser.add_argument("--label", default="behavior")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--analysis", action="store_true", help="跑分析模式评测")
    parser.add_argument("--gold-check", action="store_true", help="只检查标准 SQL 能跑通（不调用模型）")
    args = parser.parse_args(argv)

    settings = get_settings()
    if args.db:
        settings = settings.model_copy(update={"db_path": args.db})
    default_cases = "eval/cases/olist-analysis.jsonl" if args.analysis else "eval/cases/olist-behavior.jsonl"
    cases = load_cases(args.cases or default_cases)

    if args.gold_check:
        problems = gold_check(cases, settings.db_path)
        for p in problems:
            console.print(f"[red]✗[/red] {p}")
        console.print(f"标准 SQL 检查：{len(cases)} 题，{len(problems)} 个问题")
        return 1 if problems else 0

    from .. import build_agent

    agent = build_agent(settings)
    kind = "analysis" if args.analysis else "behavior"
    report = run_analysis(agent, cases) if args.analysis else run_behavior(agent, cases, settings.db_path, args.repeats)
    report["summary"].update({"label": args.label, "kind": kind, "model": settings.llm_model, "db": settings.db_path})
    out_dir = Path("eval/results")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{kind}-{args.label}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (_print_analysis if args.analysis else _print_behavior)(report["summary"], args.label, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
