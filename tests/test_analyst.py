"""分析模式：拆问题 → 并行查询 → 看结果决定是否下钻 → 每句标注出处、逐句核对数字。"""

import json

import pytest
from fastapi.testclient import TestClient

from deepquery.agent import DeepQuery
from deepquery.agent.analyst import is_done, parse_plan
from deepquery.budget import RunCancelled, RunHandle
from deepquery.server import create_app
from deepquery.tools.contract import QueryResult
from deepquery.verify import check_cited, cited_number_count
from fakes import RoutedLLM

PLAN = "先看总量再看上海。\n```plan\n1. 一共有多少客户 | 看总体\n2. 上海有多少客户 | 看上海的规模\n```"
DONE = "够了。\n```done\n```"
SQL_ALL = "```sql\nSELECT COUNT(*) AS 客户数 FROM customers\n```"
SQL_SH = "```sql\nSELECT COUNT(*) AS 客户数 FROM customers WHERE city = '上海'\n```"


def counts(db):
    total = db.run_query("SELECT COUNT(*) FROM customers").rows[0][0]
    sh = db.run_query("SELECT COUNT(*) FROM customers WHERE city = '上海'").rows[0][0]
    return total, sh


def routes(db, report=None, review=DONE, plan=PLAN, extra=()):
    total, sh = counts(db)
    report = report or f"上海客户占比不高。\n- 全部客户 {total} 位 [1]\n- 上海 {sh} 位 [2]"
    return [
        ("要回答一个需要多步分析", plan),
        ("判断现有结果是否足以回答", review),
        ("对不上出处", report),  # 重写轮
        ("根据各步查询的结果回答", report),
        *extra,
        ("用户问题：一共有多少客户", SQL_ALL),
        ("用户问题：上海有多少客户", SQL_SH),
    ]


class TestParsing:
    def test_parse_plan(self):
        text = "思路。\n```plan\n1. 总销售额 | 看总体\n2、各品类销售额\n  3) 各州销售额 | 定位地区\n不是步骤\n```"
        steps = parse_plan(text)
        assert [s["question"] for s in steps] == ["总销售额", "各品类销售额", "各州销售额"]
        assert steps[0]["purpose"] == "看总体" and steps[1]["purpose"] == ""
        assert [s["no"] for s in parse_plan(text, start=5, limit=2)] == [5, 6]
        assert parse_plan("没有代码块") == []

    def test_is_done(self):
        assert is_done(DONE) and not is_done(PLAN)


def res(*values):
    return QueryResult(ok=True, columns=["v"], rows=[(v,) for v in values], row_count=len(values))


class TestCitationCheck:
    STEPS = {1: (res(240), "SELECT COUNT(*) FROM customers"), 2: (res(21), "SELECT ... WHERE month = '2018-03'"), 3: (None, "")}

    def test_numbers_must_come_from_the_cited_step(self):
        assert check_cited("全部客户 240 位 [1]，上海 21 位 [2]。", self.STEPS) == []
        assert check_cited("上海 240 位 [2]。", self.STEPS) == ["「240」不在所标注的 [2] 的结果里"]
        assert check_cited("两者合计 240 位 [1][2]。", self.STEPS) == []

    def test_uncited_and_missing_steps(self):
        assert check_cited("全部客户 240 位。", self.STEPS) == ["「240」没有标注出自哪一步"]
        assert check_cited("有 99 位 [3]。", self.STEPS) == ["引用的第 3 步没有可用的查询结果"]
        assert check_cited("有 99 位 [7]。", self.STEPS) == ["引用的第 7 步没有可用的查询结果"]

    def test_citation_after_full_stop_and_sql_numbers(self):
        assert check_cited("全部客户 240 位。[1]\n2018 年 3 月的数据 [2]", self.STEPS) == []
        assert check_cited("结论：没有数字的句子不用标注。", self.STEPS) == []
        assert cited_number_count("全部客户 240 位 [1]，上海 21 位 [2]") == 2


