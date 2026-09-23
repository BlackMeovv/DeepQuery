"""打招呼、问"你是谁 / 能做什么"：直接回应，不查数据、不反问。"""

import json

from fastapi.testclient import TestClient

from deepquery import smalltalk
from deepquery.agent import DeepQuery
from deepquery.llm import MockLLM
from deepquery.server import create_app

SQL_REPLY = "思路。\n```sql\nSELECT COUNT(*) FROM customers\n```"


class TestKind:
    def test_small_talk(self):
        for q in ("请你先介绍一下你自己吧", "你好", "您好！", "你好，你是谁？", "介绍下你自己",
                  "自我介绍一下", "你能做什么？", "你可以回答哪些问题", "怎么用", "Hello", "在吗"):
            assert smalltalk.kind(q) == "intro", q
        for q in ("谢谢", "谢谢你！", "多谢啦", "thanks"):
            assert smalltalk.kind(q) == "thanks", q

    def test_data_questions_are_not_small_talk(self):
        for q in ("介绍一下销售额最高的品类", "你能告诉我上海有多少客户吗？", "谢谢，那按州呢？",
                  "你好，各城市的客户数是多少？", "哪个卖家最好？", "各指标使用了哪些表和字段？", ""):
            assert smalltalk.kind(q) is None, q


class TestReplyText:
    def test_uses_dataset_note_and_samples(self):
        text = smalltalk.reply("intro", "巴西电商真实订单数据。", ["销售额最高的 5 个品类是哪些？"])
        assert "DeepQuery" in text and "巴西电商真实订单数据" in text
        assert "- 销售额最高的 5 个品类是哪些？" in text

    def test_falls_back_to_table_list(self):
        text = smalltalk.reply("intro", tables=["orders", "customers"])
        assert "2 张表：orders、customers" in text


class TestAgent:
    def test_intro_needs_no_model_call(self, settings, db):
        llm = MockLLM([SQL_REPLY])
        outcome = DeepQuery(settings, db, llm).ask("请你先介绍一下你自己吧", interactive=True, allow_clarify=True)
        assert outcome.status == "ok_chat" and outcome.succeeded
        assert "DeepQuery" in outcome.answer and outcome.final_sql is None
        assert outcome.usage["llm_calls"] == 0 and llm.calls == []

    def test_eval_mode_is_untouched(self, settings, db):
        # 评测不走寒暄捷径：提示词和流程与历史评测一致
        llm = MockLLM([SQL_REPLY])
        outcome = DeepQuery(settings, db, llm).ask("你好", generate_answer=False)
        assert outcome.status == "ok" and len(llm.calls) == 1

    def test_other_chat_answered_by_model(self, settings, db):
        reply = "闲聊。\n```answer\n我是查数据的助手，可以问问各城市的客户数。\n```"
        llm = MockLLM([reply])
        outcome = DeepQuery(settings, db, llm).ask("你是用什么模型做的？", interactive=True, allow_clarify=True)
        assert outcome.status == "ok_chat" and outcome.answer.startswith("我是查数据的助手")
        system = llm.calls[0][0]["content"]
        assert "不要用 clarify 反问" in system and "闲聊" in system


class TestServer:
    def test_sse_reply_step_and_payload(self, settings, db):
        app = create_app(agent=DeepQuery(settings, db, MockLLM([SQL_REPLY], cycle=True)), settings=settings)
        with TestClient(app) as c:
            text = c.post("/api/ask", json={"question": "你好，你是谁？"}).text
        events = []
        for block in text.strip().split("\n\n"):
            lines = [line for line in block.splitlines() if not line.startswith(":")]
            events.append((lines[0][7:], json.loads(lines[1][6:])))
        nodes = [d for e, d in events if e == "node"]
        final = [d for e, d in events if e == "final"][0]
        assert [n["node"] for n in nodes] == ["reply"] and nodes[0]["label"] == "直接回复"
        assert final["status"] == "ok_chat" and final["sql"] is None and final["usage"]["llm_calls"] == 0
