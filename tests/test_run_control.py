"""运行控制：客户端断开即停止花钱、失败和取消也计入每日花费、并发上限、缓存跨访客共享。"""

import json
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from deepquery.agent import DeepQuery
from deepquery.budget import RunCancelled, RunHandle, UsageMeter
from deepquery.llm import BaseLLM, LLMError, LLMReply, MockLLM, estimate_tokens
from deepquery.memory import MemoryStore
from deepquery.server import create_app

SQL_REPLY = "数订单。\n```sql\nSELECT COUNT(*) FROM orders\n```"
ANSWER = "订单一共有 1500 笔。"


class SlowLLM(BaseLLM):
    """回答阶段逐字慢速输出，模拟真实流式；记录每次调用的 tag。"""

    model_name = "slow"

    def __init__(self, delay: float = 0.03, fail_answer: bool = False):
        self.delay, self.fail_answer = delay, fail_answer
        self.calls: list[str] = []

    def chat(self, messages, meter, tag="", on_delta=None):
        meter.check()
        self.calls.append(tag)
        text = SQL_REPLY if tag == "generate_sql" else ANSWER * 4
        prompt = estimate_tokens(str(m.get("content", "")) for m in messages)
        if tag != "generate_sql" and self.fail_answer:
            meter.add(prompt, 10, tag=tag)
            raise LLMError("上游出错")
        if on_delta:
            for i in range(1, len(text) + 1):
                if meter.cancelled():
                    raise RunCancelled("cancelled")
                time.sleep(self.delay)
                on_delta(text[:i])
        meter.add(prompt, estimate_tokens([text]), tag=tag)
        return LLMReply(text, prompt, 1, 0)


def _settings(settings, **extra):
    return settings.model_copy(update={"llm_price_input_per_m": 1.0, "llm_price_output_per_m": 1.0, **extra})


def _final(resp_text: str) -> dict:
    datas = [line[5:] for line in resp_text.splitlines() if line.startswith("data:")]
    return json.loads(datas[-1])


class TestAgentCancellation:
    def test_cancel_mid_answer_stops_the_run_and_keeps_usage(self, settings, db):
        llm = SlowLLM()
        agent = DeepQuery(settings, db, llm)
        handle = RunHandle()
        deltas = []

        def on_delta(text):
            deltas.append(text)
            if len(deltas) == 3:
                handle.cancel()

        with pytest.raises(RunCancelled):
            for _ in agent.ask_stream("订单数？", on_answer_delta=on_delta, handle=handle):
                pass
        assert llm.calls == ["generate_sql", "answer"]  # 不再有 answer_retry 等后续调用
        assert handle.usage()["llm_calls"] == 1 and handle.usage()["total_tokens"] > 0

    def test_cancelled_meter_refuses_next_call(self):
        meter = UsageMeter()
        meter.cancel_event = threading.Event()
        meter.cancel_event.set()
        with pytest.raises(RunCancelled):
            MockLLM(["x"]).chat([{"role": "user", "content": "hi"}], meter)

    def test_cancel_is_not_swallowed_as_llm_error(self):
        assert not issubclass(RunCancelled, Exception)  # 节点里的 except LLMError/Exception 拦不住它


class TestTokenEstimate:
    def test_chinese_is_not_undercounted(self):
        assert estimate_tokens(["订单一共有多少笔"]) == 8
        assert estimate_tokens(["SELECT 1"]) == 2


class TestServerBilling:
    def test_failed_runs_are_charged_to_the_daily_budget(self, settings, db):
        s = _settings(settings, daily_cost_limit=0.000001)
        app = create_app(agent=DeepQuery(s, db, SlowLLM(fail_answer=True)), settings=s)
        with TestClient(app) as c:
            c.get("/api/ask", params={"question": "订单数？"})
            # 第一次的回答阶段失败了，但已花掉的钱照样计入：再问就提示额度用完
            again = _final(c.get("/api/ask", params={"question": "换个问题？"}).text)
        assert "额度" in again["answer"]

    def test_disconnect_cancels_the_run(self, settings, db):
        llm = SlowLLM(delay=0.05)
        s = _settings(settings, daily_cost_limit=100.0)
        app = create_app(agent=DeepQuery(s, db, llm), settings=s)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        try:
            with httpx.stream("GET", f"{base}/api/ask", params={"question": "订单数？"}, timeout=20) as r:
                for line in r.iter_lines():
                    if line.startswith("event: delta"):
                        break  # 读到第一段回答就断开，相当于用户点了停止
            time.sleep(0.8)
            metrics = httpx.get(f"{base}/metrics").text
        finally:
            server.should_exit = True
        assert llm.calls == ["generate_sql", "answer"]
        assert 'deepquery_requests_total{status="cancelled"} 1.0' in metrics
        cost = next(line for line in metrics.splitlines() if line.startswith("deepquery_llm_cost_total "))
        assert float(cost.split()[-1]) > 0


class TestConcurrencyCap:
    def test_extra_runs_get_a_busy_notice(self, settings, db):
        s = _settings(settings, max_concurrent_runs=1)
        app = create_app(agent=DeepQuery(s, db, SlowLLM(delay=0.02)), settings=s)
        with TestClient(app) as c:
            first: dict = {}
            t = threading.Thread(target=lambda: first.update(_final(c.get("/api/ask", params={"question": "订单数？"}).text)))
            t.start()
            time.sleep(0.3)  # 第一个还在逐字输出
            second = _final(c.get("/api/ask", params={"question": "别的问题？"}).text)
            t.join()
        assert first["status"] == "ok"
        assert "人有点多" in second["answer"]


