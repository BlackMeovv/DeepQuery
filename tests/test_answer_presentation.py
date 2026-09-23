"""面向用户的呈现：先想后写的步骤、只有结论的回答、回答下方的出处、在线问答规则。"""

import json

from fastapi.testclient import TestClient

from deepquery.agent import DeepQuery
from deepquery.agent import prompts
from deepquery.agent.graph import extract_clarification, plain_answer
from deepquery.llm import MockLLM
from deepquery.server import create_app

SQL = "按城市统计客户数。\n```sql\nSELECT city, COUNT(*) AS n FROM customers GROUP BY city ORDER BY n DESC LIMIT 3\n```"


def _events(text: str) -> list[tuple[str, dict]]:
    out, kind = [], None
    for line in text.splitlines():
        if line.startswith("event:"):
            kind = line[6:].strip()
        elif line.startswith("data:"):
            out.append((kind, json.loads(line[5:])))
    return out


class TestPlainAnswer:
    def test_tables_and_markdown_are_removed(self):
        text = "**北京**客户最多，有 30 位。\n\n| 城市 | 客户数 |\n|---|---:|\n| 北京 | 30 |"
        assert plain_answer(text) == "北京客户最多，有 30 位。"

    def test_dangling_intro_points_to_the_result_table(self):
        assert plain_answer("客户最多的 3 个城市是：\n| 城市 | 数 |\n|---|---|\n| 北京 | 30 |") == "客户最多的 3 个城市见下方结果表。"

    def test_plain_sentences_are_untouched(self):
        assert plain_answer("一共有 1500 笔订单：其中已完成 900 笔。") == "一共有 1500 笔订单：其中已完成 900 笔。"


class TestClarifyOptions:
    def test_numbered_prefixes_are_stripped(self):
        c = extract_clarification("x\n```clarify\n问题：按什么排？\n口径词：大客户\n- 选项一：按累计消费金额\n- 选项2: 按下单次数\n```")
        assert c["options"] == ["按累计消费金额", "按下单次数"]


class TestInteractiveRules:
    def test_rules_only_in_interactive_mode(self, settings, db):
        llm = MockLLM([SQL, "北京客户最多。"])
        DeepQuery(settings, db, llm).ask("客户最多的城市？")
        assert llm.calls[0][0]["content"] == prompts.sql_system("sqlite")  # 评测提示词保持不变
        llm2 = MockLLM([SQL, "北京客户最多。"])
        DeepQuery(settings, db, llm2).ask("客户最多的城市？", interactive=True)
        assert prompts.INTERACTIVE_RULES.strip() in llm2.calls[0][0]["content"]


class TestServerPresentation:
    def test_sql_step_source_tables_and_verified_numbers(self, settings, db):
        n = db.run_query("SELECT COUNT(*) FROM customers WHERE city = (SELECT city FROM customers GROUP BY city ORDER BY COUNT(*) DESC LIMIT 1)").rows[0][0]
        top = db.run_query("SELECT city FROM customers GROUP BY city ORDER BY COUNT(*) DESC LIMIT 1").rows[0][0]
        llm = MockLLM([SQL, f"{top}的客户最多，有 {n} 位。"])
        app = create_app(agent=DeepQuery(settings, db, llm), settings=settings)
        with TestClient(app) as c:
            events = _events(c.get("/api/ask", params={"question": "客户最多的城市？"}).text)
        gen = next(d for k, d in events if k == "node" and d["node"] == "generate_sql")
        assert gen["thought"] == "按城市统计客户数。" and gen["sql"].startswith("SELECT city")
        final = events[-1][1]
        assert final["source_tables"] == ["customers"]
        assert final["numbers_verified"] == 1  # 回答里的客户数核对过出处
        # 在线问答规则已随提示词下发
        assert prompts.INTERACTIVE_RULES.strip() in llm.calls[0][0]["content"]
