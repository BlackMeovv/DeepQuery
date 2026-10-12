"""命令行入口。

    deepquery ask "上海的客户一共有多少个？" [--trace] [--no-answer]
    deepquery schema        # 查看喂给模型的 schema 上下文
    deepquery demo-db       # 生成演示库
    deepquery catalog build / status / show / describe / review / confirm   # 数据库建档与记忆同步
"""

from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


def _override_db(args: argparse.Namespace):
    """--db 指定任意 SQLite 文件：agent 与库完全解耦，schema 是运行时自省的。"""
    from .config import get_settings

    settings = get_settings()
    if getattr(args, "db", None):
        settings = settings.model_copy(update={"db_path": args.db})
    return settings


def _cmd_ask(args: argparse.Namespace) -> int:
    from . import build_agent

    agent = build_agent(_override_db(args), wait_catalog=True)
    outcome = agent.ask(
        args.question,
        generate_answer=not args.no_answer,
        generate_chart=args.chart,
        user_id=args.user,
        allow_clarify=True,
        interactive=True,
    )

    # 模型/数据库产出的文本都是不受信内容，必须 escape/Text 后再交给 rich 渲染
    if args.trace:
        for i, attempt in enumerate(outcome.attempts, 1):
            status = "[green]成功[/green]" if attempt.ok else (
                f"[red]{escape(attempt.error_kind or '')}[/red] {escape(attempt.error_message or '')}"
            )
            console.print(Panel(Text(attempt.sql_raw), title=f"尝试 {i} · {status}"))

    if outcome.final_sql:
        console.print(Panel(Text(outcome.final_sql), title="最终 SQL", border_style="cyan"))
    if outcome.result and outcome.result.ok:
        table = Table(*[Text(c) for c in outcome.result.columns])
        for row in outcome.result.rows[:20]:
            table.add_row(*[Text("NULL" if v is None else str(v)) for v in row])
        console.print(table)
        if outcome.result.row_count > 20:
            console.print(f"（共 {outcome.result.row_count} 行，仅展示前 20 行）")
    if outcome.clarification:
        c = outcome.clarification
        lines = [c["question"], ""] + [f"  {i}. {o}" for i, o in enumerate(c["options"], 1)]
        lines += ["", "在问题后补充说明重新提问，例如：", f'  deepquery ask "{args.question}（补充说明：{c["options"][0] if c["options"] else "……"}）"']
        console.print(Panel(Text("\n".join(lines)), title="需要向你确认", border_style="yellow"))
    elif outcome.answer:
        # 问口径 / 表结构时没有查数据，标题写明依据，不和查询结果混淆
        title = "回答（依据表结构与业务口径，未查询数据）" if outcome.status == "ok_meta" else "回答"
        console.print(Panel(Text(outcome.answer), title=title, border_style="green"))
    if outcome.chart_path:
        console.print(f"图表已生成: [cyan]{escape(outcome.chart_path)}[/cyan]")
    elif args.chart and outcome.chart_error:
        console.print(f"[yellow]图表生成失败：[/yellow]{escape(outcome.chart_error)}")

    usage = outcome.usage
    console.print(
        f"[dim]状态 {outcome.status} · LLM 调用 {usage.get('llm_calls', 0)} 次 · "
        f"tokens {usage.get('total_tokens', 0)} · 成本 {usage.get('cost', 0):.6f} · "
        f"耗时 {outcome.latency_ms} ms[/dim]"
    )
    return 0 if outcome.succeeded else 1