class TestSharedCache:
    def test_visitors_without_memory_share_answers(self, settings, db):
        llm = SlowLLM(delay=0)
        memory = MemoryStore(settings.memory_db_path)
        app = create_app(agent=DeepQuery(settings, db, llm, memory=memory), settings=settings)
        with TestClient(app) as c:
            a = _final(c.get("/api/ask", params={"question": "订单数？", "user": "v-a"}).text)
            b = _final(c.get("/api/ask", params={"question": "订单数？", "user": "v-b"}).text)
            memory.remember("v-c", "订单数只算已完成订单")
            cc = _final(c.get("/api/ask", params={"question": "订单数？", "user": "v-c"}).text)
        assert not a["cached"] and b["cached"]  # 第二位访客直接命中，不再花钱
        assert not cc["cached"]  # 有私有记忆的访客答案可能不同，不共享
        assert llm.calls.count("generate_sql") == 2


class TestPlainValues:
    def test_driver_types_become_json_friendly(self):
        import datetime
        from decimal import Decimal

        from deepquery.server import _sse
        from deepquery.tools.contract import plain_value

        row = (Decimal("123456.78"), Decimal("1500"), datetime.date(2018, 1, 5),
               datetime.datetime(2018, 1, 5, 9, 30), b"\x01\xff", datetime.timedelta(days=2), None)
        plain = tuple(plain_value(v) for v in row)
        assert plain[:5] == (123456.78, 1500, "2018-01-05", "2018-01-05 09:30:00", "01ff")
        assert plain[6] is None
        _sse("final", {"rows": [plain]})  # 不再抛 TypeError

    def test_decimal_numbers_pass_the_answer_check(self):
        from decimal import Decimal

        from deepquery.tools.contract import QueryResult, plain_value
        from deepquery.verify import check_answer

        result = QueryResult(ok=True, columns=["s"], rows=[(plain_value(Decimal("123456.78")),)], row_count=1)
        violations = check_answer("销售额是 123456.78 元。", result, question="销售额是多少？", sql="SELECT 1")
        assert violations == []  # 转换前 Decimal 单元格不被识别，正确回答会被判成幻觉


class TestSharedStateSafety:
    def test_memory_cache_survives_concurrent_use(self):
        from deepquery.cache import MemoryCache

        cache = MemoryCache(ttl_seconds=0, max_entries=8)  # 立即过期 + 频繁淘汰：最容易撞上竞态
        errors = []

        def worker(n):
            try:
                for i in range(2000):
                    cache.set(f"k{i % 20}", {"n": n})
                    cache.get(f"k{(i + n) % 20}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []

    def test_memory_db_has_a_global_cap(self, settings, db, monkeypatch):
        import deepquery.server as server_mod

        monkeypatch.setattr(server_mod, "MAX_NOTES_TOTAL", 2)
        memory = MemoryStore(settings.memory_db_path)
        app = create_app(agent=DeepQuery(settings, db, MockLLM(["x"]), memory=memory), settings=settings)
        with TestClient(app) as c:
            codes = [c.post("/api/memory", json={"note": f"口径{i}", "user": f"v{i}"}).status_code for i in range(3)]
        assert codes == [200, 200, 429]  # 换访客 ID 也绕不过全库上限
        assert memory.total() == 2

    def test_server_engine_schema_checks_are_throttled(self, settings, db):
        class CountingDb:
            fingerprint_ttl = 60.0

            def __init__(self, inner):
                self._inner, self.checks = inner, 0

            def schema_fingerprint(self):
                self.checks += 1
                return self._inner.schema_fingerprint()

            def __getattr__(self, name):
                return getattr(self._inner, name)

        counting = CountingDb(db)
        agent = DeepQuery(settings, counting, MockLLM(["x"]))
        counting.checks = 0  # 构建时加载快照那一次不算
        agent._fp_checked_at = 0  # 模拟距上次检查已经很久
        for _ in range(5):
            agent.maybe_refresh_schema()
        assert counting.checks == 1


class TestHeartbeat:
    def test_silent_stretches_send_keepalive_comments(self, settings, db, monkeypatch):
        import deepquery.server as server_mod

        class SlowSqlLLM(SlowLLM):
            def chat(self, messages, meter, tag="", on_delta=None):
                if tag == "generate_sql":
                    time.sleep(0.4)  # 模型写 SQL 期间没有任何输出
                return super().chat(messages, meter, tag=tag, on_delta=on_delta)

        monkeypatch.setattr(server_mod, "SSE_HEARTBEAT_SECONDS", 0.05)
        app = create_app(agent=DeepQuery(settings, db, SlowSqlLLM(delay=0)), settings=settings)
        with TestClient(app) as c:
            text = c.get("/api/ask", params={"question": "订单数？"}).text
        assert ": ping" in text  # 保活注释行（浏览器的 EventSource 会忽略）
        assert _final(text)["status"] == "ok"  # 不影响正常的事件与最终结果
