"""行为评测 / 分析评测的跑批器：路径判定、反问的精确率召回率、执行准确率、分析结论的溯源统计。"""

import json

import sqlglot

from deepquery.agent import DeepQuery
from deepquery.evalkit import behavior, behavior_set
from deepquery.evalkit.runner import load_cases
from fakes import RoutedLLM

CLARIFY = "有歧义。\n```clarify\n问题：按什么排？\n口径词：最好\n- 按消费金额\n- 按订单数\n```"
META = "看口径。\n```answer\n客户数按 customers 表计数。\n```"


class TestRoute:
    def test_route_of(self):
        assert behavior.route_of("needs_clarification") == "clarify"
        assert behavior.route_of("ok_meta") == "meta" and behavior.route_of("ok_chat") == "chat"
        for status in ("ok", "ok_empty", "failed", "budget_exceeded"):
            assert behavior.route_of(status) == "sql"

    def test_summary_metrics(self):
        rows = [
            {"category": "clarify", "expect": ["clarify"], "route": "clarify", "ex": None},
            {"category": "clarify", "expect": ["clarify"], "route": "sql", "ex": None},
            {"category": "data", "expect": ["sql"], "route": "clarify", "ex": False},
            {"category": "data", "expect": ["sql"], "route": "sql", "ex": True},
        ]
        for r in rows:
            r.update(route_ok=r["route"] in r["expect"], cost=0.0, latency_ms=10)
        s = behavior.summarize_behavior(rows)
        assert s["route_accuracy"]["rate"] == 0.5
        assert s["clarify_precision"]["rate"] == 0.5  # 问了 2 次，1 次该问
        assert s["clarify_recall"]["rate"] == 0.5  # 该问 2 次，问了 1 次
        assert s["over_clarify"]["rate"] == 0.5 and s["ex_accuracy"]["rate"] == 0.5
        assert s["confusion"] == {"clarify": {"clarify": 1, "sql": 1}, "sql": {"clarify": 1, "sql": 1}}


class TestRunBehavior:
    def test_end_to_end_on_demo_db(self, settings, db, demo_db_path):
        cases = [
            {"id": "c1", "category": "clarify", "question": "哪个客户最好？", "expect": ["clarify"], "gold_sql": None, "history": []},
            {"id": "m1", "category": "meta", "question": "客户数的口径是什么？", "expect": ["meta"], "gold_sql": None, "history": []},
            {"id": "h1", "category": "chat", "question": "你好", "expect": ["chat"], "gold_sql": None, "history": []},
            {"id": "d1", "category": "data", "question": "上海有多少客户？", "expect": ["sql"],
             "gold_sql": "SELECT COUNT(*) FROM customers WHERE city = '上海'", "history": []},
            {"id": "f1", "category": "followup", "question": "那北京呢？", "expect": ["sql"],
             "gold_sql": "SELECT COUNT(*) FROM customers WHERE city = '北京'",
             "history": [{"question": "上海有多少客户？", "sql": "SELECT COUNT(*) FROM customers WHERE city = '上海'"}]},
        ]
        llm = RoutedLLM([
            ("用户问题：哪个客户最好", CLARIFY),
            ("用户问题：客户数的口径是什么", META),
            ("用户问题：那北京呢", "```sql\nSELECT COUNT(*) FROM customers WHERE city = '北京'\n```"),
            ("用户问题：上海有多少客户", "```sql\nSELECT COUNT(*) FROM customers WHERE city = '上海'\n```"),
        ])
        report = behavior.run_behavior(DeepQuery(settings, db, llm), cases, str(demo_db_path))
        s = report["summary"]
        assert s["route_accuracy"]["rate"] == 1.0 and s["ex_accuracy"]["rate"] == 1.0
        assert s["clarify_recall"]["rate"] == 1.0 and s["over_clarify"]["rate"] == 0.0
        # 追问题的提示词里带上了上一轮
        followup_call = next(c for c in llm.calls if "用户问题：那北京呢" in c[-1]["content"])
        assert "上海有多少客户？" in followup_call[-1]["content"]


class TestRunAnalysis:
    def test_summary(self, settings, db):
        total = db.run_query("SELECT COUNT(*) FROM customers").rows[0][0]
        llm = RoutedLLM([
            ("要回答一个需要多步分析", "```plan\n1. 一共有多少客户 | 总体\n```"),
            ("判断现有结果是否足以回答", "```done\n```"),
            ("根据各步查询的结果回答", f"客户共 {total} 位 [1]"),
            ("用户问题：一共有多少客户", "```sql\nSELECT COUNT(*) FROM customers\n```"),
        ])
        report = behavior.run_analysis(DeepQuery(settings, db, llm), [{"id": "a1", "question": "客户多吗？"}])
        s = report["summary"]
        assert s["completed"]["rate"] == 1.0 and s["cited_ok"]["rate"] == 1.0
        assert s["step_success"]["rate"] == 1.0 and s["avg_steps"] == 1 and s["avg_numbers_verified"] == 1


class TestCaseFiles:
    def test_behavior_file_matches_generator(self):
        on_disk = load_cases("eval/cases/olist-behavior.jsonl")
        assert on_disk == behavior_set.cases()

    def test_behavior_cases_are_well_formed(self):
        cases = load_cases("eval/cases/olist-behavior.jsonl")
        assert len({c["id"] for c in cases}) == len(cases) >= 50
        for c in cases:
            assert set(c["expect"]) <= {"clarify", "meta", "chat", "sql"}, c["id"]
            if c["gold_sql"]:
                assert "sql" in c["expect"] and sqlglot.parse_one(c["gold_sql"], read="sqlite"), c["id"]
            for h in c["history"]:
                assert h["question"] and h["sql"], c["id"]
        cats = {c["category"] for c in cases}
        assert cats == {"data", "clarify", "missing", "meta", "chat", "followup"}

    def test_analysis_file(self):
        cases = load_cases("eval/cases/olist-analysis.jsonl")
        assert [c["question"] for c in cases] == behavior_set.ANALYSIS

    def test_gold_check_reports_problems(self, demo_db_path):
        cases = [
            {"id": "ok", "gold_sql": "SELECT COUNT(*) FROM customers", "history": []},
            {"id": "bad", "gold_sql": "SELECT nope FROM customers", "history": []},
            {"id": "empty", "gold_sql": "SELECT * FROM customers WHERE 1 = 0", "history": []},
        ]
        problems = behavior.gold_check(cases, str(demo_db_path))
        assert len(problems) == 2 and problems[0].startswith("bad") and "没有数据" in problems[1]
        json.dumps(problems)