def _cmd_analyze(args: argparse.Namespace) -> int:
    from . import build_agent

    agent = build_agent(_override_db(args), wait_catalog=True)
    outcome = agent.analyst.analyze(args.question, user_id=args.user)
    if outcome.plan_thought:
        console.print(f"[dim]思路：{escape(outcome.plan_thought)}[/dim]")
    for s in outcome.steps:
        head = f"[{s['no']}] {s['question']}"
        if s.get("result") is None:
            console.print(Panel(Text(s.get("error") or "没有结果"), title=head, border_style="red"))
            continue
        res = s["result"]
        table = Table(*[Text(c) for c in res.columns], title=Text(s.get("predicted_sql") or s.get("sql") or ""))
        for row in res.rows[:8]:
            table.add_row(*[Text("NULL" if v is None else str(v)) for v in row])
        console.print(Panel(table, title=head, border_style="cyan"))
    console.print(Panel(Text(outcome.answer), title="结论", border_style="green" if outcome.succeeded else "yellow"))
    usage = outcome.usage
    console.print(
        f"[dim]状态 {outcome.status} · {len(outcome.steps)} 步 · LLM 调用 {usage.get('llm_calls', 0)} 次 · "
        f"成本 {usage.get('cost', 0):.6f} · 耗时 {outcome.latency_ms} ms"
        + (" · 结论数字对不上出处，已改为列出结果" if outcome.hallucination_blocked else "") + "[/dim]"
    )
    return 0 if outcome.succeeded else 1


def _cmd_schema(args: argparse.Namespace) -> int:
    from .tools.engines import open_database

    settings = _override_db(args)
    db = open_database(settings.db_path)
    if not getattr(args, "catalog", False):
        console.print(Text(db.schema_text()))
        return 0
    # 渐进式披露第 1 层：表目录。顺带报告它和完整定义的体积对比
    from .disclosure import build_catalog
    from .llm import estimate_tokens

    docs = db.schema_by_table()
    columns = {t: [c["name"] for c in cols] for t, cols in db.table_columns().items()}
    catalog = "\n".join(build_catalog(docs, columns).values())
    full = "\n\n".join(docs.values())
    console.print(Text(catalog))
    console.print(
        f"[dim]{len(docs)} 张表 · 表目录约 {estimate_tokens([catalog])} tokens · "
        f"完整定义约 {estimate_tokens([full])} tokens · 目录是完整定义的 {len(catalog) / max(1, len(full)):.0%}[/dim]"
    )
    return 0


def _cmd_demo_db(_args: argparse.Namespace) -> int:
    from .demo_data import build

    path = build()
    console.print(f"演示库已生成: {path}")
    return 0


def _cmd_serve(_args: argparse.Namespace) -> int:
    from .server import main as serve_main

    serve_main()
    return 0


def _memory_store():
    from .config import get_settings
    from .memory import MemoryStore

    return MemoryStore(get_settings().memory_db_path)


def _run_log():
    from .config import get_settings
    from .runlog import RunLog

    settings = get_settings()
    if not settings.run_log_path:
        console.print("[yellow]RUN_LOG_PATH 为空，服务没有记录运行[/yellow]")
        return None
    return RunLog(settings.run_log_path, settings.run_log_keep)


def _cmd_runs(args: argparse.Namespace) -> int:
    log = _run_log()
    if log is None:
        return 1
    s = log.stats(days=args.days)
    rated = s["up"] + s["down"]
    console.print(f"[bold]最近 {s['days']} 天[/bold]：{s['runs']} 次提问（缓存命中 {s['cached']} 次），花费 {s['cost']:.4f}")
    if s["latency_p50_ms"] is not None:
        console.print(f"延迟（不含缓存）：中位数 {s['latency_p50_ms'] / 1000:.1f}s，P95 {s['latency_p95_ms'] / 1000:.1f}s")
    console.print("按结果：" + "，".join(f"{k} {v}" for k, v in s["by_status"].items()) if s["by_status"] else "按结果：无")
    console.print(f"反馈：👍 {s['up']}  👎 {s['down']}" + (f"（差评占 {s['down'] / rated:.0%}）" if rated else ""))
    for c in log.downvoted(limit=args.show)[: args.show]:
        reason = f" — {escape(c['reason'])}" if c["reason"] else ""
        console.print(f"  👎 [{c['created_at']}] {escape(c['question'])}{reason}")
    return 0


def _cmd_feedback_export(args: argparse.Namespace) -> int:
    log = _run_log()
    if log is None:
        return 1
    n = log.export_cases(args.out)
    console.print(f"已导出 {n} 条差评到 {escape(args.out)}：逐条补上 gold_sql 后并入评测集")
    return 0


def _cmd_remember(args: argparse.Namespace) -> int:
    note_id = _memory_store().remember(args.user, args.note)
    console.print(f"已记住（#{note_id}）：{escape(args.note)}")
    return 0


