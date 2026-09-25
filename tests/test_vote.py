"""多候选投票：结果一致就采用、不一致按执行结果多数决、平票取温度 0 的那条；默认关闭。"""

import threading

from deepquery.agent import DeepQuery
from deepquery.llm import BaseLLM, LLMReply


def sql(q):
    return f"思路。\n```sql\n{q}\n```"


A = "SELECT COUNT(*) FROM customers WHERE city = '上海'"
A2 = "SELECT COUNT(id) FROM customers WHERE city = '上海'"  # 写法不同、结果相同
B = "SELECT COUNT(*) FROM customers"
C = "SELECT COUNT(*) FROM orders"


class VoteLLM(BaseLLM):
    """第一次写 SQL 返回 first；投票采样（tag=vote）按加锁的队列依次返回。"""

    model_name = "vote-fake"

    def __init__(self, first: str, samples: list[str]):
        self.first, self.samples = first, list(samples)
        self.lock = threading.Lock()
        self.tags: list[str] = []
        self.temperatures: list = []

    def chat(self, messages, meter, tag="", on_delta=None, temperature=None):
        meter.check()
        with self.lock:
            self.tags.append(tag)
            self.temperatures.append(temperature)
            text = self.samples.pop(0) if tag == "vote" else self.first
        meter.add(100, 20, tag=tag)
        return LLMReply(text, 100, 20, latency_ms=0)


def run(settings, db, llm, n=3):
    cfg = settings.model_copy(update={"sql_candidates": n})
    return DeepQuery(cfg, db, llm).ask("上海有多少客户？", generate_answer=False)


class TestVote:
    def test_off_by_default(self, settings, db):
        llm = VoteLLM(sql(A), [])
        outcome = DeepQuery(settings, db, llm).ask("上海有多少客户？", generate_answer=False)
        assert llm.tags == ["generate_sql"] and outcome.vote is None

    def test_agreement_stops_early(self, settings, db):
        llm = VoteLLM(sql(A), [sql(A2), sql(B)])
        outcome = run(settings, db, llm)
        assert llm.tags == ["generate_sql", "vote"]  # 两条结果一致，不再采样
        assert outcome.vote == {"candidates": 2, "agree": 2} and "city" in outcome.final_sql
        assert llm.temperatures == [None, 0.7]

    def test_majority_by_result(self, settings, db):
        llm = VoteLLM(sql(B), [sql(A), sql(A2)])  # 第一条错，另外两条结果相同
        outcome = run(settings, db, llm)
        assert outcome.vote == {"candidates": 3, "agree": 2}
        assert "上海" in outcome.final_sql and outcome.status == "ok"

    def test_tie_keeps_the_first(self, settings, db):
        llm = VoteLLM(sql(A), [sql(B), sql(C)])
        outcome = run(settings, db, llm)
        assert outcome.vote == {"candidates": 3, "agree": 1} and "上海" in outcome.final_sql

    def test_failed_candidates_do_not_vote(self, settings, db):
        bad = "SELECT nope FROM customers"
        llm = VoteLLM(sql(bad), [sql(A), sql(bad)])
        outcome = run(settings, db, llm)
        assert outcome.vote == {"candidates": 3, "agree": 1} and "上海" in outcome.final_sql

    def test_impossible_filter_does_not_win(self, settings, db):
        wrong = "SELECT COUNT(*) FROM customers WHERE city = 'Shanghai'"  # 0，且 Shanghai 不在库里
        llm = VoteLLM(sql(wrong), [sql(A), sql(wrong)])
        outcome = run(settings, db, llm)
        assert "上海" in outcome.final_sql and outcome.result.rows[0][0] > 0

    def test_step_detail_over_sse(self, settings, db):
        import json

        from fastapi.testclient import TestClient

        from deepquery.server import create_app

        cfg = settings.model_copy(update={"sql_candidates": 3})
        llm = VoteLLM(sql(A), [sql(A2)] + ["答案见表。"] * 3)
        with TestClient(create_app(agent=DeepQuery(cfg, db, llm), settings=cfg)) as c:
            text = c.post("/api/ask", json={"question": "上海有多少客户？"}).text
        nodes = [json.loads(b.split("data: ", 1)[1]) for b in text.split("\n\n") if b.startswith("event: node")]
        gen = next(n for n in nodes if n["node"] == "generate_sql")
        assert gen["detail"] == "生成 2 条候选 SQL，2 条执行结果一致，采用多数结果"
