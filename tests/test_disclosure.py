"""渐进式披露：表目录常驻、表定义按需展开、列取值按需查询；空结果时自动查过滤列的真实取值。"""

import json

from fastapi.testclient import TestClient

from deepquery import disclosure
from deepquery.agent import DeepQuery
from deepquery.llm import MockLLM
from deepquery.server import create_app

COLUMNS = {
    "customers": ["id", "name", "city", "vip_level"],
    "orders": ["id", "customer_id", "order_date", "status"],
}


def sql_reply(sql):
    return f"思路。\n```sql\n{sql}\n```"


class TestCatalog:
    def test_table_note_sqlite_and_mysql(self):
        assert disclosure.table_note("CREATE TABLE orders (   -- 订单表\n  id INT\n)") == "订单表"
        mysql = "CREATE TABLE `orders` (\n  `id` int\n) ENGINE=InnoDB COMMENT='订单主表'"
        assert disclosure.table_note(mysql) == "订单主表"
        assert disclosure.table_note("CREATE TABLE t (id INT)") == ""

    def test_catalog_is_much_smaller_than_full_schema(self, db):
        docs = db.schema_by_table()
        cols = {t: [c["name"] for c in cs] for t, cs in db.table_columns().items()}
        catalog = disclosure.build_catalog(docs, cols)
        assert catalog["orders"].startswith("orders：") and "status" in catalog["orders"]
        assert sum(map(len, catalog.values())) < 0.5 * sum(map(len, docs.values()))

    def test_compose_expands_only_selected(self, db):
        docs = db.schema_by_table()
        cols = {t: [c["name"] for c in cs] for t, cs in db.table_columns().items()}
        text = disclosure.compose(docs, disclosure.build_catalog(docs, cols), ["orders"])
        assert text.count("CREATE TABLE") == 1 and "CREATE TABLE orders" in text
        assert "其余的表" in text and "customers" in text  # 没展开的表仍在目录里


class TestParseRequest:
    def test_tables_and_values(self):
        text = "要订单和客户。\n```tables\n- Orders\ncustomers, ghosts\n```\n```values\norders.STATUS\ncustomers.nope\n```"
        tables, probes = disclosure.parse_request(text, set(COLUMNS), COLUMNS)
        assert tables == ["orders", "customers"]  # 大小写还原成真实写法，不存在的表丢掉
        assert probes == [("orders", "status")]  # 不存在的列丢掉

    def test_value_probes_are_capped(self):
        cols = {"t": [f"c{i}" for i in range(10)]}
        body = "\n".join(f"t.c{i}" for i in range(10))
        _, probes = disclosure.parse_request(f"```values\n{body}\n```", {"t"}, cols)
        assert len(probes) == disclosure.MAX_VALUE_PROBES


class TestProbeValues:
    def test_most_common_values_with_counts(self, db):
        note = disclosure.probe_values(db, "orders", "status")
        assert note.startswith("orders.status：")
        top = db.run_query("SELECT status, COUNT(*) FROM orders GROUP BY status ORDER BY 2 DESC LIMIT 1").rows[0]
        assert f"{top[0]}（{top[1]}）" in note

    def test_high_cardinality_is_truncated(self, db):
        note = disclosure.probe_values(db, "customers", "name", limit=5)
        assert note.count("（") == 5 and note.endswith("只列出最常见的这些")


class TestFilteredColumns:
    def test_aliases_in_and_like(self):
        sql = (
            "SELECT COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id "
            "WHERE c.city = 'Shanghai' AND o.status IN ('done', 'ok') AND c.name LIKE '%王%'"
        )
        assert disclosure.filtered_columns(sql, COLUMNS) == [
            ("customers", "city"), ("orders", "status"), ("customers", "name")
        ]
        assert len(disclosure.filtered_columns(sql, COLUMNS, limit=1)) == 1

    def test_unqualified_lower_and_ignored_cases(self):
        assert disclosure.filtered_columns("SELECT * FROM customers WHERE LOWER(city) = 'sh'", COLUMNS) == [
            ("customers", "city")
        ]
        # 数字比较不是写法问题；两张表都有的 id 无法确定归属：都不查
        sql = "SELECT * FROM orders JOIN customers ON 1=1 WHERE vip_level = 3 AND id = 'x'"
        assert disclosure.filtered_columns(sql, COLUMNS) == []
        assert disclosure.filtered_columns("not sql at all (", COLUMNS) == []


def disclose_settings(settings, **kw):
    return settings.model_copy(update={"schema_rag": "disclose", **kw})


class TestDisclosureInGraph:
    def test_browse_then_expand_selected_tables_and_values(self, settings, db):
        browse = "问的是订单状态。\n```tables\norders\n```\n```values\norders.status\n```"
        llm = MockLLM([browse, sql_reply("SELECT COUNT(*) FROM orders WHERE status = 'completed'")])
        outcome = DeepQuery(disclose_settings(settings), db, llm).ask("已完成的订单有多少？", generate_answer=False)
        assert outcome.status == "ok" and outcome.selected_tables == ["orders"]
        first, second = llm.calls[0][1]["content"], llm.calls[1][1]["content"]
        assert "表目录" in first and "CREATE TABLE" not in first
        assert "CREATE TABLE orders" in second and "CREATE TABLE customers" not in second
        assert "orders.status：completed" in second  # 查到的真实取值交给写 SQL 的一步

    def test_browse_without_format_falls_back(self, settings, db):
        llm = MockLLM(["我也不知道。", sql_reply("SELECT COUNT(*) FROM customers WHERE city = '上海'")])
        cfg = disclose_settings(settings, schema_rag_top_k=2)
        outcome = DeepQuery(cfg, db, llm).ask("上海的客户数？", generate_answer=False)
        assert outcome.status == "ok" and len(outcome.selected_tables) == 2  # 退回检索选表

    def test_repair_expands_tables_used_but_not_disclosed(self, settings, db):
        browse = "```tables\ncustomers\n```"
        bad = sql_reply("SELECT COUNT(*) FROM orders WHERE state = 'x'")  # 用了没展开的 orders，列名也错
        good = sql_reply("SELECT COUNT(*) FROM orders")
        llm = MockLLM([browse, bad, good])
        outcome = DeepQuery(disclose_settings(settings), db, llm).ask("订单数？", generate_answer=False)
        assert outcome.status == "ok"
        assert "CREATE TABLE orders" not in llm.calls[1][1]["content"]
        assert "CREATE TABLE orders" in llm.calls[2][1]["content"]  # 修复前补上了完整定义
        assert outcome.selected_tables == ["customers", "orders"]


