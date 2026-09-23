"""FastAPI 服务：SSE 流式接口 + 单文件演示页 + Prometheus 指标 + 结果缓存。

    deepquery serve            # 或 uvicorn "deepquery.server:create_app" --factory

接口：
    GET  /                     演示网页（无前端框架，单文件）
    GET  /api/ask?question=…&chart=0|1   SSE：逐节点进度 + 最终结果
    GET  /charts/{name}        沙箱生成的图表文件
    GET  /metrics              Prometheus 指标
    GET  /healthz
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from . import datasets
from .agent import DeepQuery, RunOutcome
from .budget import RunCancelled, RunHandle
from .cache import BaseCache, build_cache, cache_key
from .config import Settings, get_settings
from .guard import tables_in_sql
from .ratelimit import DailyBudget, SlidingWindowLimiter

# ---------- Prometheus 指标 ----------

REQUESTS = Counter("deepquery_requests_total", "请求总数（按结果状态）", ["status"])
CACHE_HITS = Counter("deepquery_cache_hits_total", "结果缓存命中数")
HALLUCINATION_BLOCKED = Counter("deepquery_hallucination_blocked_total", "防幻觉拦截次数")
LATENCY = Histogram(
    "deepquery_request_seconds",
    "单次提问端到端延迟",
    buckets=(0.5, 1, 2, 4, 8, 16, 32, 64),
)
TOKENS = Counter("deepquery_llm_tokens_total", "累计 LLM token 消耗")
COST = Counter("deepquery_llm_cost_total", "累计 LLM 成本（按 .env 单价折算）")

_CHART_NAME = re.compile(r"^chart-[0-9a-f]{12}\.png$")
MAX_NOTES_PER_USER = 50  # 单个访客的记忆条数上限，防止公网演示时记忆库被灌满
MAX_NOTES_TOTAL = 20_000  # 全库上限：访客 ID 由客户端生成，只按访客限制挡不住换 ID 刷库
# SSE 心跳间隔（秒）：模型写 SQL、思考回答时可能十几秒没有任何输出，
# 反向代理 / 负载均衡 / CDN 会按空闲超时掐断连接；发一行注释保活（浏览器会忽略注释行）
SSE_HEARTBEAT_SECONDS = 10.0


def memory_scope(agent: DeepQuery, user: str) -> str:
    """用户记忆内容的指纹：记忆会注入提示词、影响答案，所以结果缓存按它区分。"""
    notes = sorted(note for _id, note, _ts in agent.memory.notes(user)) if agent.memory else []
    if not notes:
        return "-"
    return hashlib.sha256("\n".join(notes).encode("utf-8")).hexdigest()[:16]


class MemoryNote(BaseModel):
    # 注意：必须定义在模块顶层——`from __future__ import annotations` 下，
    # 函数内的局部类无法被 FastAPI 的类型解析找到，会被误判成查询参数
    note: str = Field(min_length=1, max_length=500)
    user: str = Field(default="default", max_length=64)

_NODE_LABELS = {
    "generate_sql": "生成 SQL",
    "execute": "守卫校验并执行",
    "repair": "自动修复",
    "chart": "生成图表（沙箱）",
    "summarize": "归纳回答",
    "fallback": "降级收尾",
    "clarify": "需要向你确认",
}


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _node_event(node: str, delta: dict) -> dict:
    payload: dict = {"node": node, "label": _NODE_LABELS.get(node, node)}
    if delta.get("thought"):
        payload["thought"] = delta["thought"]
    if node in ("generate_sql", "repair") and delta.get("candidate_sql"):
        payload["sql"] = delta["candidate_sql"]  # 前端先展示思路、再展示生成的 SQL
    attempts = delta.get("attempts")
    if node == "execute" and attempts:
        last = attempts[-1]
        payload["ok"] = last.ok
        if not last.ok:
            payload["error_kind"] = last.error_kind
            payload["error_message"] = (last.error_message or "")[:300]
    if node == "chart":
        payload["ok"] = bool(delta.get("chart_path"))
        if delta.get("chart_error"):
            payload["error_message"] = delta["chart_error"]
    return payload


def _notice_payload(message: str) -> dict:
    """不调用模型的提示（限流 / 额度用完），形状与正常结果一致，前端按"未完成"展示。"""
    return {
        "status": "failed", "cached": False, "answer": message, "sql": None,
        "predicted_sql": None, "columns": [], "rows": [], "row_count": 0, "attempts": [],
        "selected_tables": None, "context_used": None, "hallucination_blocked": False,
        "chart_url": None, "chart_error": None, "clarification": None,
        "source_tables": [], "numbers_verified": 0,
        "usage": {"llm_calls": 0, "total_tokens": 0, "cost": 0.0}, "latency_ms": 0,
    }


def _outcome_payload(outcome: RunOutcome, cached: bool = False) -> dict:
    result = outcome.result
    return {
        "status": outcome.status,
        "cached": cached,
        "answer": outcome.answer,
        "sql": outcome.final_sql,
        "predicted_sql": outcome.predicted_sql,
        "columns": result.columns if result else [],
        "rows": [list(r) for r in result.rows[:50]] if result else [],
        "row_count": result.row_count if result else 0,
        "attempts": [
            {
                "sql": a.sql_raw,
                "ok": a.ok,
                "error_kind": a.error_kind,
                "error_message": (a.error_message or "")[:300] or None,
            }
            for a in outcome.attempts
        ],
        "selected_tables": outcome.selected_tables,
        "context_used": outcome.context_used,
        "hallucination_blocked": outcome.hallucination_blocked,
        # 回答下方的"出处"：数据来自哪几张表、回答里有几个数字核对过出处
        "source_tables": sorted(tables_in_sql(outcome.final_sql)) if outcome.final_sql else [],
        "numbers_verified": outcome.numbers_verified,
        "chart_url": f"/charts/{Path(outcome.chart_path).name}" if outcome.chart_path else None,
        "chart_error": outcome.chart_error,
        "clarification": outcome.clarification,
        "usage": outcome.usage,
        "latency_ms": outcome.latency_ms,
    }


def create_app(agent: DeepQuery | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    cache: BaseCache = build_cache(settings)
    app = FastAPI(title="deepquery", docs_url=None, redoc_url=None)
    if settings.cors_allow_origins:  # 独立前端（如 Vue3 dev server）联调用
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()],
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type"],
        )
    state = {"agent": agent}
    limiter = SlidingWindowLimiter(settings.rate_limit_per_minute, 60.0)
    budget = DailyBudget(settings.daily_cost_limit)
    run_slots = threading.BoundedSemaphore(max(1, settings.max_concurrent_runs))

    def client_key(request: Request) -> str:
        if settings.trust_proxy_headers:
            # X-Real-IP 由 nginx 用 $remote_addr 覆盖写入，可信；X-Forwarded-For 是追加式的，
            # 前面几段客户端可以伪造，经一层代理时只有最后一段是代理亲眼看到的地址
            ip = request.headers.get("x-real-ip", "").strip()
            if not ip:
                ip = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
            if ip:
                return ip
        return request.client.host if request.client else "unknown"

    agent_lock = threading.Lock()

    def get_agent() -> DeepQuery:
        if state["agent"] is None:  # 惰性构建：测试可注入，生产首个请求时组装
            with agent_lock:  # 并发的首批请求只构建一次
                if state["agent"] is None:
                    from . import build_agent

                    state["agent"] = build_agent(settings)
        return state["agent"]

    def require_code(code: str | None) -> None:
        """演示部署的访问口令（.env 配 DEMO_ACCESS_CODE 即启用；不配=关闭）。

        SSE 的 EventSource 无法携带自定义请求头，所以统一走 `code` 查询参数；
        比较用 compare_digest 防时序侧信道。保护提问与记忆读写；
        healthz/schema/静态页保持开放。
        """
        expected = settings.demo_access_code
        if not expected:
            return
        if not code or not hmac.compare_digest(code, expected):
            raise HTTPException(status_code=401, detail="访问口令缺失或错误")

    @app.get("/api/ping")
    def ping(code: str | None = Query(None, max_length=64)):
        require_code(code)
        return {"ok": True}

    # 内置数据集（演示库 / Olist）自带首页说明与示例问题；DATASET_NOTE 可覆盖说明
    dataset = datasets.for_db(settings.db_path)
    dataset_note = settings.dataset_note or (dataset.description if dataset else "")
    samples = list(dataset.samples) if dataset else []
    dataset_source = dataset.source if dataset else ""

    @app.get("/healthz")
    def healthz():
        db_target = settings.db_path
        if "://" in db_target:  # 连接串脱敏：只露引擎与库名
            scheme, rest = db_target.split("://", 1)
            db_target = f"{scheme}://…/{rest.rsplit('/', 1)[-1]}"
        else:
            db_target = Path(db_target).name
        return {
            "ok": True,
            "cache": cache.backend,
            "mock": settings.llm_mock,
            "db": db_target,
            "model": "mock" if settings.llm_mock else settings.llm_model,
            "protected": bool(settings.demo_access_code),
            "dataset_note": dataset_note,
            "dataset_source": dataset_source,
            "samples": samples,
        }

    @app.get("/metrics")
    def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/schema")
    def api_schema():
        agent_ = get_agent()
        agent_.maybe_refresh_schema()  # 新建/修改的表刷新页面即可见
        visible = agent_.allowed_tables  # 表级权限：前端库表树与 Agent 视野同源
        cols = agent_.db.table_columns()
        return {"tables": [{"name": t, "columns": c} for t, c in cols.items() if t in visible]}

    # ---- 记忆管理（口径偏好）----

    def get_memory():
        agent_ = get_agent()
        if agent_.memory is None:
            from .memory import MemoryStore

            agent_.memory = MemoryStore(settings.memory_db_path)
        return agent_.memory

    @app.get("/api/memory")
    def memory_list(
        user: str = Query("default", max_length=64),
        code: str | None = Query(None, max_length=64),
    ):
        require_code(code)
        return {
            "notes": [
                {"id": i, "note": note, "created_at": ts}
                for i, note, ts in get_memory().notes(user)
            ]
        }

    @app.post("/api/memory")
    def memory_add(request: Request, item: MemoryNote, code: str | None = Query(None, max_length=64)):
        require_code(code)
        if not limiter.hit(client_key(request)):
            raise HTTPException(status_code=429, detail="操作太频繁了，请稍等一分钟再试")
        memory = get_memory()
        if len(memory.notes(item.user)) >= MAX_NOTES_PER_USER:
            raise HTTPException(status_code=429, detail=f"记忆最多保存 {MAX_NOTES_PER_USER} 条，请先删除一些")
        if memory.total() >= MAX_NOTES_TOTAL:
            raise HTTPException(status_code=429, detail="记忆库已满，请联系站点管理员")
        return {"id": memory.remember(item.user, item.note)}

    @app.delete("/api/memory/{note_id}")
    def memory_delete(
        note_id: int,
        user: str = Query("default", max_length=64),
        code: str | None = Query(None, max_length=64),
    ):
        require_code(code)
        if not get_memory().forget(user, note_id):
            raise HTTPException(status_code=404)
        return {"ok": True}

    @app.get("/charts/{name}")
    def chart_file(name: str):
        # 文件名是 48 位随机数（能力 URL），不额外要求口令——<img> 无法携带 code。
        # 防路径穿越只放行沙箱命名；再拒绝符号链接与非普通文件，兜住输出目录被污染的情况
        if not _CHART_NAME.match(name):
            raise HTTPException(status_code=404)
        path = Path(settings.chart_out_dir) / name
        if path.is_symlink() or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/png")

    @app.get("/api/ask")
    def api_ask(
        request: Request,
        question: str = Query(..., min_length=1, max_length=2000),
        chart: bool = Query(False),
        user: str = Query("default", max_length=64),
        fresh: bool = Query(False),  # true=跳过缓存读取强制重跑（结果仍会写入缓存）
        clarify: bool = Query(True),  # 允许 Agent 先向用户确认；回答澄清后的追问传 0，避免反复追问
        code: str | None = Query(None, max_length=64),
    ):
        require_code(code)
        sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        if not limiter.hit(client_key(request)):
            # EventSource 读不到非 200 响应的内容，所以用一条 final 事件把原因告诉用户
            REQUESTS.labels(status="rate_limited").inc()
            notice = _sse("final", _notice_payload("请求太频繁了，请稍等一分钟再试。"))
            return StreamingResponse(iter([notice]), media_type="text/event-stream", headers=sse_headers)
        agent_ = get_agent()
        # 缓存按"记忆内容"而不是访客 ID 区分：没有记忆（或记忆相同）的访客共享同一份答案，
        # 示例问题只需付一次钱；有私有记忆的访客答案可能不同，自然落到各自的键上
        key = cache_key(
            f"{memory_scope(agent_, user)}|{question}",
            db_path=settings.db_path,
            model=agent_.llm.model_name,
            chart=chart,
            schema=agent_.maybe_refresh_schema(),
        )

        async def stream():
            start = time.monotonic()
            cached = None if fresh else cache.get(key)
            if cached is not None:
                CACHE_HITS.inc()
                REQUESTS.labels(status=cached.get("status", "ok")).inc()
                cached_payload = dict(cached)
                cached_payload["cached"] = True
                # 如实报告本次请求的消耗：命中缓存 = 零模型调用、零成本
                cached_payload["usage"] = {"llm_calls": 0, "total_tokens": 0, "cost": 0.0}
                cached_payload["latency_ms"] = int((time.monotonic() - start) * 1000)
                yield _sse("final", cached_payload)
                return
            if budget.exceeded():  # 额度用完后缓存命中仍可用（不花钱），只拦新的模型调用
                REQUESTS.labels(status="quota_exceeded").inc()
                yield _sse("final", _notice_payload("今天的演示额度已经用完了，请明天再来。已问过的问题仍可直接查看。"))
                return
            # 全站同时运行的提问数有上限：小内存服务器上并发过多会拖垮所有人，也会瞬间放大花费
            if not run_slots.acquire(blocking=False):
                REQUESTS.labels(status="busy").inc()
                yield _sse("final", _notice_payload("现在提问的人有点多，请过几秒再试。"))
                return
            # agent 是同步代码，放到工作线程跑；节点事件与回答增量经 asyncio 队列送回这里。
            # 生成器本身是异步的，等待期间不占用服务器线程池
            loop = asyncio.get_running_loop()
            q: asyncio.Queue = asyncio.Queue()
            handle = RunHandle()

            def put(item) -> None:
                try:
                    loop.call_soon_threadsafe(q.put_nowait, item)
                except RuntimeError:  # 事件循环已关闭（服务正在退出）
                    pass

            def pump():
                try:
                    for kind, item, extra in agent_.ask_stream(
                        question,
                        generate_chart=chart,
                        user_id=user,
                        on_answer_delta=lambda text: put(("delta", {"text": text})),
                        allow_clarify=clarify,
                        handle=handle,
                        interactive=True,
                    ):
                        put(("node", _node_event(item, extra or {})) if kind == "node" else ("outcome", item))
                except RunCancelled:
                    REQUESTS.labels(status="cancelled").inc()
                except BaseException as e:  # noqa: BLE001 —— 原样转交给请求协程抛出
                    put(("error", e))
                finally:
                    # 成功、失败、中途取消都按实际用量记账：每日花费上限不能被"开了就关"绕过
                    usage = handle.usage()
                    TOKENS.inc(usage["total_tokens"])
                    COST.inc(usage["cost"])
                    budget.add(usage["cost"])
                    run_slots.release()
                    put(("end", None))

            threading.Thread(target=pump, daemon=True).start()
            outcome: RunOutcome | None = None
            try:
                while True:
                    try:
                        kind, item = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    if kind == "end":
                        break
                    if kind == "error":
                        raise item
                    if kind == "outcome":
                        outcome = item
                        continue
                    yield _sse(kind, item)
            finally:
                # 浏览器断开（点了停止或关掉页面）时协程在 await 处被取消，走到这里：
                # 通知工作线程停下，不再继续调用模型
                handle.cancel()
            assert outcome is not None
            payload = _outcome_payload(outcome)
            REQUESTS.labels(status=outcome.status).inc()
            LATENCY.observe(time.monotonic() - start)
            if outcome.hallucination_blocked:
                HALLUCINATION_BLOCKED.inc()
            if outcome.succeeded:
                cache.set(key, payload)
            yield _sse("final", payload)

        return StreamingResponse(stream(), media_type="text/event-stream", headers=sse_headers)

    web_dist = Path(settings.web_dist)
    if (web_dist / "index.html").exists():
        # Vue 前端构建产物存在：作为主页托管，内置单文件页降为 /legacy 后备
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=str(web_dist / "assets")), name="assets")

        @app.get("/", response_class=HTMLResponse)
        def index():
            return (web_dist / "index.html").read_text(encoding="utf-8")

        @app.get("/legacy", response_class=HTMLResponse)
        def legacy():
            return PAGE

    else:

        @app.get("/", response_class=HTMLResponse)
        def index():
            return PAGE

    return app


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(create_app(settings=settings), host=settings.server_host, port=settings.server_port)


# ---------- 单文件演示页（零前端依赖） ----------

PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepQuery · 数据查询台</title>
<style>
  :root {
    --bg: #f7f8fa; --panel: #ffffff; --text: #1d2129; --muted: #86909c;
    --border: #e5e6eb; --border-strong: #c9cdd4;
    --accent: #165dff; --accent-hover: #0e4bd6;
    --ok: #00b42a; --err: #f53f3f; --warn: #ff7d00;
    --log-bg: #fbfcfd;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #17171a; --panel: #1e1f24; --text: #e6e8ea; --muted: #7d8592;
      --border: #313339; --border-strong: #46484f;
      --accent: #3c7eff; --accent-hover: #5a91ff;
      --ok: #27c346; --err: #f76965; --warn: #ff9626;
      --log-bg: #191a1e;
    }
  }
  :root[data-theme="dark"] {
    --bg: #17171a; --panel: #1e1f24; --text: #e6e8ea; --muted: #7d8592;
    --border: #313339; --border-strong: #46484f;
    --accent: #3c7eff; --accent-hover: #5a91ff;
    --ok: #27c346; --err: #f76965; --warn: #ff9626;
    --log-bg: #191a1e;
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background: var(--bg); color: var(--text); font-size: 13px; line-height: 1.6;
    font-family: -apple-system, "SF Pro Text", "PingFang SC", "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  .mono { font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace; }

  .topbar {
    display: flex; align-items: center; gap: 16px; height: 44px; padding: 0 16px;
    background: var(--panel); border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 10;
  }
  .brand { font-size: 14px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
  .brand::before { content: ""; width: 10px; height: 10px; background: var(--accent); border-radius: 2px; }
  .brand span { color: var(--accent); }
  .brand .sub { color: var(--muted); font-weight: 400; font-size: 12px; }
  .topbar .env {
    margin-left: auto; display: flex; align-items: center; gap: 14px; color: var(--muted);
    font-size: 12px; font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
  }
  .env b { color: var(--text); font-weight: 500; }
  .iconbtn {
    font-size: 12px; padding: 3px 10px; border: 1px solid var(--border-strong); border-radius: 2px;
    background: transparent; color: var(--muted); cursor: pointer;
  }
  .iconbtn:hover { color: var(--accent); border-color: var(--accent); }

  .layout { display: flex; align-items: flex-start; }

  /* 左栏 */
  .sidebar {
    width: 236px; flex-shrink: 0; border-right: 1px solid var(--border); background: var(--panel);
    position: sticky; top: 44px; max-height: calc(100vh - 44px); overflow-y: auto;
  }
  .side-sec { border-bottom: 1px solid var(--border); padding: 10px 12px; }
  .side-title {
    font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .06em;
    color: var(--muted); margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center;
  }
  .tree details { margin-bottom: 2px; }
  .tree summary {
    cursor: pointer; font-size: 12.5px; padding: 2px 4px; border-radius: 2px; user-select: none;
    font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
  }
  .tree summary:hover { background: var(--bg); color: var(--accent); }
  .tree ul { list-style: none; padding: 0 0 4px 18px; }
  .tree li {
    display: flex; justify-content: space-between; gap: 8px; font-size: 12px; padding: 1px 4px;
    font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace; cursor: pointer; border-radius: 2px;
  }
  .tree li:hover { background: var(--bg); color: var(--accent); }
  .tree li .ty { color: var(--muted); font-size: 11px; }
  .memlist { list-style: none; padding: 0; }
  .memlist li {
    display: flex; gap: 6px; align-items: baseline; font-size: 12px; padding: 3px 0;
    border-bottom: 1px dashed var(--border);
  }
  .memlist li:last-child { border-bottom: 0; }
  .memlist .del { color: var(--muted); cursor: pointer; border: 0; background: none; font-size: 12px; padding: 0 2px; }
  .memlist .del:hover { color: var(--err); }
  .memadd { display: flex; gap: 6px; margin-top: 6px; }
  .memadd input {
    flex: 1; min-width: 0; font-size: 12px; padding: 4px 6px; color: var(--text);
    background: var(--bg); border: 1px solid var(--border-strong); border-radius: 2px; outline: none;
  }
  .memadd input:focus { border-color: var(--accent); }
  .histlist { list-style: none; padding: 0; }
  .histlist li {
    font-size: 12px; color: var(--muted); padding: 3px 4px; cursor: pointer; border-radius: 2px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .histlist li:hover { background: var(--bg); color: var(--accent); }
  .side-empty { color: var(--muted); font-size: 12px; }

  /* 中栏 */
  .main { flex: 1; min-width: 0; display: flex; justify-content: center; }
  .maincol { width: 100%; max-width: 940px; min-width: 0; padding: 16px 20px; }

  .querybox { background: var(--panel); border: 1px solid var(--border); border-radius: 3px; }
  .querybox .row { display: flex; gap: 8px; padding: 12px; }
  #q {
    flex: 1; padding: 8px 12px; font-size: 13.5px; color: var(--text); background: var(--bg);
    border: 1px solid var(--border-strong); border-radius: 2px; outline: none;
  }
  #q:focus { border-color: var(--accent); }
  button.primary {
    padding: 7px 18px; font-size: 13px; border: 1px solid var(--accent); border-radius: 2px;
    background: var(--accent); color: #fff; cursor: pointer; white-space: nowrap;
  }
  button.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
  button.danger { border-color: var(--err); background: var(--err); }
  .opt { display: flex; align-items: center; gap: 5px; color: var(--muted); font-size: 12px;
    white-space: nowrap; cursor: pointer; user-select: none; }
  .opt input { accent-color: var(--accent); }
  .samples { border-top: 1px solid var(--border); padding: 6px 10px; color: var(--muted); font-size: 12px; }
  .samples a { color: var(--muted); text-decoration: none; margin-right: 14px; cursor: pointer; }
  .samples a:hover { color: var(--accent); text-decoration: underline; }

  section.block { background: var(--panel); border: 1px solid var(--border); border-radius: 3px; margin-top: 14px; overflow: hidden; }
  .block-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 7px 12px; border-bottom: 1px solid var(--border); font-size: 12px; font-weight: 600;
    background: var(--log-bg); border-radius: 3px 3px 0 0; letter-spacing: .02em;
  }
  .block-head .sub { color: var(--muted); font-weight: 400; }
  pre.sql {
    padding: 10px 12px; overflow-x: auto; font-size: 12.5px; line-height: 1.7; background: var(--log-bg);
    font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
  }
  .ghost {
    font-size: 12px; padding: 2px 8px; border: 1px solid var(--border-strong); border-radius: 2px;
    background: transparent; color: var(--muted); cursor: pointer; margin-left: 8px;
  }
  .ghost:hover { color: var(--accent); border-color: var(--accent); }
  .tblwrap { overflow: auto; max-height: 420px; }
  table { border-collapse: collapse; width: 100%; font-size: 12.5px; }
  th {
    position: sticky; top: 0; background: var(--bg); color: var(--muted); font-weight: 500;
    text-align: left; padding: 6px 12px; border-bottom: 1px solid var(--border-strong); white-space: nowrap;
  }
  td { padding: 5px 12px; border-bottom: 1px solid var(--border); white-space: nowrap;
    font-variant-numeric: tabular-nums; }
  td.num, th.num { text-align: right; font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace; }
  td.idx { color: var(--muted); font-size: 11px; text-align: right; width: 1%; }
  tbody tr:nth-child(even) td { background: color-mix(in srgb, var(--bg) 50%, transparent); }
  tbody tr:hover td { background: color-mix(in srgb, var(--accent) 7%, transparent); }
  td i { color: var(--muted); }
  .answer { padding: 11px 14px; white-space: pre-wrap; font-size: 13.5px; border-left: 2px solid var(--ok); }
  #chartimg { display: block; max-width: 100%; padding: 10px 12px; }

  /* 右栏：运行过程 */
  .runpanel {
    width: 288px; flex-shrink: 0; border-left: 1px solid var(--border); background: var(--panel);
    position: sticky; top: 44px; max-height: calc(100vh - 44px); overflow-y: auto;
  }
  .run-sec { border-bottom: 1px solid var(--border); padding: 10px 12px; }
  #tasks { list-style: none; padding: 0; }
  #tasks li.task { display: flex; gap: 9px; padding: 5px 0; align-items: flex-start; }
  .ticon {
    width: 16px; height: 16px; flex-shrink: 0; margin-top: 3px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 10px; font-weight: 700; color: #fff;
  }
  .task.ok .ticon { background: var(--ok); }
  .task.bad .ticon { background: var(--err); }
  .task.run .ticon { background: transparent; }
  .tlabel { font-size: 12.5px; }
  .task.bad .tlabel { color: var(--err); }
  .tthought {
    font-size: 12px; color: var(--muted); margin-top: 3px; padding: 4px 8px;
    background: var(--log-bg); border-left: 2px solid var(--border-strong); border-radius: 0 2px 2px 0;
  }
  .terr { font-size: 12px; color: var(--err); margin-top: 2px; }
  .spinner {
    width: 12px; height: 12px; border: 2px solid var(--border-strong); border-top-color: var(--accent);
    border-radius: 50%; display: inline-block; animation: spin .7s linear infinite; margin-top: 5px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  details.attempts summary { font-size: 12px; color: var(--muted); cursor: pointer; padding: 2px 0; }
  .attempt { border-top: 1px dashed var(--border); padding: 6px 0; }
  .attempt .meta { font-size: 11.5px; margin-bottom: 2px; }
  .attempt .meta .bad { color: var(--err); }
  .attempt .meta .good { color: var(--ok); }
  .attempt pre {
    font-size: 11.5px; overflow-x: auto; color: var(--muted);
    font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
  }
  #runstats { font-size: 12px; font-family: "SF Mono", ui-monospace, Menlo, Consolas, monospace; }
  #runstats div { display: flex; justify-content: space-between; padding: 2px 0; color: var(--muted); }
  #runstats b { color: var(--text); font-weight: 500; }
  #runstats .flag-warn { color: var(--warn); }
  .run-empty { color: var(--muted); font-size: 12px; }
  .sidebar::-webkit-scrollbar, .runpanel::-webkit-scrollbar, .tblwrap::-webkit-scrollbar,
  pre.sql::-webkit-scrollbar { width: 8px; height: 8px; }
  .sidebar::-webkit-scrollbar-thumb, .runpanel::-webkit-scrollbar-thumb,
  .tblwrap::-webkit-scrollbar-thumb, pre.sql::-webkit-scrollbar-thumb {
    background: var(--border-strong); border-radius: 4px;
  }
  .hidden { display: none !important; }
  @media (max-width: 1180px) { .sidebar { display: none; } }
  @media (max-width: 860px) { .runpanel { display: none; } }
</style>
</head>
<body>
<div class="topbar">
  <div class="brand">Deep<span>Query</span> <span class="sub">数据查询台</span></div>
  <div class="env">
    <span id="envinfo"></span>
    <button class="iconbtn" id="theme" title="切换主题">主题</button>
  </div>
</div>

<div class="layout">
  <aside class="sidebar">
    <div class="side-sec">
      <div class="side-title">数据表</div>
      <div class="tree" id="tree"><span class="side-empty">加载中…</span></div>
    </div>
    <div class="side-sec">
      <div class="side-title">口径记忆</div>
      <ul class="memlist" id="memlist"></ul>
      <div class="memadd">
        <input id="memnote" placeholder="如：销售额=已完成订单成交金额" maxlength="200">
        <button class="iconbtn" id="memaddbtn">记住</button>
      </div>
    </div>
    <div class="side-sec">
      <div class="side-title">查询历史 <button class="iconbtn" id="histclear">清空</button></div>
      <ul class="histlist" id="histlist"></ul>
    </div>
  </aside>

  <div class="main"><div class="maincol">
    <div class="querybox">
      <div class="row">
        <input id="q" placeholder="输入业务问题，例如：支付总金额最高的前3个城市是哪几个？" autocomplete="off">
        <label class="opt"><input type="checkbox" id="chart">生成图表</label>
        <button class="primary" id="go">查 询</button>
        <button class="primary danger hidden" id="stop">停 止</button>
      </div>
      <div class="samples" id="samples">示例：</div>
    </div>

    <section class="block" id="welcome">
      <div class="block-head">开始之前</div>
      <div class="answer" style="border-left:2px solid var(--accent); color: var(--muted); font-size: 13px">
        用自然语言查询左侧的数据库。系统会检索相关表结构、生成 SQL、在只读守卫下执行，
        失败会自动修复；回答里的每个数字都经过与查询结果的比对校验。
        右侧面板会实时展示每一步的执行过程与模型思路。
      </div>
    </section>

    <section class="block hidden" id="sqlblock">
      <div class="block-head"><span>SQL <span class="sub" id="sqlnote"></span></span><button class="ghost" id="copy">复制</button></div>
      <pre class="sql"><code id="sql"></code></pre>
    </section>

    <section class="block hidden" id="datablock">
      <div class="block-head">
        <span>查询结果 <span class="sub" id="rowcount"></span></span>
        <span><button class="ghost" id="csv">导出 CSV</button></span>
      </div>
      <div class="tblwrap"><table id="tbl"></table></div>
    </section>

    <section class="block hidden" id="ansblock">
      <div class="block-head">回答</div>
      <div class="answer" id="answer"></div>
    </section>

    <section class="block hidden" id="chartblock">
      <div class="block-head">图表</div>
      <img id="chartimg" alt="chart">
    </section>
  </div></div>

  <aside class="runpanel">
    <div class="run-sec">
      <div class="side-title">运行过程</div>
      <ol id="tasks"><li class="run-empty">提交一个问题后，这里实时显示 agent 的每一步与思路。</li></ol>
    </div>
    <div class="run-sec hidden" id="attsec">
      <div class="side-title">尝试详情</div>
      <details class="attempts" id="attempts" open>
        <summary id="attsummary"></summary>
        <div id="attbody"></div>
      </details>
    </div>
    <div class="run-sec hidden" id="statsec">
      <div class="side-title">本次消耗</div>
      <div id="runstats"></div>
    </div>
  </aside>
</div>

<script>
const $ = (id) => document.getElementById(id);
const EXAMPLES = [
  "支付总金额最高的前3个城市是哪几个？",
  "各品类的成交金额分别是多少？",
  "下单次数最多的前5名客户是谁？",
  "2025年上半年每个月的支付总金额是多少？",
];
const NL = String.fromCharCode(10);
let es = null, lastData = null;

function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }

/* ---------- 主题 ---------- */
function applyTheme(mode) {
  if (mode === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = mode;
  $("theme").textContent = { auto: "主题:自动", light: "主题:亮", dark: "主题:暗" }[mode];
}
let themeMode = localStorage.getItem("ia_theme") || "auto";
applyTheme(themeMode);
$("theme").onclick = () => {
  themeMode = { auto: "light", light: "dark", dark: "auto" }[themeMode];
  localStorage.setItem("ia_theme", themeMode);
  applyTheme(themeMode);
};

/* ---------- 环境信息 ---------- */
fetch("/healthz").then(r => r.json()).then(h => {
  $("envinfo").innerHTML =
    `db <b>${esc(h.db || "-")}</b>&nbsp; model <b>${esc(h.model || "-")}</b>&nbsp; cache <b>${esc(h.cache)}</b>` +
    (h.mock ? '&nbsp;<span style="color:var(--warn)">MOCK</span>' : "");
}).catch(() => {});

/* ---------- 库表结构树 ---------- */
fetch("/api/schema").then(r => r.json()).then(d => {
  const tree = $("tree"); tree.innerHTML = "";
  d.tables.forEach(t => {
    const det = document.createElement("details");
    const sum = document.createElement("summary");
    sum.textContent = `${t.name} (${t.columns.length})`;
    sum.title = "双击插入表名"; sum.ondblclick = () => insertToken(t.name);
    det.appendChild(sum);
    const ul = document.createElement("ul");
    t.columns.forEach(c => {
      const li = document.createElement("li");
      li.innerHTML = `<span>${esc(c.name)}</span><span class="ty">${esc(c.type)}</span>`;
      li.title = "点击插入列名"; li.onclick = () => insertToken(c.name);
      ul.appendChild(li);
    });
    det.appendChild(ul);
    tree.appendChild(det);
  });
}).catch(() => { $("tree").innerHTML = '<span class="side-empty">schema 加载失败</span>'; });

function insertToken(token) {
  const input = $("q");
  input.value = input.value ? input.value + " " + token : token;
  input.focus();
}

/* ---------- 口径记忆 ---------- */
function loadMemory() {
  fetch("/api/memory").then(r => r.json()).then(d => {
    const ul = $("memlist"); ul.innerHTML = "";
    if (!d.notes.length) { ul.innerHTML = '<li class="side-empty" style="border:0">（无，在下方添加）</li>'; return; }
    d.notes.forEach(n => {
      const li = document.createElement("li");
      li.innerHTML = `<button class="del" title="删除">×</button><span>${esc(n.note)}</span>`;
      li.querySelector(".del").onclick = () => {
        fetch(`/api/memory/${n.id}`, { method: "DELETE" }).then(loadMemory);
      };
      ul.appendChild(li);
    });
  }).catch(() => {});
}
$("memaddbtn").onclick = () => {
  const note = $("memnote").value.trim();
  if (!note) return;
  fetch("/api/memory", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  }).then(() => { $("memnote").value = ""; loadMemory(); });
};
$("memnote").addEventListener("keydown", e => { if (e.key === "Enter") $("memaddbtn").click(); });
loadMemory();

/* ---------- 查询历史 ---------- */
function history() { try { return JSON.parse(localStorage.getItem("ia_history") || "[]"); } catch { return []; } }
function renderHistory() {
  const ul = $("histlist"); ul.innerHTML = "";
  const items = history();
  if (!items.length) { ul.innerHTML = '<li class="side-empty" style="cursor:default">（空）</li>'; return; }
  items.slice(0, 15).forEach(h => {
    const li = document.createElement("li");
    li.textContent = h.q; li.title = h.q;
    li.onclick = () => { $("q").value = h.q; $("chart").checked = !!h.chart; run(); };
    ul.appendChild(li);
  });
}
function pushHistory(q, chart) {
  const items = history().filter(h => h.q !== q);
  items.unshift({ q, chart, ts: Date.now() });
  try { localStorage.setItem("ia_history", JSON.stringify(items.slice(0, 50))); } catch {}
  renderHistory();
}
$("histclear").onclick = () => { localStorage.removeItem("ia_history"); renderHistory(); };
renderHistory();

/* ---------- 示例 ---------- */
EXAMPLES.forEach(text => {
  const a = document.createElement("a");
  a.textContent = text;
  a.onclick = () => { $("q").value = text; run(); };
  $("samples").appendChild(a);
});

/* ---------- CSV 导出 ---------- */
$("csv").onclick = () => {
  if (!lastData) return;
  const quote = (v) => '"' + String(v === null ? "" : v).replaceAll('"', '""') + '"';
  const lines = [lastData.columns.map(quote).join(",")];
  lastData.rows.forEach(r => lines.push(r.map(quote).join(",")));
  const blob = new Blob(["﻿" + lines.join(NL)], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "query-result.csv";
  a.click();
  URL.revokeObjectURL(a.href);
};

/* ---------- 运行过程面板 ---------- */
function taskRow(state, label, thought, err) {
  const li = document.createElement("li");
  li.className = "task " + state;
  const icon = state === "run" ? '<span class="spinner"></span>' : (state === "bad" ? "✕" : "✓");
  let body = `<div><div class="tlabel">${esc(label)}</div>`;
  if (thought) body += `<div class="tthought">${esc(thought)}</div>`;
  if (err) body += `<div class="terr">${esc(err)}</div>`;
  body += "</div>";
  li.innerHTML = `<span class="ticon">${icon}</span>${body}`;
  return li;
}

$("go").onclick = run;
$("stop").onclick = () => {
  if (es) es.close();
  $("tasks").appendChild(taskRow("bad", "已手动停止"));
  running(false);
};
$("q").addEventListener("keydown", e => { if (e.key === "Enter") run(); });
$("copy").onclick = () => {
  navigator.clipboard.writeText($("sql").textContent).then(() => {
    $("copy").textContent = "已复制"; setTimeout(() => $("copy").textContent = "复制", 1200);
  });
};

function isNumCol(rows, i) {
  return rows.length > 0 && rows.every(r => r[i] === null || typeof r[i] === "number");
}
function running(on) {
  $("go").classList.toggle("hidden", on);
  $("stop").classList.toggle("hidden", !on);
}

function run() {
  const q = $("q").value.trim();
  if (!q) return;
  if (es) es.close();
  ["welcome", "sqlblock", "datablock", "ansblock", "chartblock", "attsec", "statsec"].forEach(id => $(id).classList.add("hidden"));
  $("tasks").innerHTML = "";
  running(true);
  pushHistory(q, $("chart").checked);
  const pending = taskRow("run", "思考中…");
  $("tasks").appendChild(pending);

  es = new EventSource(`/api/ask?question=${encodeURIComponent(q)}&chart=${$("chart").checked ? 1 : 0}`);

  es.addEventListener("node", (e) => {
    const d = JSON.parse(e.data);
    const bad = d.ok === false;
    const err = bad ? `${d.error_kind || ""}${d.error_message ? "：" + d.error_message : ""}` : "";
    $("tasks").insertBefore(taskRow(bad ? "bad" : "ok", d.label, d.thought, err), pending);
  });

  es.addEventListener("final", (e) => {
    const d = JSON.parse(e.data);
    pending.remove();
    $("tasks").appendChild(taskRow(
      d.status.startsWith("ok") ? "ok" : "bad",
      d.status.startsWith("ok") ? "完成" : "未能得到可靠结果",
      d.cached ? "缓存命中：直接返回上次结果，未消耗模型调用" : ""
    ));

    if (d.sql) {
      $("sql").textContent = d.sql;
      $("sqlnote").textContent = d.cached ? "（缓存命中）" : "";
      $("sqlblock").classList.remove("hidden");
    }
    if (d.attempts && d.attempts.length > 1) {
      $("attsummary").textContent = `共 ${d.attempts.length} 次尝试（含自动修复）`;
      $("attbody").innerHTML = d.attempts.map((a, i) => {
        const state = a.ok
          ? '<span class="good">成功</span>'
          : `<span class="bad">${esc(a.error_kind || "失败")}${a.error_message ? "：" + esc(a.error_message) : ""}</span>`;
        return `<div class="attempt"><div class="meta mono">#${i + 1} ${state}</div><pre>${esc(a.sql)}</pre></div>`;
      }).join("");
      $("attsec").classList.remove("hidden");
    }
    if (d.columns && d.columns.length) {
      lastData = { columns: d.columns, rows: d.rows };
      const numCols = d.columns.map((_, i) => isNumCol(d.rows, i));
      let html = "<thead><tr><th class='idx'>#</th>" +
        d.columns.map((c, i) => `<th${numCols[i] ? " class='num'" : ""}>${esc(c)}</th>`).join("") + "</tr></thead><tbody>";
      html += d.rows.map((r, ri) => `<tr><td class="idx">${ri + 1}</td>` +
        r.map((v, i) => `<td${numCols[i] ? " class='num'" : ""}>${v === null ? "<i>NULL</i>" : esc(v)}</td>`).join("") + "</tr>").join("");
      $("tbl").innerHTML = html + "</tbody>";
      $("rowcount").textContent = d.row_count > d.rows.length
        ? `共 ${d.row_count} 行，展示前 ${d.rows.length} 行` : `${d.row_count} 行`;
      $("datablock").classList.remove("hidden");
    }
    if (d.answer) { $("answer").textContent = d.answer; $("ansblock").classList.remove("hidden"); }
    if (d.chart_url) { $("chartimg").src = d.chart_url; $("chartblock").classList.remove("hidden"); }

    const u = d.usage || {};
    const rows = [
      ["status", d.status], ["llm_calls", u.llm_calls ?? 0], ["tokens", u.total_tokens ?? 0],
      ["cost", (u.cost ?? 0).toFixed(6)], ["latency", d.latency_ms + "ms"],
      ["cache", d.cached ? "hit" : "miss"],
    ];
    if (d.selected_tables) rows.push(["rag_tables", d.selected_tables.join(",")]);
    let stats = rows.map(([k, v]) => `<div><span>${k}</span><b>${esc(v)}</b></div>`).join("");
    if (d.hallucination_blocked) stats += '<div class="flag-warn"><span>hallucination</span><b class="flag-warn">blocked</b></div>';
    if (d.chart_error) stats += `<div class="flag-warn"><span>chart</span><b class="flag-warn">${esc(d.chart_error)}</b></div>`;
    $("runstats").innerHTML = stats;
    $("statsec").classList.remove("hidden");
    es.close(); running(false);
  });

  es.onerror = () => {
    pending.remove();
    $("tasks").appendChild(taskRow("bad", "连接中断"));
    es.close(); running(false);
  };
}

const params = new URLSearchParams(location.search);
if (params.get("theme")) applyTheme(params.get("theme"));
if (params.get("q")) { $("q").value = params.get("q"); if (params.get("chart") === "1") $("chart").checked = true; run(); }
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