def _cmd_memories(args: argparse.Namespace) -> int:
    rows = _memory_store().notes(args.user)
    if not rows:
        console.print("（暂无记忆）")
        return 0
    flagged = _flagged_memories()
    for note_id, note, created_at in rows:
        line = f"#{note_id} [{created_at}] {escape(note)}"
        if str(note_id) in flagged:
            line += f"  [yellow]（待复核，暂不使用：{escape(flagged[str(note_id)])}）[/yellow]"
        console.print(line)
    return 0


def _flagged_memories() -> dict[str, str]:
    """当前库的档案里待复核的记忆 {id: 原因}；没开建档或档案库打不开时为空。"""
    from .config import get_settings

    settings = get_settings()
    if not settings.catalog_enabled:
        return {}
    try:
        from .catalog import CatalogStore, db_identity
        from .tools.engines import open_database

        store = CatalogStore(settings.catalog_db_path)
        key = db_identity(open_database(settings.db_path))
        return {i.key: i.reason for i in store.items(key, "memory") if i.status == "review"}
    except Exception:  # noqa: BLE001
        return {}


# ---------- 数据库建档 ----------


def _open_catalog(args: argparse.Namespace, describe: bool = False):
    """打开当前库的档案，并把例句、口径、用户记忆接上（复核要用）。describe=True 时接上模型补说明。"""
    from .agent.graph import resolve_allowed_tables
    from .catalog import Catalog
    from .datasets import knowledge_paths
    from .memory import MemoryStore
    from .retrieval import load_examples, load_glossary
    from .tools.engines import open_database

    settings = _override_db(args)
    db = open_database(settings.db_path, timeout_seconds=settings.sql_timeout_seconds, max_rows=settings.sql_max_rows)
    llm = None
    if describe and settings.catalog_describe and settings.llm_api_key and not settings.llm_mock:
        from .llm import LLMClient

        llm = LLMClient(settings)
    catalog = Catalog.from_settings(settings, db, llm=llm)
    glossary_path, examples_path = knowledge_paths(settings)
    catalog.attach(
        memory=MemoryStore(settings.memory_db_path),
        allowed=lambda: resolve_allowed_tables(settings, db),
        glossary=load_glossary(glossary_path),
        examples=load_examples(examples_path),
    )
    return catalog


def _fmt_time(ts) -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def _cmd_catalog_build(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args, describe=not args.no_describe)
    if catalog.llm is None and not args.no_describe:
        console.print("[dim]没有配置 LLM_API_KEY（或关了 CATALOG_DESCRIBE）：只建档，不补中文说明[/dim]")
    report = catalog.sync(force=args.force, wait_lock=120)  # 服务正在同步时等它做完
    for line in report.lines():
        console.print(f"· {escape(line)}")
    st = catalog.status()
    console.print(f"[dim]{st['profiled']}/{st['tables']} 张表已建档 · 用时 {report.seconds}s · 档案库 {escape(str(catalog.store.path))}[/dim]")
    return 0


def _cmd_catalog_status(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args)
    st = catalog.status()
    console.print(f"[bold]建档[/bold]：{st['profiled']}/{st['tables']} 张表，最近一次 {_fmt_time(st['last_profiled_at'])}")
    d = st["descriptions"]
    console.print(f"[bold]说明[/bold]：建表注释 {d['ddl']} 条 · 模型补写（待确认）{d['llm']} 条 · 人工 {d['human']} 条")
    names = {"glossary": "口径", "example": "例句", "memory": "用户记忆"}
    parts = [f"{names[k]} {v['active']} 条有效" + (f"、[yellow]{v['review']} 条待复核[/yellow]" if v["review"] else "") for k, v in st["items"].items()]
    console.print("[bold]依赖索引[/bold]：" + " · ".join(parts))
    if st["last_error"]:
        console.print(f"[yellow]最近的错误[/yellow]：{escape(st['last_error'])}")
    events = catalog.store.events(catalog.key, limit=args.events)
    if events:
        console.print("[bold]最近的变化[/bold]：")
        for at, _kind, detail in events:
            console.print(f"  [{_fmt_time(at)}] {escape(detail)}")
    return 0