class TestAnalyst:
    def test_plan_parallel_steps_and_cited_report(self, settings, db):
        llm = RoutedLLM(routes(db))
        agent = DeepQuery(settings, db, llm)
        nodes, outcome = [], None
        for kind, item, _ in agent.analyst.analyze_stream("上海的客户多吗？"):
            if kind == "node":
                nodes.append(item)
            else:
                outcome = item
        assert nodes[0] == "plan" and nodes.count("run_step") == 2 and nodes[-2:] == ["review", "report"]
        assert outcome.status == "ok" and outcome.numbers_verified == 2 and not outcome.hallucination_blocked
        assert [s["no"] for s in outcome.steps] == [1, 2] and all(s["ok"] for s in outcome.steps)
        assert outcome.steps[1]["summary"] == ["筛选：所在城市 为 上海"]
        # 子查询必须查数据：不带"依据表结构直接回答"的规则
        sql_calls = [c for c in llm.calls if "数据分析工程师" in c[0]["content"]]
        assert sql_calls and all("不用查数据的问题" not in c[0]["content"] for c in sql_calls)

    def test_review_adds_a_drill_down_step(self, settings, db):
        drill = "再看上海的会员等级。\n```plan\n3. 上海客户的会员等级分布 | 下钻\n```"
        sql3 = "```sql\nSELECT vip_level, COUNT(*) AS n FROM customers WHERE city = '上海' GROUP BY vip_level\n```"
        llm = RoutedLLM(routes(db, review=drill, extra=[("用户问题：上海客户的会员等级分布", sql3)]))
        outcome = DeepQuery(settings, db, llm).analyst.analyze("上海的客户多吗？")
        assert [s["no"] for s in outcome.steps] == [1, 2, 3] and outcome.steps[2]["ok"]
        assert outcome.review_note == "再看上海的会员等级。"
        assert sum("判断现有结果是否足以回答" in c[0]["content"] for c in llm.calls) == 1  # 下钻只一轮

    def test_wrong_citation_is_blocked_after_one_retry(self, settings, db):
        total, _ = counts(db)
        bad = f"上海有 {total} 位客户 [2]"  # 数字对，但标错了步骤
        llm = RoutedLLM(routes(db, report=bad))
        outcome = DeepQuery(settings, db, llm).analyst.analyze("上海的客户多吗？")
        assert outcome.hallucination_blocked and outcome.status == "ok"
        assert "已改为直接列出各步查询结果" in outcome.answer and "[1]" in outcome.answer
        assert sum("对不上出处" in c[-1]["content"] for c in llm.calls) == 1

    def test_unparseable_plan_falls_back_to_the_question(self, settings, db):
        llm = RoutedLLM(routes(db, plan="我不太会拆。", extra=[("用户问题：上海的客户多吗", SQL_SH)]))
        outcome = DeepQuery(settings, db, llm).analyst.analyze("上海的客户多吗？")
        assert [s["question"] for s in outcome.steps] == ["上海的客户多吗？"]

    def test_all_steps_failing(self, settings, db):
        bad = "```sql\nSELECT nope FROM customers\n```"
        broken = [("你之前的 SQL 没有得到可用结果", bad), ("用户问题：一共有多少客户", bad), ("用户问题：上海有多少客户", bad)]
        llm = RoutedLLM(routes(db, extra=broken), default=bad)  # 修复轮、换思路重写也修不好
        outcome = DeepQuery(settings, db, llm).analyst.analyze("上海的客户多吗？")
        assert outcome.status == "failed" and "没法下结论" in outcome.answer

    def test_cancel(self, settings, db):
        handle = RunHandle()
        handle.cancel()
        with pytest.raises(RunCancelled):
            list(DeepQuery(settings, db, RoutedLLM(routes(db))).analyst.analyze_stream("x", handle=handle))


def sse(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if lines:
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


class TestServer:
    def test_analyze_mode_over_sse(self, settings, db):
        app = create_app(agent=DeepQuery(settings, db, RoutedLLM(routes(db))), settings=settings)
        with TestClient(app) as c:
            events = sse(c.post("/api/ask", json={"question": "上海的客户多吗？", "mode": "analyze"}).text)
            nodes = [d for e, d in events if e == "node"]
            final = [d for e, d in events if e == "final"][0]
            assert nodes[0]["node"] == "plan" and len(nodes[0]["steps"]) == 2
            step_events = [n for n in nodes if n["node"] == "run_step"]
            assert len(step_events) == 2 and all(n["step"]["rows"] for n in step_events)
            assert final["mode"] == "analyze" and final["status"] == "ok" and final["run_id"]
            assert [s["no"] for s in final["steps"]] == [1, 2] and final["source_tables"] == ["customers"]
            assert final["numbers_verified"] == 2
            again = [d for e, d in sse(c.post("/api/ask", json={"question": "上海的客户多吗？", "mode": "analyze"}).text) if e == "final"][0]
            assert again["cached"] is True and again["mode"] == "analyze"
            # 同一个问题的普通模式不会命中分析模式的缓存
            plain = [d for e, d in sse(c.post("/api/ask", json={"question": "上海的客户多吗？"}).text) if e == "final"][0]
            assert plain.get("mode") != "analyze" and plain["cached"] is False

    def test_small_talk_in_analyze_mode(self, settings, db):
        app = create_app(agent=DeepQuery(settings, db, RoutedLLM(routes(db))), settings=settings)
        with TestClient(app) as c:
            final = [d for e, d in sse(c.post("/api/ask", json={"question": "你好", "mode": "analyze"}).text) if e == "final"][0]
            assert final["status"] == "ok_chat"


class TestMcpTool:
    def test_analyze_data(self, settings, db):
        from deepquery import mcp_server

        mcp_server.set_agent(DeepQuery(settings, db, RoutedLLM(routes(db))))
        try:
            out = mcp_server.analyze_data("上海的客户多吗？")
        finally:
            mcp_server.set_agent(None)
        assert out["status"] == "ok" and "[1]" in out["answer"]
        assert [s["no"] for s in out["steps"]] == [1, 2] and "customers" in out["steps"][1]["sql"]
        json.dumps(out)  # 能直接作为工具结果返回
