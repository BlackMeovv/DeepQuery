"""针对代码审查确认缺陷的回归测试。每个测试对应一条已修复的发现。"""

import sqlite3

from deepquery.agent import DeepQuery
from deepquery.agent.graph import extract_sql
from deepquery.evalkit.scorer import execution_match
from deepquery.llm import MockLLM


def make_agent(settings, db, replies):
    return DeepQuery(settings, db, MockLLM(replies))


def sql_reply(sql: str) -> str:
    return f"思路说明。\n```sql\n{sql}\n```"


class TestAcceptEmptyStrictness:
    def test_resending_older_failed_sql_is_not_confirmation(self, settings, db):
        """重发【更早的失败 SQL】是模型混乱，不能被当成"确认空结果"。"""
        bad = "SELECT nope FROM customers"
        empty = "SELECT * FROM customers WHERE city = '月球'"
        agent = make_agent(
            settings,
            db,
            [sql_reply(bad), sql_reply(empty), sql_reply(bad)],  # 最后模型回退到更早的坏 SQL
        )
        outcome = agent.ask("月球的客户？", generate_answer=False)
        assert outcome.status != "ok_empty"

    def test_resending_last_empty_sql_is_confirmation(self, settings, db):
        empty = "SELECT * FROM customers WHERE city = '月球'"
        agent = make_agent(settings, db, [sql_reply(empty)])  # repair 原样重发同一条
        outcome = agent.ask("月球的客户？", generate_answer=False)
        assert outcome.status == "ok_empty"


class TestGenerateAnswerInState:
    def test_no_cross_call_leakage(self, settings, db):
        """generate_answer 必须在图状态里，不能是实例属性（并发串扰）。"""
        good = "SELECT COUNT(*) FROM customers"
        agent1 = make_agent(settings, db, [sql_reply(good), "总结。"])
        out1 = agent1.ask("客户数？", generate_answer=True)
        assert out1.usage["llm_calls"] == 2 and out1.answer == "总结。"
        # 直接验证实现：不允许存在跨调用的实例属性
        assert not hasattr(agent1, "_generate_answer")


class TestPredictedSqlForScoring:
    def test_guard_limit_does_not_break_ex(self, settings, db, demo_db_path):
        """守卫注入的 LIMIT 不参与 EX 判定：>200 行的正确预测必须判对。"""
        sql = "SELECT name FROM customers"  # 240 行 > 行数限额 200
        agent = make_agent(settings, db, [sql_reply(sql)])
        outcome = agent.ask("列出所有客户姓名", generate_answer=False)
        assert outcome.status == "ok"
        assert "limit" in (outcome.final_sql or "").lower()  # 执行版被限额
        assert "limit" not in (outcome.predicted_sql or "").lower()  # 评测版没被污染
        assert execution_match(demo_db_path, outcome.predicted_sql, sql).match


class TestBudgetStatus:
    def test_budget_exceeded_status_via_route(self, settings, db):
        tight = settings.model_copy(update={"agent_max_tokens_per_run": 1})
        agent = make_agent(tight, db, [sql_reply("SELECT bad FROM customers")])
        outcome = agent.ask("问题", generate_answer=False)
        assert outcome.status == "budget_exceeded"
        assert "预算超限" in outcome.answer


class TestRecursionLimit:
    def test_high_repair_rounds_do_not_crash(self, settings, db):
        """max_repair_rounds 调大不允许触发 LangGraph 默认 recursion_limit=25。"""
        deep = settings.model_copy(update={"agent_max_repair_rounds": 15})
        replies = [sql_reply(f"SELECT col_{i} FROM customers") for i in range(20)]
        agent = make_agent(deep, db, replies)
        outcome = agent.ask("问题", generate_answer=False)  # 不允许抛异常
        assert outcome.status == "failed"
        assert len(outcome.attempts) == 1 + deep.agent_max_repair_rounds


class TestExtractSqlVariants:
    def test_sqlite_language_tag(self):
        assert extract_sql("```sqlite\nSELECT 1\n```") == "SELECT 1"

    def test_plain_fence(self):
        assert extract_sql("```\nSELECT 1\n```") == "SELECT 1"

    def test_prefers_sql_block_over_json(self):
        text = '思路：\n```json\n{"plan": 1}\n```\n```sql\nSELECT 2\n```'
        assert extract_sql(text) == "SELECT 2"

    def test_select_block_without_tag_among_others(self):
        text = "```json\n{}\n```\n```\nWITH t AS (SELECT 1) SELECT * FROM t\n```"
        assert extract_sql(text).startswith("WITH t AS")

    def test_no_fence_at_all(self):
        assert extract_sql("SELECT 3;") == "SELECT 3"