def _cmd_catalog_show(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args)
    view = catalog.view()
    if not view.tables:
        console.print("还没有建档：先运行 deepquery catalog build")
        return 1
    if not args.table:
        for name, card in view.tables.items():
            state = "已建档" if card.profile is not None else "建档中"
            note = view.table_note(name)
            console.print(f"[bold]{escape(name)}[/bold]（{len(card.structure.columns)} 列，{state}）{escape(note)}")
        return 0
    real = next((t for t in view.tables if t.lower() == args.table.lower()), None)
    if real is None:
        console.print(f"[yellow]没有这张表：{escape(args.table)}[/yellow]")
        return 1
    card = view.tables[real]
    cols = "、".join(f"{c} {t}".strip() for c, t in card.structure.columns)
    console.print(f"[bold]{escape(real)}[/bold] 列：{escape(cols)}")
    block = view.render(real)
    console.print(Text(block or "（这张表还没建完档，或没有需要特别说明的特征）"))
    return 0


def _cmd_catalog_describe(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args)
    table, _, column = args.target.partition(".")
    view = catalog.view()
    real = next((t for t in view.tables if t.lower() == table.lower()), None)
    if real is None:
        console.print(f"[yellow]没有这张表：{escape(table)}（先运行 deepquery catalog build）[/yellow]")
        return 1
    if column:
        real_col = view.tables[real].structure.real_column(column)
        if real_col is None:
            console.print(f"[yellow]{escape(real)} 没有列 {escape(column)}[/yellow]")
            return 1
        column = real_col
    catalog.set_description(real, column, args.text)
    target = f"{real}.{column}" if column else real
    console.print(f"已{'清除' if not args.text.strip() else '更新'} {escape(target)} 的说明（人工说明优先，之后的建档不会覆盖）")
    return 0


def _cmd_catalog_review(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args)
    items = catalog.review_items()
    if not items:
        console.print("没有待复核的条目")
        return 0
    names = {"glossary": "口径", "example": "例句", "memory": "用户记忆"}
    for item in items:
        who = f"（用户 {item.user_id}）" if item.user_id else ""
        text = item.text if len(item.text) <= 60 else item.text[:59] + "…"
        console.print(f"#{item.id} [{names.get(item.kind, item.kind)}]{escape(who)} {escape(text)}")
        console.print(f"    [yellow]{escape(item.reason)}[/yellow]")
    console.print("[dim]改好源文件（例句 / 口径 jsonl）或记忆后会自动重新登记；确认仍然有效：deepquery catalog confirm <编号>[/dim]")
    return 0


def _cmd_catalog_confirm(args: argparse.Namespace) -> int:
    catalog = _open_catalog(args)
    item = catalog.confirm(args.item_id)
    if item is None:
        console.print("[yellow]没有这个编号[/yellow]")
        return 1
    console.print(f"已确认 #{item.id} 仍然有效，恢复召回")
    return 0


def _cmd_forget(args: argparse.Namespace) -> int:
    ok = _memory_store().forget(args.user, args.note_id)
    console.print("已删除" if ok else "[yellow]未找到该记忆[/yellow]")
    return 0 if ok else 1


