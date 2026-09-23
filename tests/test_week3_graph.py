"""Week 3 图接线测试：Schema RAG、按错误类型的修复提示、防幻觉拦截。"""

from deepquery.agent import DeepQuery
from deepquery.llm import MockLLM


def make_agent(settings, db, replies):
    llm = MockLLM(replies)
    return DeepQuery(settings, db, llm), llm


def sql_reply(sql):
    return f"思路。\n```sql\n{sql}\n```"


GOOD_SQL = "SELECT COUNT(*) FROM customers WHERE city = '上海'"


def shanghai_count(db) -> int:
    """测试里的数字必须从库里取——硬编码的数字会被防幻觉校验正确地拦下。"""
    return db.run_query(GOOD_SQL).rows[0][0]


class TestSchemaRag:
    def test_auto_mode_keeps_full_schema_for_small_db(self, settings, db):
        # 演示库 6 张表，top_k 默认 6：auto 不启用检索
        agent, llm = make_agent(settings, db, [sql_reply(GOOD_SQL)])
        outcome = agent.ask("上海的客户数？", generate_answer=False)
        assert outcome.selected_tables is None
        assert "CREATE TABLE payments" in llm.calls[0][1]["content"]  # 全量 schema

    def test_forced_on_selects_topk(self, settings, db):
        rag = settings.model_copy(update={"schema_rag": "on", "schema_rag_top_k": 2})
        agent, llm = make_agent(rag, db, [sql_reply("SELECT method, COUNT(*) FROM payments GROUP BY method")])
        outcome = agent.ask("每种支付方式有多少笔支付？", generate_answer=False)
        assert outcome.selected_tables is not None and len(outcome.selected_tables) == 2
        assert "payments" in outcome.selected_tables
        prompt = llm.calls[0][1]["content"]
        assert "CREATE TABLE payments" in prompt
        # 只喂了 2 张表：6 张表的全量 DDL 不应全部出现
        assert prompt.count("CREATE TABLE") == 2

    def test_off_mode(self, settings, db):
        off = settings.model_copy(update={"schema_rag": "off", "schema_rag_top_k": 2})
        agent, _ = make_agent(off, db, [sql_reply(GOOD_SQL)])
        outcome = agent.ask("上海的客户数？", generate_answer=False)
        assert outcome.selected_tables is None

    def test_glossary_injected(self, settings, db, tmp_path):
        g = tmp_path / "glossary.jsonl"
        g.write_text('{"term": "成交额", "definition": "quantity*unit_price 求和"}\n', encoding="utf-8")
        cfg = settings.model_copy(update={"glossary_path": str(g)})
        agent, llm = make_agent(cfg, db, [sql_reply(GOOD_SQL)])
        agent.ask("本月成交额是多少？", generate_answer=False)
        assert "业务字典" in llm.calls[0][1]["content"]
        assert "quantity*unit_price" in llm.calls[0][1]["content"]


class TestRepairHints:
    def test_error_specific_hint_in_repair_prompt(self, settings, db):
        agent, llm = make_agent(
            settings,
            db,
            [sql_reply("SELECT nope FROM customers"), sql_reply(GOOD_SQL)],
        )
        outcome = agent.ask("上海的客户数？", generate_answer=False)
        assert outcome.status == "ok"
        repair_prompt = llm.calls[1][-1]["content"]
        assert "列名不存在" in repair_prompt  # no_such_column 的针对性提示


class TestHallucinationGate:
    def test_fabricated_number_blocked_after_retry(self, settings, db):
        cnt = shanghai_count(db)
        agent, _ = make_agent(
            settings,
            db,
            [
                sql_reply(GOOD_SQL),
                f"上海共有 {cnt} 位客户，占全国的 37.9%。",  # 37.9% 无出处
                "重写后依然声称占比 37.9%。",  # 重写仍编造
            ],
        )
        outcome = agent.ask("上海的客户一共有多少个？")
        assert outcome.status == "ok"
        assert outcome.hallucination_blocked
        assert "37.9%" not in outcome.answer  # 降级为确定性结果预览
        assert outcome.usage["llm_calls"] == 3  # 生成 + 总结 + 一次重写

    def test_retry_fixes_answer(self, settings, db):
        cnt = shanghai_count(db)
        agent, _ = make_agent(
            settings,
            db,
            [
                sql_reply(GOOD_SQL),
                f"上海共有 {cnt} 位客户，占全国的 37.9%。",
                f"上海共有 {cnt} 位客户。",  # 重写后干净
            ],
        )
        outcome = agent.ask("上海的客户一共有多少个？")
        assert not outcome.hallucination_blocked
        assert outcome.answer == f"上海共有 {cnt} 位客户。"

    def test_clean_answer_untouched(self, settings, db):
        cnt = shanghai_count(db)
        agent, _ = make_agent(settings, db, [sql_reply(GOOD_SQL), f"上海共有 {cnt} 位客户。"])
        outcome = agent.ask("上海的客户一共有多少个？")
        assert not outcome.hallucination_blocked
        assert outcome.usage["llm_calls"] == 2  # 干净回答不触发重写

    def test_verify_can_be_disabled(self, settings, db):
        cfg = settings.model_copy(update={"answer_verify": False})
        agent, _ = make_agent(cfg, db, [sql_reply(GOOD_SQL), "占比 37.9%。"])
        outcome = agent.ask("上海的客户一共有多少个？")
        assert not outcome.hallucination_blocked
        assert outcome.answer == "占比 37.9%。"


