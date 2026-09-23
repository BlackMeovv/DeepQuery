"""问口径 / 表结构的问题不查数据直接回答；追问时带上之前几轮对话。"""

import json

from fastapi.testclient import TestClient

from deepquery.agent import DeepQuery
from deepquery.agent import prompts
from deepquery.agent.graph import extract_meta_answer
from deepquery.llm import MockLLM
from deepquery.server import create_app

META_REPLY = """这是在问上一轮查询用了哪些表和字段，看 SQL 就能回答。
```answer
**客户数**只用了 customers 表，按 customers.city 分组计数。
```"""
META_WITH_NUMBER = "看表结构回答。\n```answer\n上海一共有 98765 个客户。\n```"
SQL_REPLY = "按城市统计客户数。\n```sql\nSELECT city, COUNT(*) AS n FROM customers GROUP BY city\n```"
ANSWER_REPLY = "上海的客户最多。"

PREV_TURN = {
    "question": "各城市有多少客户？",
    "sql": "SELECT city, COUNT(*) AS n FROM customers GROUP BY city LIMIT 200",
    "answer": "上海的客户最多。",
}


class TestExtractMetaAnswer:
    def test_answer_block_is_plain_text(self):
        assert extract_meta_answer(META_REPLY) == "客户数只用了 customers 表，按 customers.city 分组计数。"

    def test_sql_block_wins(self):
        # 同时给了 SQL：能查就查，按查数据处理
        assert extract_meta_answer(META_REPLY + "\n```sql\nSELECT 1 FROM customers\n```") is None

    def test_no_answer_block(self):
        assert extract_meta_answer(SQL_REPLY) is None
        assert extract_meta_answer("```answer\n\n```") is None


class TestMetaQuestion:
    def test_answered_from_schema_without_query(self, settings, db):
        llm = MockLLM([META_REPLY])
        outcome = DeepQuery(settings, db, llm).ask("这个指标用了哪些字段？", interactive=True)
        assert outcome.status == "ok_meta" and outcome.succeeded
        assert outcome.answer.startswith("客户数只用了 customers 表")
        assert outcome.final_sql is None and outcome.result is None and outcome.attempts == []
        assert outcome.usage["llm_calls"] == 1
        assert "不用查数据的问题" in llm.calls[0][0]["content"]

    def test_unsourced_number_falls_back_to_sql(self, settings, db):
        # 没查数据却报了数：退回去写 SQL，结果以查询为准
        llm = MockLLM([META_WITH_NUMBER, SQL_REPLY, ANSWER_REPLY])
        outcome = DeepQuery(settings, db, llm).ask("上海有多少客户？", interactive=True)
        assert outcome.status == "ok" and outcome.final_sql
        nudge = llm.calls[1][-1]["content"]
        assert "98765" in nudge and "写 SQL" in nudge

    def test_numbers_from_context_are_allowed(self, settings, db):
        # 对话上下文、口径里写着的数字（如上一轮 SQL 的 LIMIT 200）不是"从数据里得出的数值"，可以说
        reply = "看上一轮的 SQL。\n```answer\n上一轮按 customers.city 分组，最多返回 200 行。\n```"
        agent = DeepQuery(settings, db, MockLLM([reply]))
        outcome = agent.ask("上一轮是怎么查的？", interactive=True, history=[PREV_TURN])
        assert outcome.status == "ok_meta"

    def test_eval_mode_never_uses_meta_rules(self, settings, db):
        llm = MockLLM([SQL_REPLY])
        DeepQuery(settings, db, llm).ask("客户数？", generate_answer=False)
        assert llm.calls[0][0]["content"] == prompts.sql_system("sqlite")

    def test_repair_round_has_no_meta_rules(self, settings, db):
        bad = "思路。\n```sql\nSELECT nope FROM customers\n```"
        llm = MockLLM([bad, SQL_REPLY, ANSWER_REPLY])
        outcome = DeepQuery(settings, db, llm).ask("客户数？", interactive=True)
        assert outcome.status == "ok"
        assert "不用查数据的问题" not in llm.calls[1][0]["content"]

    def test_constant_sql_is_not_a_query_result(self, settings, db):
        # 模型用 SELECT 常量拼"结果"：守卫拦下，进修复，而不是标成"查询结果 N 行"
        const = "列出指标。\n```sql\nSELECT '客户数' AS 指标, 'customers' AS 表\n```"
        llm = MockLLM([const, SQL_REPLY, ANSWER_REPLY])
        outcome = DeepQuery(settings, db, llm).ask("客户数？", interactive=True)
        assert outcome.attempts[0].error_kind == "guard_rejected"
        assert outcome.status == "ok" and "customers" in outcome.final_sql.lower()