def _cmd_check_api(_args: argparse.Namespace) -> int:
    """API 体检：验证第三方/中转接口能支撑本项目的全部依赖点。

    共 4 项：1 配置 2 连通与延迟 3 token 用量回传 4 SQL 围栏格式遵循。
    本项目不使用 function calling，不检查工具调用兼容性。
    """
    from .agent.graph import extract_sql
    from .budget import UsageMeter
    from .config import get_settings
    from .llm import LLMClient, LLMError

    settings = get_settings()
    failed = warned = False

    key = settings.llm_api_key or ""
    masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else ("(未配置)" if not key else "(过短?)")
    console.print("[bold]1/4 配置[/bold]")
    console.print(f"    base_url = {escape(settings.llm_base_url)}")
    console.print(f"    model    = {escape(settings.llm_model)}")
    console.print(f"    api_key  = {masked}")
    if not key:
        console.print("[red]    ✗ LLM_API_KEY 为空，先编辑 .env[/red]")
        return 1

    meter = UsageMeter(
        price_input_per_m=settings.llm_price_input_per_m,
        price_output_per_m=settings.llm_price_output_per_m,
    )
    llm = LLMClient(settings)

    console.print("[bold]2/4 连通与延迟[/bold]")
    try:
        reply = llm.chat(
            [{"role": "user", "content": "只回复两个字：正常"}], meter, tag="check"
        )
        console.print(
            f"    [green]✓[/green] 收到回复（{reply.latency_ms} ms）：{escape(reply.text.strip()[:40])}"
        )
    except LLMError as e:
        console.print(f"[red]    ✗ 调用失败：{escape(str(e))}[/red]")
        console.print("    排查：base_url 是否带 /v1 后缀、key 是否有效、模型名是否是该中转站支持的写法")
        return 1

    console.print("[bold]3/4 token 用量回传[/bold]（影响成本统计的准确性）")
    if meter.unmetered_calls:
        warned = True
        console.print("    [yellow]![/yellow] 上游未返回 usage，已按字符保守估算——成本数字是近似值，评测结论不受影响")
    else:
        console.print(f"    [green]✓[/green] 正常回传（本次 {meter.total_tokens} tokens）")

    console.print("[bold]4/4 SQL 围栏格式遵循[/bold]（本项目靠 ```sql 代码块提取 SQL，不用 function calling）")
    try:
        reply = llm.chat(
            [{
                "role": "user",
                "content": "请只输出一个 ```sql 代码块，内容为：SELECT 1 AS ok。不要输出任何其他内容。",
            }],
            meter,
            tag="check",
        )
        sql = extract_sql(reply.text)
        if sql and sql.lower().lstrip().startswith("select"):
            console.print(f"    [green]✓[/green] 提取成功：{escape(sql[:60])}")
        else:
            failed = True
            console.print(f"    [red]✗ 未能从回复中提取 SQL，原文前 120 字：{escape(reply.text[:120])}[/red]")
            console.print("    该模型不遵循代码块指令，换模型名重试（记下试过的模型与结果）")
    except LLMError as e:
        failed = True
        console.print(f"[red]    ✗ 调用失败：{escape(str(e))}[/red]")

    console.print(
        f"[dim]本次体检消耗 {meter.total_tokens} tokens · 约 {meter.cost:.6f} 元[/dim]"
    )
    if failed:
        console.print("[red]体检未通过：第 4 项失败会直接影响主流程，先解决再跑评测。[/red]")
        return 1
    if warned:
        console.print("[yellow]体检通过（带警告）：可以继续 make smoke。[/yellow]")
    else:
        console.print("[green]体检全部通过：可以跑 make smoke 了。[/green]")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="deepquery", description="企业数据分析 Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="用自然语言提问")
    ask.add_argument("question")
    ask.add_argument("--trace", action="store_true", help="展示每一次尝试的 SQL 与错误")
    ask.add_argument("--no-answer", action="store_true", help="跳过总结节点（只要 SQL 和数据）")
    ask.add_argument("--chart", action="store_true", help="生成图表（模型写代码 → 沙箱执行）")
    ask.add_argument("--user", default="default", help="用户标识（跨会话记忆按用户隔离）")
    ask.add_argument(
        "--db",
        default=None,
        help="连接任意库：SQLite 文件路径，或 mysql:// / postgres:// 连接串（默认 .env 的 DB_PATH）",
    )
    ask.set_defaults(func=_cmd_ask)

    analyze = sub.add_parser("analyze", help='分析模式：拆成几步查询再写带出处的结论，如 analyze "为什么 3 月销售额下降了"')
    analyze.add_argument("question")
    analyze.add_argument("--db", default=None, help="连接任意 SQLite 库文件")
    analyze.add_argument("--user", default="default")
    analyze.set_defaults(func=_cmd_analyze)

    schema = sub.add_parser("schema", help="查看喂给模型的 schema 上下文")
    schema.add_argument("--db", default=None, help="连接任意 SQLite 库文件")
    schema.add_argument("--catalog", action="store_true", help="只看表目录（渐进式披露第 1 层）及体积对比")
    schema.set_defaults(func=_cmd_schema)

    demo = sub.add_parser("demo-db", help="生成演示数据库")
    demo.set_defaults(func=_cmd_demo_db)

    check = sub.add_parser("check-api", help="API 体检：连通/延迟/usage 回传/SQL 围栏遵循（4 项，花费忽略不计）")
    check.set_defaults(func=_cmd_check_api)

    serve = sub.add_parser("serve", help="启动 FastAPI 服务（SSE + 演示网页 + /metrics）")
    serve.set_defaults(func=_cmd_serve)

    remember = sub.add_parser("remember", help='记住口径偏好：remember "销售额一律指已完成订单的成交金额"')
    remember.add_argument("note")
    remember.add_argument("--user", default="default")
    remember.set_defaults(func=_cmd_remember)

    memories = sub.add_parser("memories", help="列出记忆")
    memories.add_argument("--user", default="default")
    memories.set_defaults(func=_cmd_memories)

    forget = sub.add_parser("forget", help="删除一条记忆：forget <id>")
    forget.add_argument("note_id", type=int)
    forget.add_argument("--user", default="default")
    forget.set_defaults(func=_cmd_forget)

    cat = sub.add_parser("catalog", help="数据库建档与记忆同步：build / status / show / describe / review / confirm")
    csub = cat.add_subparsers(dest="catalog_command", required=True)
    c_build = csub.add_parser("build", help="建档 / 同步：只重建结构变了的表，复核受影响的例句、口径和记忆")
    c_build.add_argument("--force", action="store_true", help="所有表重新建档")
    c_build.add_argument("--no-describe", action="store_true", help="不调用模型补写中文说明")
    c_build.set_defaults(func=_cmd_catalog_build)
    c_status = csub.add_parser("status", help="建档进度、说明来源、待复核条目数、最近的变化")
    c_status.add_argument("--events", type=int, default=10)
    c_status.set_defaults(func=_cmd_catalog_status)
    c_show = csub.add_parser("show", help="看档案：不带表名列出全部表，带表名看这张表的档案")
    c_show.add_argument("table", nargs="?")
    c_show.set_defaults(func=_cmd_catalog_show)
    c_desc = csub.add_parser("describe", help='人工写说明：describe orders.status "订单状态，paid 表示已支付"（空字符串表示清除）')
    c_desc.add_argument("target", help="表 或 表.列")
    c_desc.add_argument("text")
    c_desc.set_defaults(func=_cmd_catalog_describe)
    c_review = csub.add_parser("review", help="列出待复核的例句、口径和用户记忆")
    c_review.set_defaults(func=_cmd_catalog_review)
    c_confirm = csub.add_parser("confirm", help="确认一条待复核的条目仍然有效：confirm <编号>")
    c_confirm.add_argument("item_id", type=int)
    c_confirm.set_defaults(func=_cmd_catalog_confirm)
    for p in (c_build, c_status, c_show, c_desc, c_review, c_confirm):
        p.add_argument("--db", default=None, help="SQLite 文件路径，或 mysql:// / postgres:// 连接串（默认 DB_PATH）")

    runs = sub.add_parser("runs", help="运行概况：提问次数、延迟、花费、好评差评，列出最近的差评")
    runs.add_argument("--days", type=int, default=7)
    runs.add_argument("--show", type=int, default=10, help="列出最近几条差评")
    runs.set_defaults(func=_cmd_runs)

    fb = sub.add_parser("feedback-export", help="把差评导出成待标注的评测用例（jsonl）")
    fb.add_argument("--out", default="eval/cases/feedback-todo.jsonl")
    fb.set_defaults(func=_cmd_feedback_export)

    args = parser.parse_args()
    try:
        sys.exit(args.func(args))
    except FileNotFoundError as e:
        console.print(f"[red]错误：[/red]{escape(str(e))}")
        sys.exit(2)
    except Exception as e:
        from .llm import LLMError

        if isinstance(e, LLMError):
            console.print(f"[red]错误：[/red]{escape(str(e))}")
            sys.exit(2)
        raise


if __name__ == "__main__":
    main()
