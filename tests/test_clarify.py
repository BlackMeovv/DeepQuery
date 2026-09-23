"""澄清：问题有歧义或数据缺失时，交互模式下 Agent 先向用户确认，而不是猜一个答案。"""

import json

from fastapi.testclient import TestClient

from deepquery.agent import DeepQuery
from deepquery.agent import prompts
from deepquery.agent.graph import extract_clarification
from deepquery.llm import MockLLM
from deepquery.server import create_app

CLARIFY_REPLY = """"最好的客户"有多种理解，需要先确认口径。
```clarify
问题：你说的"最好的客户"按什么衡量？
口径词："最好的客户"
- 累计消费金额最高
- 下单次数最多
- 会员等级最高
```"""

SQL_REPLY = "思路。\n```sql\nSELECT COUNT(*) FROM customers\n```"


class TestExtractClarification:
    def test_full_format(self):
        c = extract_clarification(CLARIFY_REPLY)
        assert c["question"] == '你说的"最好的客户"按什么衡量？'
        assert c["term"] == "最好的客户"  # 引号被去掉，用来生成记忆
        assert c["options"] == ["累计消费金额最高", "下单次数最多", "会员等级最高"]

    def test_numbered_options_and_no_prefix(self):
        text = "```clarify\n库里没有退货记录，要换个问法吗？\n1. 用已取消订单占比近似\n2、改看各城市订单状态分布\n```"
        c = extract_clarification(text)
        assert c["question"].startswith("库里没有退货记录")
        assert c["term"] == "" and len(c["options"]) == 2

    def test_caps_options_and_ignores_other_blocks(self):
        opts = "\n".join(f"- 选项{i}" for i in range(6))
        c = extract_clarification(f"```sql\nSELECT 1\n```\n```clarify\n问题：选哪个？\n{opts}\n```")
        assert len(c["options"]) == 4
        assert extract_clarification(SQL_REPLY) is None


class TestAgentClarify:
    def test_interactive_mode_returns_question_without_running_sql(self, settings, db):
        llm = MockLLM([CLARIFY_REPLY])
        outcome = DeepQuery(settings, db, llm).ask("哪个客户最好？", allow_clarify=True)
        assert outcome.status == "needs_clarification"
        assert outcome.clarification["options"][0] == "累计消费金额最高"
        assert outcome.answer == outcome.clarification["question"]
        assert outcome.attempts == [] and outcome.usage["llm_calls"] == 1
        assert not outcome.succeeded  # 不会被当成答案写进缓存
        assert "澄清规则" in llm.calls[0][0]["content"]

    def test_eval_mode_prompt_is_unchanged(self, settings, db):
        # 评测默认不允许澄清：系统提示词必须与加入澄清功能之前逐字一致，历史评测数字才可比
        llm = MockLLM([SQL_REPLY])
        DeepQuery(settings, db, llm).ask("客户数？", generate_answer=False)
        assert llm.calls[0][0]["content"] == prompts.sql_system("sqlite")

    def test_clear_question_still_answered_directly(self, settings, db):
        llm = MockLLM([SQL_REPLY])
        outcome = DeepQuery(settings, db, llm).ask("客户数？", generate_answer=False, allow_clarify=True)
        assert outcome.status == "ok" and outcome.clarification is None


def sse_events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        ev = next(line[7:] for line in lines if line.startswith("event: "))
        data = next(line[6:] for line in lines if line.startswith("data: "))
        out.append((ev, json.loads(data)))
    return out


class TestServerClarify:
    def make(self, settings, db):
        llm = MockLLM([CLARIFY_REPLY], cycle=True)
        return TestClient(create_app(agent=DeepQuery(settings, db, llm), settings=settings)), llm

    def test_sse_carries_clarification_and_is_not_cached(self, settings, db):
        c, llm = self.make(settings, db)
        with c:
            events = sse_events(c.get("/api/ask", params={"question": "哪个客户最好？"}).text)
            nodes = [d["node"] for e, d in events if e == "node"]
            final = [d for e, d in events if e == "final"][0]
            assert nodes == ["generate_sql", "clarify"]
            assert final["status"] == "needs_clarification"
            assert final["clarification"]["term"] == "最好的客户"
            calls = len(llm.calls)
            again = [d for e, d in sse_events(c.get("/api/ask", params={"question": "哪个客户最好？"}).text) if e == "final"][0]
            assert again["cached"] is False and len(llm.calls) > calls

    def test_follow_up_with_clarify_off_never_asks_again(self, settings, db):
        c, llm = self.make(settings, db)
        with c:
            resp = c.get("/api/ask", params={"question": "哪个客户最好？（补充说明：累计消费金额最高）", "clarify": 0})
            final = [d for e, d in sse_events(resp.text) if e == "final"][0]
            assert final["status"] != "needs_clarification"
            assert "澄清规则" not in llm.calls[0][0]["content"]