class TestScorerEmptyColumns:
    def test_both_empty_but_different_columns(self, demo_db_path):
        gold = "SELECT id FROM customers WHERE 1 = 0"
        pred = "SELECT id, name FROM customers WHERE 1 = 0"
        score = execution_match(demo_db_path, pred, gold)
        assert not score.match and "列数" in score.reason

    def test_both_empty_same_columns_match(self, demo_db_path):
        gold = "SELECT id FROM customers WHERE 1 = 0"
        pred = "SELECT id FROM customers WHERE 2 = 1"
        assert execution_match(demo_db_path, pred, gold).match


class TestDemoDataWindow:
    def test_all_dates_within_window(self, demo_db_path):
        conn = sqlite3.connect(demo_db_path)
        try:
            max_order = conn.execute("SELECT MAX(order_date) FROM orders").fetchone()[0]
            max_signup = conn.execute("SELECT MAX(signup_date) FROM customers").fetchone()[0]
            min_order = conn.execute(
                "SELECT MIN(julianday(o.order_date) - julianday(c.signup_date)) "
                "FROM orders o JOIN customers c ON o.customer_id = c.id"
            ).fetchone()[0]
        finally:
            conn.close()
        assert max_order <= "2025-06-30"
        assert max_signup <= "2025-06-29"
        assert min_order >= 0  # 订单不得早于注册


class TestUnmeteredAccounting:
    def test_meter_snapshot_exposes_unmetered(self, settings, db):
        agent = make_agent(settings, db, [sql_reply("SELECT COUNT(*) FROM customers")])
        outcome = agent.ask("客户数？", generate_answer=False)
        assert "unmetered_calls" in outcome.usage


class TestRunnerAbortOnApiOutage:
    """API 断供（余额耗尽/限流封禁）时评测应熔断中止，而不是烧完全部调用。"""

    def test_consecutive_llm_failures_abort(self, demo_db_path, tmp_path, monkeypatch):
        import json

        from deepquery.evalkit import runner as runner_mod
        from deepquery.llm import BaseLLM, LLMError

        class DeadLLM(BaseLLM):
            model_name = "dead"

            def chat(self, messages, meter, tag=""):
                raise LLMError("insufficient quota")

        monkeypatch.setattr(runner_mod, "LLMClient", lambda _settings: DeadLLM())
        monkeypatch.setenv("DB_PATH", str(demo_db_path))
        from deepquery.config import get_settings

        get_settings.cache_clear()
        cases_file = tmp_path / "cases.jsonl"
        cases_file.write_text(
            "\n".join(
                json.dumps(
                    {
                        "id": f"c{i}",
                        "question": "客户有多少个？",
                        "gold_sql": "SELECT COUNT(*) FROM customers",
                    },
                    ensure_ascii=False,
                )
                for i in range(12)
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            report = runner_mod.run_eval(cases_file, repeats=3, out_path=tmp_path / "r.json")
        finally:
            get_settings.cache_clear()
        summary = report["summary"]
        assert summary["aborted"]
        assert summary["trials"] == 8  # 熔断阈值处停下：只发生了 8 次 trial，而非 36 次
        assert summary["ex_accuracy"] == 0.0
        # 没跑到的题不应让报告崩溃（无 last、空延迟列表）
        assert len(report["results"]) == 12


class TestConcurrentRunIsolation:
    """回归：共享的 agent 实例被并发请求使用时，本次注入的上下文（含用户私有
    记忆原文）不能串到别的请求里——此前它挂在实例属性上，后开始的请求会覆盖
    先开始的，A 的回答里带出 B 的记忆，并随结果写进缓存。"""

    def test_private_memories_do_not_leak_across_concurrent_runs(self, settings, db, tmp_path):
        import threading

        from deepquery.memory import MemoryStore

        memory = MemoryStore(tmp_path / "mem.sqlite")
        memory.remember("alice", "上海客户数口径：只算钻石会员（alice 私有）")
        memory.remember("bob", "上海客户数口径：包含注销账户（bob 私有）")

        barrier = threading.Barrier(2, timeout=10)

        class BarrierLLM(MockLLM):
            def chat(self, messages, meter, tag="", on_delta=None):
                if tag == "generate_sql":
                    barrier.wait()  # 两个运行都已完成上下文组装，才允许任一继续
                return super().chat(messages, meter, tag=tag, on_delta=on_delta)

        llm = BarrierLLM(["```sql\nSELECT COUNT(*) FROM customers WHERE city = '上海'\n```"], cycle=True)
        agent = DeepQuery(settings, db, llm, memory=memory)

        outcomes: dict = {}

        def run(user):
            outcomes[user] = agent.ask("上海客户数口径下有多少？", generate_answer=False, user_id=user)

        threads = [threading.Thread(target=run, args=(u,)) for u in ("alice", "bob")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        alice = outcomes["alice"].context_used["memories"]
        bob = outcomes["bob"].context_used["memories"]
        assert alice and all("alice" in m for m in alice)
        assert bob and all("bob" in m for m in bob)
