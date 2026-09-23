"""命令行入口。

    deepquery ask "上海的客户一共有多少个？" [--trace] [--no-answer]
    deepquery schema        # 查看喂给模型的 schema 上下文
    deepquery demo-db       # 生成演示库
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

    agent = build_agent(_override_db(args))
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


def _cmd_schema(args: argparse.Namespace) -> int:
    from .tools.engines import open_database

    settings = _override_db(args)
    db = open_database(settings.db_path)
    console.print(db.schema_text())
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


def _cmd_remember(args: argparse.Namespace) -> int:
    note_id = _memory_store().remember(args.user, args.note)
    console.print(f"已记住（#{note_id}）：{escape(args.note)}")
    return 0


def _cmd_memories(args: argparse.Namespace) -> int:
    rows = _memory_store().notes(args.user)
    if not rows:
        console.print("（暂无记忆）")
        return 0
    for note_id, note, created_at in rows:
        console.print(f"#{note_id} [{created_at}] {escape(note)}")
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

    schema = sub.add_parser("schema", help="查看喂给模型的 schema 上下文")
    schema.add_argument("--db", default=None, help="连接任意 SQLite 库文件")
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