class TestHistory:
    def test_history_goes_into_sql_prompt(self, settings, db):
        llm = MockLLM([SQL_REPLY, ANSWER_REPLY])
        DeepQuery(settings, db, llm).ask("那按会员等级呢？", interactive=True, history=[PREV_TURN])
        user_msg = llm.calls[0][1]["content"]
        assert "之前的对话" in user_msg and "各城市有多少客户？" in user_msg
        assert PREV_TURN["sql"] in user_msg and "上海的客户最多" in user_msg
        assert user_msg.rstrip().endswith("用户问题：那按会员等级呢？")

    def test_no_history_keeps_original_prompt(self, settings, db):
        llm = MockLLM([SQL_REPLY])
        agent = DeepQuery(settings, db, llm)
        agent.ask("客户数？", generate_answer=False)
        assert "之前的对话" not in llm.calls[0][1]["content"]
        assert llm.calls[0][1]["content"].endswith("用户问题：客户数？")

    def test_repair_keeps_history(self, settings, db):
        bad = "思路。\n```sql\nSELECT nope FROM customers\n```"
        llm = MockLLM([bad, SQL_REPLY, ANSWER_REPLY])
        DeepQuery(settings, db, llm).ask("那按会员等级呢？", interactive=True, history=[PREV_TURN])
        assert "各城市有多少客户？" in llm.calls[1][1]["content"]

    def test_retrieval_uses_previous_question(self, settings, db):
        # "那按城市拆开呢"本身不含口径词：检索口径时要带上上一问
        prev = {"question": "沉默客户有多少个？", "sql": "", "answer": ""}
        agent = DeepQuery(settings, db, MockLLM([SQL_REPLY, ANSWER_REPLY]))
        alone = agent.ask("那按城市拆开呢？", interactive=True)
        follow = agent.ask("那按城市拆开呢？", interactive=True, history=[prev])
        assert "沉默客户" not in alone.context_used["glossary"]
        assert "沉默客户" in follow.context_used["glossary"]


def sse_events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if not lines:
            continue
        ev = next(line[7:] for line in lines if line.startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        out.append((ev, json.loads(data)))
    return out


def final_of(resp):
    return [d for e, d in sse_events(resp.text) if e == "final"][0]


class TestServerFollowUp:
    def make(self, settings, db, replies):
        llm = MockLLM(replies, cycle=True)
        return TestClient(create_app(agent=DeepQuery(settings, db, llm), settings=settings)), llm

    def test_post_with_history(self, settings, db):
        c, llm = self.make(settings, db, [SQL_REPLY, ANSWER_REPLY])
        with c:
            resp = c.post("/api/ask", json={"question": "那按会员等级呢？", "history": [PREV_TURN]})
            assert resp.status_code == 200
            assert final_of(resp)["status"] == "ok"
            assert "各城市有多少客户？" in llm.calls[0][1]["content"]

    def test_cache_is_keyed_by_history(self, settings, db):
        c, llm = self.make(settings, db, [SQL_REPLY, ANSWER_REPLY])
        with c:
            q = {"question": "那按会员等级呢？"}
            assert final_of(c.post("/api/ask", json={**q, "history": [PREV_TURN]}))["cached"] is False
            # 同一句追问接在另一个上一问后面，答案可能不同：不能命中
            other = {**PREV_TURN, "question": "各品类卖了多少？"}
            assert final_of(c.post("/api/ask", json={**q, "history": [other]}))["cached"] is False
            assert final_of(c.post("/api/ask", json={**q}))["cached"] is False
            assert final_of(c.post("/api/ask", json={**q, "history": [PREV_TURN]}))["cached"] is True

    def test_get_and_post_without_history_share_cache(self, settings, db):
        c, _ = self.make(settings, db, [SQL_REPLY, ANSWER_REPLY])
        with c:
            assert final_of(c.get("/api/ask", params={"question": "客户数？"}))["cached"] is False
            assert final_of(c.post("/api/ask", json={"question": "客户数？"}))["cached"] is True

    def test_meta_payload(self, settings, db):
        c, _ = self.make(settings, db, [META_REPLY])
        with c:
            resp = c.post("/api/ask", json={"question": "这些用了哪些字段？", "history": [PREV_TURN]})
            events = sse_events(resp.text)
            assert [d["node"] for e, d in events if e == "node"] == ["generate_sql", "explain"]
            final = final_of(resp)
            assert final["status"] == "ok_meta"
            assert final["sql"] is None and final["source_tables"] == [] and final["row_count"] == 0
            again = c.post("/api/ask", json={"question": "这些用了哪些字段？", "history": [PREV_TURN]})
            assert final_of(again)["cached"] is True

    def test_history_limits(self, settings, db):
        c, _ = self.make(settings, db, [SQL_REPLY, ANSWER_REPLY])
        with c:
            too_many = {"question": "客户数？", "history": [PREV_TURN] * 4}
            assert c.post("/api/ask", json=too_many).status_code == 422
            too_long = {"question": "客户数？", "history": [{**PREV_TURN, "sql": "x" * 4001}]}
            assert c.post("/api/ask", json=too_long).status_code == 422

    def test_post_requires_access_code_in_body(self, settings, db):
        settings = settings.model_copy(update={"demo_access_code": "s3cret"})
        c, _ = self.make(settings, db, [SQL_REPLY, ANSWER_REPLY])
        with c:
            assert c.post("/api/ask", json={"question": "客户数？"}).status_code == 401
            ok = c.post("/api/ask", json={"question": "客户数？", "code": "s3cret"})
            assert ok.status_code == 200 and final_of(ok)["status"] == "ok"
