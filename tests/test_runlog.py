"""运行记录与反馈：每次提问一条记录，👍/👎 挂在具体运行上，差评能导出成待标注的评测用例。"""

import json

from fastapi.testclient import TestClient

from deepquery.agent import DeepQuery
from deepquery.llm import MockLLM
from deepquery.runlog import RunLog
from deepquery.server import create_app

SQL_REPLY = "思路。\n```sql\nSELECT COUNT(*) AS n FROM customers\n```"
ANSWER_REPLY = "客户数见下方结果表。"


def payload(**kw):
    base = {"status": "ok", "predicted_sql": "SELECT 1 FROM t", "answer": "好", "row_count": 1,
            "attempts": [{}], "latency_ms": 1200, "usage": {"total_tokens": 900, "cost": 0.001}}
    return {**base, **kw}


class TestRunLog:
    def test_record_feedback_and_export(self, tmp_path):
        log = RunLog(tmp_path / "runs.sqlite")
        a = log.record(user="u1", question="各州客户数？", payload=payload(), history=[{"question": "上一问"}])
        b = log.record(user="u1", question="销售额？", payload=payload(status="failed", latency_ms=3000))
        assert log.feedback(a, "u1", "up")
        assert log.feedback(b, "u1", "down", "数字不对")
        assert not log.feedback(b, "u2", "down")  # 不能给别人的运行打分
        assert not log.feedback("nope", "u1", "up")
        assert log.feedback(a, "u1", "down", "理解错了问题")  # 改主意：覆盖

        s = log.stats()
        assert s["runs"] == 2 and s["up"] == 0 and s["down"] == 2
        assert s["by_status"] == {"ok": 1, "failed": 1} and s["latency_p50_ms"] in (1200, 3000)

        out = tmp_path / "todo.jsonl"
        assert log.export_cases(out) == 2
        cases = [json.loads(line) for line in out.read_text().splitlines() if not line.startswith("#")]
        first = next(c for c in cases if c["question"] == "各州客户数？")
        assert first["gold_sql"] is None and first["predicted_sql"] == "SELECT 1 FROM t"
        assert first["reason"] == "理解错了问题" and first["history"] == [{"question": "上一问"}]

    def test_keeps_only_latest(self, tmp_path):
        log = RunLog(tmp_path / "runs.sqlite", keep=3)
        ids = [log.record(user="u", question=f"q{i}", payload=payload()) for i in range(5)]
        assert log.stats()["runs"] == 3
        assert not log.feedback(ids[0], "u", "up")  # 最旧的已被删掉
        assert log.feedback(ids[-1], "u", "up")


def events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if lines:
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


def final_of(resp):
    return [d for e, d in events(resp.text) if e == "final"][0]


class TestServer:
    def make(self, settings, db):
        agent = DeepQuery(settings, db, MockLLM([SQL_REPLY, ANSWER_REPLY], cycle=True))
        return TestClient(create_app(agent=agent, settings=settings))

    def test_run_id_and_feedback(self, settings, db):
        with self.make(settings, db) as c:
            final = final_of(c.post("/api/ask", json={"question": "客户数？", "user": "v-1"}))
            run_id = final["run_id"]
            assert run_id and final["status"] == "ok"
            ok = c.post("/api/feedback", json={"run_id": run_id, "rating": "down", "reason": "数字不对", "user": "v-1"})
            assert ok.status_code == 200
            other = c.post("/api/feedback", json={"run_id": run_id, "rating": "up", "user": "v-2"})
            assert other.status_code == 404
            assert c.post("/api/feedback", json={"run_id": run_id, "rating": "meh", "user": "v-1"}).status_code == 422
        down = RunLog(settings.run_log_path).downvoted()
        assert down[0]["question"] == "客户数？" and down[0]["reason"] == "数字不对"
        assert "COUNT(*)" in down[0]["predicted_sql"] and "LIMIT" not in down[0]["predicted_sql"].upper()

    def test_cached_answer_gets_its_own_run(self, settings, db):
        with self.make(settings, db) as c:
            first = final_of(c.post("/api/ask", json={"question": "客户数？", "user": "v-1"}))
            again = final_of(c.post("/api/ask", json={"question": "客户数？", "user": "v-2"}))
            assert again["cached"] is True and again["run_id"] != first["run_id"]
            # 命中缓存的访客给的是自己的这次运行打分
            ok = c.post("/api/feedback", json={"run_id": again["run_id"], "rating": "up", "user": "v-2"})
            assert ok.status_code == 200
        stats = RunLog(settings.run_log_path).stats()
        assert stats["runs"] == 2 and stats["cached"] == 1 and stats["up"] == 1

    def test_disabled(self, settings, db):
        off = settings.model_copy(update={"run_log_path": ""})
        agent = DeepQuery(off, db, MockLLM([SQL_REPLY, ANSWER_REPLY], cycle=True))
        with TestClient(create_app(agent=agent, settings=off)) as c:
            final = final_of(c.post("/api/ask", json={"question": "客户数？"}))
            assert "run_id" not in final or final["run_id"] is None
            assert c.post("/api/feedback", json={"run_id": "x" * 32, "rating": "up"}).status_code == 404

    def test_feedback_needs_access_code(self, settings, db):
        locked = settings.model_copy(update={"demo_access_code": "s3cret"})
        agent = DeepQuery(locked, db, MockLLM([SQL_REPLY, ANSWER_REPLY], cycle=True))
        with TestClient(create_app(agent=agent, settings=locked)) as c:
            assert c.post("/api/feedback", json={"run_id": "x" * 32, "rating": "up"}).status_code == 401