class TestValueProbeOnEmptyResult:
    def test_empty_result_brings_real_values(self, settings, db):
        llm = MockLLM([
            sql_reply("SELECT name FROM customers WHERE city = 'Shanghai'"),
            sql_reply("SELECT name FROM customers WHERE city = '上海'"),
        ])
        outcome = DeepQuery(settings, db, llm).ask("上海有哪些客户？", generate_answer=False)
        assert outcome.status == "ok"
        repair_prompt = llm.calls[1][1]["content"]
        assert "customers.city：" in repair_prompt and "上海（" in repair_prompt

    def test_zero_count_with_impossible_filter_is_not_an_answer(self, settings, db):
        # COUNT 得 0 不报错；但 'Shanghai' 在 city 列里根本不存在，这个 0 不可信
        llm = MockLLM([
            sql_reply("SELECT COUNT(*) FROM customers WHERE city = 'Shanghai'"),
            sql_reply("SELECT COUNT(*) FROM customers WHERE city = '上海'"),
        ])
        outcome = DeepQuery(settings, db, llm).ask("上海的客户数？", generate_answer=False)
        first = outcome.attempts[0]
        assert not first.ok and first.error_kind == "empty_result"
        assert "'Shanghai' 不在 customers.city 里" in first.error_message
        assert "customers.city：" in llm.calls[1][1]["content"]
        assert outcome.status == "ok" and outcome.result.rows[0][0] > 0

    def test_real_zero_is_kept(self, settings, db):
        # 过滤值真实存在、只是组合起来没有数据：0 是真实答案，不打扰
        sql = "SELECT COUNT(*) FROM orders WHERE status = 'completed' AND order_date < '1900-01-01'"
        llm = MockLLM([sql_reply(sql)])
        outcome = DeepQuery(settings, db, llm).ask("1900 年以前完成的订单数？", generate_answer=False)
        assert outcome.status == "ok" and outcome.result.rows[0][0] == 0 and len(llm.calls) == 1

    def test_confirmed_empty_is_accepted(self, settings, db):
        # 模型核对后原样重发：确认结果确实为空
        sql = sql_reply("SELECT COUNT(*) FROM customers WHERE city = '亚特兰蒂斯'")
        outcome = DeepQuery(settings, db, MockLLM([sql, sql])).ask("亚特兰蒂斯的客户数？")
        assert outcome.status == "ok_empty" and len(outcome.attempts) == 1

    def test_can_be_switched_off(self, settings, db):
        llm = MockLLM([
            sql_reply("SELECT name FROM customers WHERE city = 'Shanghai'"),
            sql_reply("SELECT name FROM customers WHERE city = '上海'"),
        ])
        cfg = settings.model_copy(update={"repair_value_probe": False})
        DeepQuery(cfg, db, llm).ask("上海有哪些客户？", generate_answer=False)
        assert "列的真实取值" not in llm.calls[1][1]["content"]
        llm = MockLLM([sql_reply("SELECT COUNT(*) FROM customers WHERE city = 'Shanghai'")])
        outcome = DeepQuery(cfg, db, llm).ask("上海的客户数？", generate_answer=False)
        assert outcome.status == "ok" and len(llm.calls) == 1  # 关掉后 0 按原样当答案

    def test_no_probe_without_empty_result(self, settings, db):
        llm = MockLLM([sql_reply("SELECT COUNT(*) FROM customers WHERE city = '上海'")])
        DeepQuery(settings, db, llm).ask("上海的客户数？", generate_answer=False)
        assert "列的真实取值" not in llm.calls[0][1]["content"]


def sse_nodes(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if lines and lines[0] == "event: node":
            out.append(json.loads(lines[1][6:]))
    return out


class TestServerSteps:
    def test_node_events_carry_details(self, settings, db):
        browse = "问的是订单状态。\n```tables\norders\n```\n```values\norders.status\n```"
        replies = [
            browse,
            sql_reply("SELECT COUNT(*) FROM orders WHERE status = 'done'"),
            sql_reply("SELECT COUNT(*) FROM orders WHERE status = 'completed'"),
            "已完成的订单见下方结果表。",
        ]
        cfg = disclose_settings(settings)
        app = create_app(agent=DeepQuery(cfg, db, MockLLM(replies)), settings=cfg)
        with TestClient(app) as c:
            nodes = sse_nodes(c.post("/api/ask", json={"question": "已完成的订单有多少？"}).text)
        browse_ev = next(n for n in nodes if n["node"] == "browse_schema")
        assert browse_ev["label"] == "浏览表目录"
        assert browse_ev["detail"] == "展开 orders 的完整定义；查看 orders.status 的真实取值"
        repair_ev = next(n for n in nodes if n["node"] == "repair")
        assert "detail" not in repair_ev  # orders.status 已经查过，不重复查