class TestRunnerTableRecall:
    def test_recall_metric(self, settings, db, demo_db_path, tmp_path, monkeypatch):
        import json

        from deepquery.evalkit.runner import run_eval

        cases = tmp_path / "cases.jsonl"
        cases.write_text(
            json.dumps(
                {
                    "id": "c1",
                    "question": "每种支付方式有多少笔支付？",
                    "gold_sql": "SELECT method, COUNT(*) FROM payments GROUP BY method",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("SCHEMA_RAG", "on")
        monkeypatch.setenv("SCHEMA_RAG_TOP_K", "2")
        monkeypatch.setenv("DB_PATH", str(demo_db_path))
        from deepquery.config import get_settings

        get_settings.cache_clear()
        try:
            report = run_eval(cases, gold_replay=True, out_path=tmp_path / "r.json")
        finally:
            get_settings.cache_clear()
        assert report["summary"]["ex_accuracy"] == 1.0
        assert report["summary"]["avg_table_recall"] == 1.0
        assert report["results"][0]["table_recall"] == 1.0


class TestSchemaRagAutoBySize:
    """auto 判据按 schema 体积而非表数——BIRD 消融驱动的架构决策回归测试。"""

    def test_many_small_tables_stay_full(self, settings, db):
        # 6 表 > top_k=2，但全量体积远小于阈值：不检索，直供全量 schema
        cfg = settings.model_copy(update={"schema_rag": "auto", "schema_rag_top_k": 2})
        agent, llm = make_agent(cfg, db, [sql_reply(GOOD_SQL)])
        outcome = agent.ask("上海的客户数？", generate_answer=False)
        assert outcome.selected_tables is None
        assert llm.calls[0][1]["content"].count("CREATE TABLE") == 6

    def test_oversized_schema_enables_rag(self, settings, db):
        cfg = settings.model_copy(
            update={"schema_rag": "auto", "schema_rag_top_k": 2, "schema_rag_auto_max_chars": 10}
        )
        agent, _ = make_agent(cfg, db, [sql_reply(GOOD_SQL)])
        outcome = agent.ask("上海的客户数？", generate_answer=False)
        assert outcome.selected_tables is not None and len(outcome.selected_tables) == 2


class TestAllowedTables:
    """表级权限：ALLOWED_TABLES 过滤 Agent 视野与守卫白名单。"""

    def test_hidden_table_invisible_and_rejected(self, settings, db):
        cfg = settings.model_copy(update={"allowed_tables": "customers"})
        agent, llm = make_agent(
            cfg, db, [sql_reply("SELECT COUNT(*) FROM orders"), sql_reply(GOOD_SQL)]
        )
        assert agent.allowed_tables == {"customers"}
        assert "CREATE TABLE customers" in agent.full_schema  # schema 注入只含可见表
        assert "CREATE TABLE orders" not in agent.full_schema
        outcome = agent.ask("客户数？", generate_answer=False)
        # 第一条 SQL 查了隐藏表 orders：守卫拒绝 → 修复为 customers → 成功
        assert outcome.status == "ok"
        assert "customers" in outcome.final_sql
        assert any(a.error_kind == "guard_rejected" for a in outcome.attempts)

    def test_case_insensitive_and_unknown_ignored(self, settings, db, capsys):
        cfg = settings.model_copy(update={"allowed_tables": "CUSTOMERS, no_such_table"})
        agent, _ = make_agent(cfg, db, [sql_reply(GOOD_SQL)])
        assert agent.allowed_tables == {"customers"}
        assert "no_such_table" in capsys.readouterr().err

    def test_empty_config_means_all_tables(self, settings, db):
        agent, _ = make_agent(settings, db, [sql_reply(GOOD_SQL)])
        assert agent.allowed_tables == set(db.table_names())


class TestSchemaRefresh:
    """schema 指纹：建/改表后无需重启即被 Agent 看见。"""

    def _mutable_agent(self, settings, tmp_path, replies):
        import sqlite3

        from deepquery.demo_data import build
        from deepquery.tools.database import ReadOnlyDatabase

        path = tmp_path / "mutable.sqlite"
        build(path)
        db = ReadOnlyDatabase(path, timeout_seconds=5, max_rows=200)
        cfg = settings.model_copy(update={"db_path": str(path)})
        agent, llm = make_agent(cfg, db, replies)
        return agent, path, sqlite3

    def test_new_table_visible_without_restart(self, settings, tmp_path):
        agent, path, sqlite3 = self._mutable_agent(
            settings, tmp_path, [sql_reply("SELECT COUNT(*) FROM refunds")]
        )
        assert "refunds" not in agent.allowed_tables
        fp_before = agent.schema_fingerprint

        rw = sqlite3.connect(path)
        rw.execute("CREATE TABLE refunds (id INTEGER PRIMARY KEY, amount REAL)")
        rw.execute("INSERT INTO refunds VALUES (1, 9.9)")
        rw.commit()
        rw.close()

        outcome = agent.ask("退款总数？", generate_answer=False)
        assert agent.schema_fingerprint != fp_before  # 指纹随 DDL 变化
        assert "refunds" in agent.allowed_tables  # 新表已进白名单
        assert "refunds" in agent.full_schema  # 且进入 schema 注入
        assert outcome.status == "ok"  # 对新表的查询直接成功

    def test_data_change_keeps_fingerprint(self, settings, tmp_path):
        agent, path, sqlite3 = self._mutable_agent(settings, tmp_path, [sql_reply(GOOD_SQL)])
        fp = agent.schema_fingerprint
        rw = sqlite3.connect(path)
        rw.execute("UPDATE customers SET city = '上海' WHERE id = 1")
        rw.commit()
        rw.close()
        assert agent.maybe_refresh_schema() == fp  # 数据增删不触发重建
