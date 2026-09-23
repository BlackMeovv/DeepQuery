"""FastAPI 服务测试：SSE 流、缓存、指标、图表文件安全。全离线（MockLLM）。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deepquery.agent import DeepQuery
from deepquery.llm import MockLLM
from deepquery.server import create_app


def sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line[len("event: "):] for line in lines if line.startswith("event: "))
        data = next(line[len("data: "):] for line in lines if line.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


@pytest.fixture()
def client(settings, db):
    cnt = db.run_query("SELECT COUNT(*) FROM customers WHERE city = '上海'").rows[0][0]
    llm = MockLLM(
        [
            "思路。\n```sql\nSELECT COUNT(*) FROM customers WHERE city = '上海'\n```",
            f"上海共有 {cnt} 位客户。",
        ],
        cycle=True,
    )
    agent = DeepQuery(settings, db, llm)
    app = create_app(agent=agent, settings=settings)
    with TestClient(app) as c:
        c.agent_llm = llm
        yield c


class TestBasics:
    def test_healthz(self, client):
        body = client.get("/healthz").json()
        assert body["ok"] is True and body["cache"] == "memory"

    def test_index_page(self, client):
        resp = client.get("/")
        assert resp.status_code == 200 and "DeepQuery" in resp.text
        for block in ("数据表", "口径记忆", "查询历史", "运行过程"):  # 三栏控制台关键区块
            assert block in resp.text

    def test_metrics(self, client):
        client.get("/api/ask", params={"question": "上海的客户一共有多少个？"})
        text = client.get("/metrics").text
        assert "deepquery_requests_total" in text and "deepquery_request_seconds" in text


class TestAskStream:
    def test_sse_events_and_final(self, client):
        resp = client.get("/api/ask", params={"question": "上海的客户一共有多少个？"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = sse_events(resp.text)
        node_names = [d["node"] for e, d in events if e == "node"]
        assert node_names[:2] == ["generate_sql", "execute"]
        assert "summarize" in node_names
        final = [d for e, d in events if e == "final"][0]
        assert final["status"] == "ok"
        assert "customers" in final["sql"]
        assert final["rows"] and final["usage"]["llm_calls"] >= 1
        assert "位客户" in final["answer"]

    def test_cache_hit_on_second_request(self, client):
        params = {"question": "上海的客户一共有多少个？"}
        client.get("/api/ask", params=params)
        calls_before = len(client.agent_llm.calls)
        resp = client.get("/api/ask", params=params)
        final = [d for e, d in sse_events(resp.text) if e == "final"][0]
        assert final["cached"] is True
        assert len(client.agent_llm.calls) == calls_before  # 没有新的 LLM 调用

    def test_question_validation(self, client):
        assert client.get("/api/ask", params={"question": ""}).status_code == 422

    def test_fresh_param_bypasses_cache(self, client):
        params = {"question": "缓存旁路测试：上海的客户数？"}
        client.get("/api/ask", params=params)
        calls_before = len(client.agent_llm.calls)
        resp = client.get("/api/ask", params={**params, "fresh": 1})
        final = [d for e, d in sse_events(resp.text) if e == "final"][0]
        assert final["cached"] is False  # 重跑：不读缓存真实执行
        assert len(client.agent_llm.calls) > calls_before

    def test_answer_streams_as_deltas(self, client):
        resp = client.get("/api/ask", params={"question": "流式测试：上海的客户数？"})
        events = sse_events(resp.text)
        deltas = [d["text"] for e, d in events if e == "delta"]
        final = [d for e, d in events if e == "final"][0]
        assert len(deltas) >= 2, "回答应以多个增量事件推送"
        assert deltas[-1] == final["answer"]  # 增量累积文本与最终回答一致
        assert deltas[0] != deltas[-1]


class TestChartFiles:
    def test_path_traversal_rejected(self, client):
        assert client.get("/charts/..%2f..%2fetc%2fpasswd").status_code == 404
        assert client.get("/charts/evil.png").status_code == 404

    def test_symlink_in_chart_dir_not_served(self, client, settings, tmp_path):
        import os

        secret = tmp_path / "secret.env"
        secret.write_text("LLM_API_KEY=sk-leak")
        out = Path(settings.chart_out_dir)
        out.mkdir(parents=True, exist_ok=True)
        os.symlink(secret, out / "chart-0123456789ab.png")
        assert client.get("/charts/chart-0123456789ab.png").status_code == 404

    def test_missing_chart_404(self, client):
        assert client.get("/charts/chart-000000000000.png").status_code == 404


class TestSchemaApi:
    def test_tables_and_columns(self, client):
        data = client.get("/api/schema").json()
        names = {t["name"] for t in data["tables"]}
        assert {"customers", "orders", "payments"} <= names
        customers = next(t for t in data["tables"] if t["name"] == "customers")
        assert {"name": "city", "type": "TEXT"} in customers["columns"]


class TestMemoryApi:
    def test_crud_roundtrip(self, client):
        assert client.get("/api/memory").json()["notes"] == []
        note_id = client.post("/api/memory", json={"note": "销售额=已完成订单成交金额"}).json()["id"]
        notes = client.get("/api/memory").json()["notes"]
        assert len(notes) == 1 and notes[0]["note"].startswith("销售额")
        assert client.delete(f"/api/memory/{note_id}").json()["ok"]
        assert client.get("/api/memory").json()["notes"] == []

    def test_delete_missing_404(self, client):
        assert client.delete("/api/memory/9999").status_code == 404

    def test_validation(self, client):
        assert client.post("/api/memory", json={"note": ""}).status_code == 422


class TestThoughtInStream:
    def test_generate_sql_event_carries_thought(self, client):
        resp = client.get("/api/ask", params={"question": "思路事件测试：上海的客户数？"})
        events = sse_events(resp.text)
        gen = next(d for e, d in events if e == "node" and d["node"] == "generate_sql")
        assert gen.get("thought") == "思路。"


class TestAllowedTablesApi:
    def test_schema_endpoint_filtered(self, settings, db):
        cfg = settings.model_copy(update={"allowed_tables": "customers, orders"})
        llm = MockLLM(["思路。\n```sql\nSELECT COUNT(*) FROM customers\n```"], cycle=True)
        app = create_app(agent=DeepQuery(cfg, db, llm), settings=cfg)
        with TestClient(app) as c:
            names = {t["name"] for t in c.get("/api/schema").json()["tables"]}
        assert names == {"customers", "orders"}


class TestAccessCode:
    """演示部署访问口令：配了 DEMO_ACCESS_CODE 才启用，healthz/schema 始终开放。"""

    @pytest.fixture()
    def guarded(self, settings, db):
        cfg = settings.model_copy(update={"demo_access_code": "s3cret"})
        llm = MockLLM(["思路。\n```sql\nSELECT COUNT(*) FROM customers\n```", "共有客户。"], cycle=True)
        app = create_app(agent=DeepQuery(cfg, db, llm), settings=cfg)
        with TestClient(app) as c:
            yield c

    def test_ask_and_memory_require_code(self, guarded):
        assert guarded.get("/api/ask", params={"question": "客户数？"}).status_code == 401
        assert guarded.get("/api/memory").status_code == 401
        assert guarded.post("/api/memory", json={"note": "x"}).status_code == 401
        assert guarded.delete("/api/memory/1").status_code == 401

    def test_correct_code_passes(self, guarded):
        resp = guarded.get("/api/ask", params={"question": "客户数？", "code": "s3cret"})
        assert resp.status_code == 200
        assert [d for e, d in sse_events(resp.text) if e == "final"]
        assert guarded.get("/api/memory", params={"code": "s3cret"}).status_code == 200

    def test_wrong_code_rejected(self, guarded):
        assert guarded.get("/api/ping", params={"code": "wrong"}).status_code == 401
        assert guarded.get("/api/ping", params={"code": "s3cret"}).json()["ok"] is True

    def test_open_endpoints_stay_open(self, guarded):
        body = guarded.get("/healthz").json()
        assert body["ok"] is True and body["protected"] is True
        assert guarded.get("/api/schema").status_code == 200

    def test_disabled_by_default(self, client):
        assert client.get("/healthz").json()["protected"] is False
        assert client.get("/api/ping").json()["ok"] is True  # 未配置口令=直接放行


class TestWebDistServing:
    def test_dist_served_when_present(self, settings, db, tmp_path):
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html><body>VUE-APP</body></html>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        cfg = settings.model_copy(update={"web_dist": str(dist)})
        from deepquery.agent import DeepQuery
        from deepquery.llm import MockLLM

        app = create_app(agent=DeepQuery(cfg, db, MockLLM(["x"])), settings=cfg)
        with TestClient(app) as c:
            assert "VUE-APP" in c.get("/").text
            assert c.get("/assets/app.js").status_code == 200
            assert "DeepQuery" in c.get("/legacy").text  # 内置页降为后备
