"""数据库建档与记忆同步：结构 / 取值档案、表级指纹增量同步、依赖复核与自动恢复、Agent 接入。

全部离线：业务库是临时目录里的演示库副本，测试用普通连接改它（模拟 DBA 建表、删列、改数据），
Agent 和建档始终只读。
"""

import hashlib
import json
import sqlite3
import sys
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from deepquery.agent import DeepQuery
from deepquery.catalog import Catalog, CatalogStore
from deepquery.catalog.deps import missing_refs, sql_deps, text_deps
from deepquery.catalog.manager import diff_structure
from deepquery.catalog.profiler import (
    Profiler,
    TableStructure,
    column_notes_from_ddl,
    detect_date_format,
    keys_from_ddl,
    read_structures,
)
from deepquery.config import Settings
from deepquery.demo_data import build
from deepquery.llm import LLMError, MockLLM
from deepquery.memory import MemoryStore
from deepquery.retrieval import TextEntry, TextIndex
from deepquery.server import create_app
from deepquery.tools.database import ReadOnlyDatabase

from fakes import RoutedLLM

DESCRIBE_REPLY = (
    "```describe\n表：优惠券表\ncode：优惠券编码\nkind：优惠类型\n"
    "discount：不确定\nexpire_on：过期日期\n```"
)


@pytest.fixture()
def dbfile(tmp_path):
    path = tmp_path / "biz.sqlite"
    build(path)
    return path


@pytest.fixture()
def writer(dbfile):
    """模拟 DBA / 业务系统：用普通（可写）连接改业务库。"""
    conn = sqlite3.connect(dbfile)
    yield conn
    conn.close()


@pytest.fixture()
def rodb(dbfile):
    return ReadOnlyDatabase(dbfile, timeout_seconds=5)


def make_catalog(rodb, tmp_path, llm=None, **kw) -> Catalog:
    return Catalog(CatalogStore(tmp_path / "catalog.sqlite"), rodb, llm=llm, **kw)


def glossary(*pairs) -> TextIndex:
    return TextIndex([TextEntry(k, f"{k}：{v}") for k, v in pairs])


def examples(*pairs) -> TextIndex:
    return TextIndex([TextEntry(q, f"问：{q}\n```sql\n{s}\n```", sql=s) for q, s in pairs])


def add_coupons(writer, rows=60):
    writer.execute("CREATE TABLE coupons (code TEXT, kind TEXT, discount REAL, expire_on TEXT, order_id INTEGER)")
    writer.executemany(
        "INSERT INTO coupons VALUES (?, ?, ?, ?, ?)",
        [(f"CP{i:04d}", ["满减", "折扣"][i % 2], 0.1 * (i % 5), f"2025-0{1 + i % 9}-01", i) for i in range(1, rows)],
    )
    writer.commit()


# ---------------------------------------------------------------- 结构


class TestStructure:
    def test_keys_from_sqlite_and_mysql_ddl(self):
        pk, fks = keys_from_ddl(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, a INT REFERENCES u(x), b INT, FOREIGN KEY (b) REFERENCES v (y))"
        )
        assert pk == ["id"] and set(fks) == {("a", "u", "x"), ("b", "v", "y")}
        mysql = (
            "CREATE TABLE `orders` (\n  `id` int NOT NULL,\n  `customer_id` int NOT NULL COMMENT '下单客户',\n"
            "  PRIMARY KEY (`id`),\n  CONSTRAINT `fk` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`)\n"
            ") ENGINE=InnoDB AUTO_INCREMENT=17 COMMENT='订单表'"
        )
        assert keys_from_ddl(mysql, "mysql") == (["id"], [("customer_id", "customers", "id")])
        assert keys_from_ddl("CREATE TABLE t (a INT, b INT, PRIMARY KEY (a, b))")[0] == ["a", "b"]
        assert keys_from_ddl("这不是 SQL") == ([], [])

    def test_column_notes_sqlite_and_mysql(self):
        sqlite_ddl = "CREATE TABLE t (  -- 表\n    status TEXT NOT NULL,  -- 订单状态\n    x INT\n)"
        assert column_notes_from_ddl(sqlite_ddl, ["status", "x"]) == {"status": "订单状态"}
        mysql_ddl = "CREATE TABLE `t` (\n  `status` varchar(16) NOT NULL COMMENT '订单状态'\n) COMMENT='表'"
        assert column_notes_from_ddl(mysql_ddl, ["status"]) == {"status": "订单状态"}

    def test_fingerprint_ignores_data_but_not_structure(self, rodb, writer):
        before = read_structures(rodb)["orders"].fingerprint()
        writer.execute("INSERT INTO orders (customer_id, order_date, status) VALUES (1, '2025-07-01', 'refunded')")
        writer.execute("UPDATE orders SET status = 'pending' WHERE id = 1")
        writer.execute("DELETE FROM orders WHERE id = 2")
        writer.commit()
        assert read_structures(rodb)["orders"].fingerprint() == before  # 增删改行：指纹不变
        writer.execute("ALTER TABLE orders ADD COLUMN channel TEXT")
        writer.commit()
        assert read_structures(rodb)["orders"].fingerprint() != before  # 加一列：指纹变

    def test_diff_reports_columns_types_and_renames(self):
        old = TableStructure("t", [("id", "INT"), ("status", "TEXT"), ("amt", "REAL")], ["id"])
        new = TableStructure("t", [("id", "INT"), ("state", "TEXT"), ("amt", "INTEGER"), ("memo", "TEXT")], ["id"])
        change = diff_structure(old, new)
        assert change.added_columns == ["state", "memo"] and change.dropped_columns == ["status"]
        assert change.retyped == ["amt"] and change.renamed == []  # 一删两增，不猜改名
        renamed = diff_structure(old, TableStructure("t", [("id", "INT"), ("state", "TEXT"), ("amt", "REAL")], ["id"]))
        assert renamed.renamed == [("status", "state")]


# ---------------------------------------------------------------- 取值档案


class TestProfiler:
    def test_demo_profile_four_kinds(self, rodb):
        st = read_structures(rodb)
        profiler = Profiler(rodb)
        orders = profiler.profile_table(st["orders"])
        status = orders.columns["status"]
        assert status.kind == "enum" and set(status.values) == {"completed", "shipped", "pending", "cancelled"}
        assert status.complete
        date = orders.columns["order_date"]
        assert date.kind == "date" and date.date_format == "YYYY-MM-DD"
        assert date.date_min <= date.date_max and date.date_min.startswith("20")
        assert orders.columns["customer_id"].kind == "key"  # 声明的外键：不列取值
        customers = profiler.profile_table(st["customers"])
        assert customers.columns["name"].kind == "text" and len(customers.columns["name"].examples) == 3
        assert set(customers.columns["vip_level"].values) == {"0", "1", "2", "3"}

    def test_numbers_get_no_min_max(self, rodb):
        prof = Profiler(rodb).profile_table(read_structures(rodb)["payments"])
        amount = prof.columns["amount"]
        assert amount.kind == "number" and not amount.values and not amount.date_min

    def test_sampling_marks_enum_incomplete(self, rodb, writer):
        writer.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, level TEXT)")
        writer.executemany("INSERT INTO logs (level) VALUES (?)", [("info",)] * 150 + [("error",)])
        writer.commit()
        prof = Profiler(rodb, sample_rows=100).profile_table(read_structures(rodb)["logs"])
        level = prof.columns["level"]
        assert prof.row_count == 101 and prof.sampled == 100  # 行数只数到抽样上限 + 1，大表不做全表 COUNT
        assert level.values == ["info"] and not level.complete  # 抽样没覆盖全表：取值可能不全

    def test_sparse_and_empty(self, rodb, writer, tmp_path):
        writer.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, memo TEXT, flag TEXT)")
        writer.executemany("INSERT INTO notes (memo) VALUES (?)", [("备注",)] + [(None,)] * 9)
        writer.execute("CREATE TABLE void (id INTEGER PRIMARY KEY, x TEXT)")
        writer.commit()
        cat = make_catalog(rodb, tmp_path)
        cat.sync()
        block = cat.view().render("notes")
        assert "memo：" in block and "多数为空" in block and "flag：全部为空" in block
        assert "空表" in cat.view().render("void")

    def test_date_formats(self):
        assert detect_date_format(["2025-01-02", "2024-12-31"], False) == ("YYYY-MM-DD", True)
        assert detect_date_format(["2025-01-02 08:00:00"], False)[0] == "YYYY-MM-DD HH:MM:SS"
        assert detect_date_format([20250102, 20241231], True) == ("YYYYMMDD（8 位数字）", True)
        assert detect_date_format([20250102], False) is None  # 8 位数字只在列名像日期时才认
        assert detect_date_format(["1/2/2025"], False)[1] is False  # 非 ISO 写法：不给起止
        assert detect_date_format(["上海", "2025-01-02"], True) is None

    def test_relation_inferred_only_when_data_agrees(self, rodb, writer):
        writer.execute("CREATE TABLE refunds (id INTEGER PRIMARY KEY, order_id INTEGER, customer_id INTEGER)")
        # order_id 都能在 orders 里找到；customer_id 全是不存在的编号（只是名字像）
        writer.executemany("INSERT INTO refunds (order_id, customer_id) VALUES (?, ?)", [(i, 90000 + i) for i in range(1, 40)])
        writer.commit()
        rels = Profiler(rodb).infer_relations(read_structures(rodb), targets={"refunds"})
        got = {(r.column, r.ref_table, r.source) for r in rels["refunds"]}
        assert got == {("order_id", "orders", "inferred")}

    def test_shared_key_family_points_to_master_table(self, rodb, writer):
        """BIRD 式一对一表：frpm.CDSCode、satscores.cds、events.cds_code 都应指向主表 schools.CDSCode。"""
        writer.execute('CREATE TABLE schools ("CDSCode" TEXT NOT NULL PRIMARY KEY, "County" TEXT)')
        writer.execute('CREATE TABLE frpm ("CDSCode" TEXT NOT NULL PRIMARY KEY, "Academic Year" TEXT)')
        writer.execute("CREATE TABLE satscores (cds TEXT NOT NULL PRIMARY KEY, rtype TEXT)")
        writer.execute("CREATE TABLE visits (visit_id INTEGER, cds_code TEXT, ts INTEGER)")
        codes = [f"{10000000 + i:014d}" for i in range(300)]
        writer.executemany("INSERT INTO schools VALUES (?, 'Alameda')", [(c,) for c in codes])
        writer.executemany("INSERT INTO frpm VALUES (?, '2014-2015')", [(c,) for c in codes[:200]])
        writer.executemany("INSERT INTO satscores VALUES (?, 'S')", [(c,) for c in codes[100:250]])
        writer.executemany("INSERT INTO visits VALUES (?, ?, ?)", [(i, codes[i % 300], 1600000000 + i) for i in range(900)])
        writer.commit()
        rels = Profiler(rodb).infer_relations(read_structures(rodb))
        got = {(t, r.column, r.ref_table) for t, rs in rels.items() for r in rs if r.source == "inferred"}
        assert {("frpm", "CDSCode", "schools"), ("satscores", "cds", "schools"), ("visits", "cds_code", "schools")} <= got
        assert not any(t == "schools" for t, _c, _r in got)  # 主表不反过来指向子表
        ts = Profiler(rodb).profile_table(read_structures(rodb)["visits"]).columns["ts"]
        assert ts.kind == "date" and ts.date_format == "Unix 时间戳（秒）"

    def test_long_text_gets_no_examples(self, rodb, writer):
        writer.execute("CREATE TABLE reviews (id INTEGER PRIMARY KEY, body TEXT)")
        writer.executemany("INSERT INTO reviews (body) VALUES (?)", [("很长的评论" * 30 + str(i),) for i in range(50)])
        writer.commit()
        prof = Profiler(rodb).profile_table(read_structures(rodb)["reviews"])
        assert prof.columns["body"].kind == "longtext" and not prof.columns["body"].examples

    def test_profiling_never_writes_business_db(self, rodb, dbfile, tmp_path):
        digest = hashlib.sha256(dbfile.read_bytes()).hexdigest()
        llm = RoutedLLM([("数据字典", DESCRIBE_REPLY)])
        cat = make_catalog(rodb, tmp_path, llm=llm)
        cat.sync(force=True)
        cat.refresh_values(list(cat.view().tables))
        assert hashlib.sha256(dbfile.read_bytes()).hexdigest() == digest
        assert (tmp_path / "catalog.sqlite").exists()  # 档案写在自己的库里


# ---------------------------------------------------------------- 增量同步


class TestIncrementalSync:
    def test_first_sync_then_noop(self, rodb, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        first = cat.sync()
        assert first.first and len(first.profiled) == 6
        version = cat.cards_version
        again = cat.sync()
        assert not again.structural and again.profiled == [] and cat.cards_version == version

    def test_data_changes_do_not_rebuild(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.sync()
        writer.executemany("INSERT INTO payments (order_id, amount, method, paid_at) VALUES (1, 9.9, 'card', '2025-07-02')", [()] * 5)
        writer.commit()
        assert cat.sync().profiled == []

    def test_add_column_rebuilds_only_that_table(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.sync()
        writer.execute("ALTER TABLE orders ADD COLUMN channel TEXT")
        writer.execute("UPDATE orders SET channel = CASE WHEN id % 2 = 0 THEN 'app' ELSE 'web' END")
        writer.commit()
        report = cat.sync()
        assert report.profiled == ["orders"] and report.changed["orders"].added_columns == ["channel"]
        assert "channel：取值 " in cat.view().render("orders")
        events = [d for _at, _k, d in cat.store.events(cat.key)]
        assert any("新增列 channel" in d for d in events)

    def test_new_table_gets_relation_and_existing_table_links_to_it(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.sync()
        writer.execute("ALTER TABLE customers ADD COLUMN region_id INTEGER")
        writer.execute("UPDATE customers SET region_id = 1 + id % 3")
        writer.commit()
        cat.sync()
        writer.execute("CREATE TABLE regions (id INTEGER PRIMARY KEY, name TEXT)")
        writer.executemany("INSERT INTO regions (name) VALUES (?)", [("华东",), ("华北",), ("华南",)])
        writer.commit()
        report = cat.sync()
        assert report.added == ["regions"] and report.profiled == ["regions"]  # customers 没有重建
        rels = cat.view().tables["customers"].relations
        assert ("region_id", "regions", "id") in {(r.column, r.ref_table, r.ref_column) for r in rels}

    def test_drop_table_removes_card_and_descriptions(self, rodb, writer, tmp_path):
        add_coupons(writer)
        cat = make_catalog(rodb, tmp_path, llm=RoutedLLM([("数据字典", DESCRIBE_REPLY)]))
        cat.sync()
        assert cat.view().description("coupons")[1] == "llm"
        writer.execute("DROP TABLE coupons")
        writer.commit()
        report = cat.sync()
        assert report.dropped == ["coupons"] and "coupons" not in cat.view().tables
        assert ("coupons", "") not in cat.view().descriptions

    def test_allowed_tables_only(self, rodb, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.attach(allowed=lambda: {"orders", "customers"})
        cat.sync()
        assert set(cat.view().tables) == {"orders", "customers"}

    def test_background_sync_and_wait(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.sync()
        writer.execute("ALTER TABLE products ADD COLUMN brand TEXT")
        writer.commit()
        cat.sync_in_background()
        cat.wait(10)
        assert read_structures(rodb)["products"].fingerprint() == cat.view().tables["products"].fingerprint

    def test_one_table_failing_does_not_block_others(self, rodb, tmp_path, monkeypatch):
        cat = make_catalog(rodb, tmp_path)
        real = cat.profiler.profile_table

        def flaky(structure, keys=None):
            if structure.name == "payments":
                raise RuntimeError("坏表")
            return real(structure, keys)

        monkeypatch.setattr(cat.profiler, "profile_table", flaky)
        report = cat.sync()
        assert "payments" not in report.profiled and len(report.profiled) == 5
        again = cat.sync()  # 退避期内不重试，也不重复报"新增表"
        assert again.profiled == [] and again.added == []
        assert sum("payments 建档失败" in d for _at, _k, d in cat.store.events(cat.key)) == 1
        monkeypatch.setattr(cat.profiler, "profile_table", real)
        cat.store.set_meta(cat.key, "fail:payments", json.dumps({"n": 1, "next": 0}))  # 退避到期
        assert cat.sync().profiled == ["payments"]
        assert cat.store.meta_prefix(cat.key, "fail:") == {}

    def test_value_refresh_detects_structure_change(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.sync()
        writer.execute("ALTER TABLE orders DROP COLUMN status")
        writer.commit()
        lines = cat.refresh_values(["orders"])  # 到期刷新时结构已经变了：改走完整同步
        assert any("删除列 status" in x for x in lines)
        assert not cat.view().tables["orders"].structure.has_column("status")

    def test_background_failure_is_recorded_not_raised(self, rodb, tmp_path, monkeypatch):
        cat = make_catalog(rodb, tmp_path)
        monkeypatch.setattr(cat, "sync", lambda force=False: (_ for _ in ()).throw(RuntimeError("boom")))
        cat.sync_in_background()
        cat.wait(10)
        assert "boom" in cat.last_error
        assert any("boom" in d for _at, _k, d in cat.store.events(cat.key))


# ---------------------------------------------------------------- 依赖索引与复核


class TestDependencies:
    def test_sql_deps_resolve_aliases_and_values(self, rodb):
        st = read_structures(rodb)
        deps = sql_deps(
            "SELECT c.city, COUNT(*) AS n FROM orders o JOIN customers c ON o.customer_id = c.id "
            "WHERE o.status = 'completed' GROUP BY c.city ORDER BY n DESC",
            st,
        )
        assert {"orders", "customers", "orders.status", "customers.city", "orders.customer_id"} <= deps.refs
        assert not any(r.endswith(".n") for r in deps.refs)  # SELECT 别名不是依赖
        assert ("orders", "status", "completed") in deps.values

    def test_text_deps_only_confident_names(self, rodb):
        st = read_structures(rodb)
        deps = text_deps("成交额用 order_items.quantity * unit_price，渠道看 payments 的 method；状态 status = 'completed'", st)
        assert {"order_items.quantity", "order_items.unit_price", "payments", "payments.method", "orders.status"} <= deps.refs
        assert ("orders", "status", "completed") in deps.values
        assert text_deps("我说的客户默认指钻石会员", st).refs == set()  # 纯中文：认不出依赖，也就不会误伤
        assert "customers.name" not in text_deps("name 字段", st).refs  # 通用列名不认

    def test_drop_column_flags_and_readd_restores(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.attach(
            glossary=glossary(("支付方式", "看 payments.method：alipay / wechat / card")),
            examples=examples(("各支付方式的金额", "SELECT method, SUM(amount) FROM payments GROUP BY method")),
        )
        cat.sync()
        writer.execute("ALTER TABLE payments DROP COLUMN method")
        writer.commit()
        report = cat.sync()
        assert cat.excluded("glossary") == {"支付方式"} and cat.excluded("example") == {"各支付方式的金额"}
        assert any("payments.method 已不存在" in f for f in report.flagged)
        assert any("试编译失败" in f for f in report.flagged)  # 例句靠试编译（LIMIT 0，不取数）
        writer.execute("ALTER TABLE payments ADD COLUMN method TEXT")
        writer.commit()
        report = cat.sync()
        assert cat.excluded("glossary") == set() and cat.excluded("example") == set()
        assert len(report.restored) == 2

    def test_rename_column_flags_dependents(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.attach(examples=examples(("各城市客户数", "SELECT city, COUNT(*) FROM customers GROUP BY city")))
        cat.sync()
        writer.execute("ALTER TABLE customers RENAME COLUMN city TO city_name")
        writer.commit()
        report = cat.sync()
        assert report.changed["customers"].renamed == [("city", "city_name")]
        assert cat.excluded("example") == {"各城市客户数"}

    def test_unrelated_change_does_not_touch_items(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.attach(examples=examples(("订单数", "SELECT COUNT(*) FROM orders")))
        cat.sync()
        calls = []
        real = cat.profiler.dry_run
        cat.profiler.dry_run = lambda sql, allowed: calls.append(sql) or real(sql, allowed)
        writer.execute("ALTER TABLE products ADD COLUMN brand TEXT")
        writer.commit()
        cat.sync()
        assert calls == []  # 只复核依赖变更表的条目

    def test_value_disappears_then_reappears(self, rodb, writer, tmp_path):
        mem = MemoryStore(tmp_path / "mem.sqlite")
        note_id = mem.remember("u1", "我说的成交订单指 orders.status = 'completed'")
        cat = make_catalog(rodb, tmp_path)
        cat.attach(memory=mem)
        cat.sync()
        writer.execute("UPDATE orders SET status = 'done' WHERE status = 'completed'")
        writer.commit()
        changes = cat.refresh_values(["orders"])
        assert any("新取值 done" in c for c in changes) and any("不再出现取值 completed" in c for c in changes)
        assert cat.excluded("memory") == {str(note_id)}
        reason = cat.review_items()[0].reason
        assert "'completed'" in reason and "orders.status" in reason
        writer.execute("UPDATE orders SET status = 'completed' WHERE status = 'done'")
        writer.commit()
        cat.refresh_values(["orders"])
        assert cat.excluded("memory") == set()

    def test_values_that_never_existed_are_not_tracked(self, rodb, tmp_path):
        mem = MemoryStore(tmp_path / "mem.sqlite")
        mem.remember("u1", "退款订单是 orders.status = 'refunded'")  # 库里本来就没有 refunded
        cat = make_catalog(rodb, tmp_path)
        cat.attach(memory=mem)
        cat.sync()
        item = cat.store.items(cat.key, "memory")[0]
        assert item.vals == [] and item.status == "active"

    def test_reference_to_missing_column_flagged_on_registration(self, rodb, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.attach(glossary=glossary(("退款", "看 orders.refund_flag")))
        report = cat.sync()
        assert cat.excluded("glossary") == {"退款"} and "orders.refund_flag" in report.flagged[0]

    def test_edited_and_deleted_sources(self, rodb, tmp_path):
        mem = MemoryStore(tmp_path / "mem.sqlite")
        keep = mem.remember("u1", "只看 customers.city 为上海的")
        gone = mem.remember("u1", "临时口径")
        cat = make_catalog(rodb, tmp_path)
        cat.attach(memory=mem, glossary=glossary(("退款", "看 orders.refund_flag")))
        cat.sync()
        assert cat.excluded("glossary") == {"退款"}
        cat.attach(glossary=glossary(("退款", "看 orders.status")))  # 口径文件改好了
        mem.forget("u1", gone)
        cat.sync()
        assert cat.excluded("glossary") == set()
        assert [i.key for i in cat.store.items(cat.key, "memory")] == [str(keep)]

    def test_confirm_restores_and_recomputes(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.attach(glossary=glossary(("城市", "看 customers.city")))
        cat.sync()
        writer.execute("ALTER TABLE customers RENAME COLUMN city TO city_name")
        writer.commit()
        cat.sync()
        item = cat.review_items()[0]
        confirmed = cat.confirm(item.id)
        assert confirmed.status == "active" and "customers.city" not in confirmed.refs
        assert cat.excluded("glossary") == set()
        assert cat.confirm(999999) is None

    def test_missing_refs(self, rodb):
        st = read_structures(rodb)
        assert missing_refs(["orders", "orders.status", "orders.nope", "ghost", "ghost.x"], st) == [
            "orders.nope", "ghost", "ghost.x"
        ]


# ---------------------------------------------------------------- 中文说明


class TestDescriptions:
    def test_llm_fills_only_missing_and_marks_unconfirmed(self, rodb, writer, tmp_path):
        add_coupons(writer)
        llm = RoutedLLM([("数据字典", DESCRIBE_REPLY)])
        cat = make_catalog(rodb, tmp_path, llm=llm)
        report = cat.sync()
        asked = [m for m in llm.calls if "数据字典" in m[0]["content"]]
        assert len(asked) == 1 and "coupons" in asked[0][1]["content"]  # 演示表都有注释，不调用模型
        assert "order_id" not in asked[0][1]["content"].split("请为这些写说明")[1]  # 关联列不用解释
        assert report.described == 4  # 表 + code + kind + expire_on；"不确定"的丢掉
        view = cat.view()
        assert view.table_note("coupons") == "优惠券表（待确认）"
        block = view.render("coupons")
        assert "code：优惠券编码（待确认）" in block and "discount：" not in block

    def test_human_description_wins_and_survives(self, rodb, writer, tmp_path):
        add_coupons(writer)
        cat = make_catalog(rodb, tmp_path, llm=RoutedLLM([("数据字典", DESCRIBE_REPLY)]))
        cat.sync()
        cat.set_description("coupons", "kind", "券类型：满减 / 折扣")
        cat.set_description("orders", "", "订单主表（人工）")  # 有建表注释的表，人工说明同样优先
        writer.execute("ALTER TABLE coupons ADD COLUMN channel TEXT")
        writer.commit()
        cat.sync()  # 重建 coupons、再次补说明
        view = cat.view()
        assert view.description("coupons", "kind") == ("券类型：满减 / 折扣", "human")
        assert "kind：券类型：满减 / 折扣；" in view.render("coupons")
        assert view.table_note("orders") == "订单主表（人工）"

    def test_llm_failure_does_not_break_sync(self, rodb, writer, tmp_path):
        add_coupons(writer)

        class Broken(MockLLM):
            def chat(self, *a, **k):
                raise LLMError("上游 503")

        cat = make_catalog(rodb, tmp_path, llm=Broken(["x"]))
        report = cat.sync()
        assert report.describe_failed == 1 and "coupons" in report.profiled
        assert any("上游 503" in d for _at, _k, d in cat.store.events(cat.key))

    def test_ddl_comment_removed(self, rodb, writer, tmp_path):
        writer.execute("CREATE TABLE t (\n    id INTEGER PRIMARY KEY,\n    a TEXT  -- 旧注释\n)")
        writer.commit()
        cat = make_catalog(rodb, tmp_path)
        cat.sync()
        assert cat.view().description("t", "a") == ("旧注释", "ddl")
        writer.execute("DROP TABLE t")
        writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
        writer.commit()
        cat.sync()
        assert cat.view().description("t", "a") == ("", "")


# ---------------------------------------------------------------- 接入 Agent


def agent_settings(dbfile, tmp_path, **kw):
    return Settings(
        _env_file=None,
        db_path=str(dbfile),
        memory_db_path=str(tmp_path / "memory.sqlite"),
        run_log_path=str(tmp_path / "runs.sqlite"),
        chart_out_dir=str(tmp_path / "charts"),
        catalog_db_path=str(tmp_path / "catalog.sqlite"),
        web_dist=str(tmp_path / "no-dist"),
        glossary_path=str(tmp_path / "no-glossary.jsonl"),  # 不带演示库的口径，依赖索引里只有测试自己放的条目
        examples_path=str(tmp_path / "no-examples.jsonl"),
        **kw,
    )


def sql_reply(sql):
    return f"思路。\n```sql\n{sql}\n```"


class TestAgentIntegration:
    def test_prompt_carries_profile(self, dbfile, rodb, writer, tmp_path):
        add_coupons(writer)
        settings = agent_settings(dbfile, tmp_path)
        llm = RoutedLLM([("数据字典", DESCRIBE_REPLY)], default=sql_reply("SELECT COUNT(*) FROM coupons"))
        cat = Catalog.from_settings(settings, rodb, llm=llm)
        agent = DeepQuery(settings, rodb, llm, catalog=cat)
        cat.sync()
        agent.ask("优惠券有多少张", generate_answer=False)
        prompt = llm.calls[-1][1]["content"]
        assert "coupons 档案" in prompt and "kind：优惠类型（待确认）；取值 折扣 / 满减" in prompt
        assert "status：取值 " in prompt and "关联 orders.id" in prompt

    def test_disclose_catalog_uses_llm_note(self, dbfile, rodb, writer, tmp_path):
        add_coupons(writer)
        settings = agent_settings(dbfile, tmp_path, schema_rag="disclose")
        llm = RoutedLLM(
            [("数据字典", DESCRIBE_REPLY), ("表目录", "```tables\ncoupons\n```")],
            default=sql_reply("SELECT COUNT(*) FROM coupons"),
        )
        cat = Catalog.from_settings(settings, rodb, llm=llm)
        agent = DeepQuery(settings, rodb, llm, catalog=cat)
        cat.sync()
        agent.ask("优惠券有多少张", generate_answer=False)
        browse = next(m for m in llm.calls if "表目录" in m[0]["content"])
        assert "coupons：优惠券表（待确认）" in browse[1]["content"]

    def test_schema_change_picked_up_without_blocking(self, dbfile, rodb, writer, tmp_path):
        settings = agent_settings(dbfile, tmp_path)
        llm = RoutedLLM([], default=sql_reply("SELECT COUNT(*) FROM orders"))
        cat = Catalog.from_settings(settings, rodb)
        agent = DeepQuery(settings, rodb, llm, catalog=cat)
        cat.sync()
        token = agent.maybe_refresh_schema()
        writer.execute("ALTER TABLE orders ADD COLUMN channel TEXT")
        writer.execute("UPDATE orders SET channel = 'app'")
        writer.commit()
        out = agent.ask("订单数", generate_answer=False)  # 结构刚变：照常回答，不等建档
        assert out.status == "ok"
        cat.wait(10)
        agent.ask("订单数", generate_answer=False)
        assert "channel：取值 app" in llm.calls[-1][1]["content"]
        assert agent.maybe_refresh_schema() != token  # 缓存 key 跟着变

    def test_flagged_items_not_recalled(self, dbfile, rodb, writer, tmp_path):
        settings = agent_settings(dbfile, tmp_path)
        mem = MemoryStore(settings.memory_db_path)
        mem.remember("u1", "支付渠道一律看 payments.method")
        llm = RoutedLLM([], default=sql_reply("SELECT COUNT(*) FROM payments"))
        cat = Catalog.from_settings(settings, rodb)
        agent = DeepQuery(settings, rodb, llm, memory=mem, catalog=cat)
        cat.sync()
        agent.ask("各支付渠道的笔数", generate_answer=False, user_id="u1")
        assert "payments.method" in llm.calls[-1][1]["content"]
        writer.execute("ALTER TABLE payments DROP COLUMN method")
        writer.commit()
        agent.maybe_refresh_schema()
        cat.wait(10)
        agent.ask("各支付渠道的笔数", generate_answer=False, user_id="u1")
        assert "支付渠道一律看" not in llm.calls[-1][1]["content"]

    def test_new_memory_registered_at_question_time(self, dbfile, rodb, tmp_path):
        settings = agent_settings(dbfile, tmp_path)
        mem = MemoryStore(settings.memory_db_path)
        llm = RoutedLLM([], default=sql_reply("SELECT COUNT(*) FROM orders"))
        cat = Catalog.from_settings(settings, rodb)
        agent = DeepQuery(settings, rodb, llm, memory=mem, catalog=cat)
        cat.sync()
        mem.remember("u1", "看 orders.refund_flag")  # 引用了不存在的列：登记时就标出来
        agent.ask("订单数", generate_answer=False, user_id="u1")
        assert "refund_flag" not in llm.calls[-1][1]["content"]
        assert cat.store.items(cat.key, "memory")[0].status == "review"

    def test_probe_feeds_new_values_back(self, dbfile, rodb, writer, tmp_path):
        settings = agent_settings(dbfile, tmp_path)
        cat = Catalog.from_settings(settings, rodb)
        cat.sync()
        writer.execute("UPDATE orders SET status = 'refunded' WHERE id <= 5")
        writer.commit()
        llm = RoutedLLM(
            [("核对", sql_reply("SELECT COUNT(*) FROM orders WHERE status = 'refunded'"))],
            default=sql_reply("SELECT COUNT(*) FROM orders WHERE status = '已退款'"),
        )
        agent = DeepQuery(settings, rodb, llm, catalog=cat)
        agent.ask("退款订单有多少", generate_answer=False)  # 空结果 → 修复前查真实取值 → 回写档案
        assert "refunded" in cat.view().tables["orders"].profile.columns["status"].values
        assert any("新取值 refunded" in d for _at, _k, d in cat.store.events(cat.key))

    def test_tick_refreshes_stale_values_in_background(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path, refresh_hours=1)
        cat.sync()
        cat.refresh_seconds = 0.001  # 立刻到期
        writer.execute("UPDATE payments SET method = 'paypal' WHERE id <= 3")
        writer.commit()
        cat.tick()
        cat.wait(10)
        assert "paypal" in cat.view().tables["payments"].profile.columns["method"].values

    def test_build_agent_wires_catalog(self, dbfile, tmp_path):
        from deepquery import build_agent

        on = agent_settings(dbfile, tmp_path, llm_mock=True)
        agent = build_agent(on, wait_catalog=True)
        assert agent.catalog is not None and agent.catalog.llm is None  # mock 模式不让假模型写说明
        assert agent.catalog.status()["profiled"] == 6
        off = agent_settings(dbfile, tmp_path, llm_mock=True, catalog_enabled=False)
        assert build_agent(off).catalog is None

    def test_unwritable_catalog_path_disables_catalog(self, dbfile, tmp_path):
        from deepquery import build_agent

        blocker = tmp_path / "file"
        blocker.write_text("x")
        settings = agent_settings(dbfile, tmp_path, llm_mock=True).model_copy(
            update={"catalog_db_path": str(blocker / "catalog.sqlite")}
        )
        assert build_agent(settings).catalog is None  # 档案库打不开：退回不建档，照常提问


class TestServerAndCli:
    def test_api_catalog_hides_other_users_memories(self, dbfile, rodb, writer, tmp_path):
        settings = agent_settings(dbfile, tmp_path)
        mem = MemoryStore(settings.memory_db_path)
        mine = mem.remember("alice", "看 payments.method")
        mem.remember("bob", "bob 的口径：payments.method")
        cat = Catalog.from_settings(settings, rodb)
        agent = DeepQuery(settings, rodb, MockLLM(["x"]), memory=mem, catalog=cat)
        cat.sync()
        writer.execute("ALTER TABLE payments DROP COLUMN method")
        writer.commit()
        cat.sync()
        client = TestClient(create_app(agent=agent, settings=settings))
        body = client.get("/api/catalog", params={"user": "alice"}).json()
        assert body["enabled"] and body["status"]["profiled"] == 6
        texts = [r["text"] for r in body["review"]]
        assert texts == ["看 payments.method"]
        assert all("bob 的口径" not in e["detail"] for e in body["events"])  # 事件里只有编号，没有记忆原文
        notes = client.get("/api/memory", params={"user": "alice"}).json()["notes"]
        assert notes[0]["id"] == mine and "payments.method" in notes[0]["review"]

    def test_api_catalog_disabled(self, settings, db):
        client = TestClient(create_app(agent=DeepQuery(settings, db, MockLLM(["x"])), settings=settings))
        assert client.get("/api/catalog").json() == {"enabled": False}

    def test_cli_build_show_describe_review_confirm(self, dbfile, writer, tmp_path, monkeypatch, capsys):
        from deepquery import cli
        from deepquery.config import get_settings

        for k, v in {
            "DB_PATH": str(dbfile),
            "CATALOG_DB_PATH": str(tmp_path / "catalog.sqlite"),
            "MEMORY_DB_PATH": str(tmp_path / "memory.sqlite"),
            "GLOSSARY_PATH": str(tmp_path / "none.jsonl"),
            "EXAMPLES_PATH": str(tmp_path / "none.jsonl"),
            "LLM_API_KEY": "",
        }.items():
            monkeypatch.setenv(k, v)
        monkeypatch.chdir(tmp_path)  # 不读仓库里的 .env
        get_settings.cache_clear()

        def run(*argv):
            monkeypatch.setattr(sys, "argv", ["deepquery", *argv])
            with pytest.raises(SystemExit) as e:
                cli.main()
            return e.value.code, capsys.readouterr().out

        try:
            MemoryStore(tmp_path / "memory.sqlite").remember("default", "看 customers.city")
            code, out = run("catalog", "build")
            assert code == 0 and "首次接入" in out
            code, out = run("catalog", "show", "orders")
            assert code == 0 and "status：取值" in out
            code, out = run("catalog", "describe", "orders.status", "订单状态（人工）")
            assert code == 0
            writer.execute("ALTER TABLE customers RENAME COLUMN city TO city_name")
            writer.commit()
            code, out = run("catalog", "build")
            assert "待复核" in out
            code, out = run("memories")
            assert "待复核" in out
            code, out = run("catalog", "review")
            item_id = int(out.split("#", 1)[1].split(" ", 1)[0])
            code, out = run("catalog", "confirm", str(item_id))
            assert code == 0
            code, out = run("catalog", "status")
            assert "人工 1 条" in out and "待复核" not in out.split("依赖索引")[1].split("\n")[0]
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------- 评审发现的问题（回归测试）


class TestReviewRegressions:
    def test_partial_value_check_does_not_restore(self, rodb, writer, tmp_path):
        """依赖两张表的取值：只刷新没问题的那张表，不能把条目恢复。"""
        mem = MemoryStore(tmp_path / "mem.sqlite")
        mem.remember("u1", "看 orders.status = 'completed' 并且 payments.method = 'card'")
        cat = make_catalog(rodb, tmp_path)
        cat.attach(memory=mem)
        cat.sync()
        writer.execute("UPDATE payments SET method = 'visa' WHERE method = 'card'")
        writer.commit()
        cat.refresh_values(["payments"])
        assert cat.excluded("memory") == {"1"}
        cat.refresh_values(["orders"])  # 只看到 orders 的取值都在
        assert cat.excluded("memory") == {"1"}

    def test_value_review_escalates_to_schema(self, rodb, writer, tmp_path):
        """先因取值待复核，再删列：原因升级成 schema，之后不能被"另一个取值还在"恢复。"""
        mem = MemoryStore(tmp_path / "mem.sqlite")
        mem.remember("u1", "看 orders.status = 'completed' 并且 payments.method = 'card'")
        cat = make_catalog(rodb, tmp_path)
        cat.attach(memory=mem)
        cat.sync()
        writer.execute("UPDATE payments SET method = 'visa' WHERE method = 'card'")
        writer.commit()
        cat.refresh_values(["payments"])
        writer.execute("ALTER TABLE payments DROP COLUMN method")
        writer.commit()
        cat.sync()
        cat.refresh_values(["orders"])
        item = cat.review_items()[0]
        assert item.reason_kind == "schema" and "payments.method" in item.reason

    def test_interrupted_sync_still_reviews_next_time(self, rodb, writer, tmp_path, monkeypatch):
        cat = make_catalog(rodb, tmp_path)
        cat.attach(glossary=glossary(("支付方式", "看 payments.method")))
        cat.sync()
        writer.execute("ALTER TABLE payments DROP COLUMN method")
        writer.commit()
        real = cat._review
        monkeypatch.setattr(cat, "_review", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            cat.sync()  # 新指纹已经存下，复核前被打断
        monkeypatch.setattr(cat, "_review", real)
        report = cat.sync()
        assert report.changed == {}  # 指纹已一致
        assert cat.excluded("glossary") == {"支付方式"}  # 但复核没有漏掉

    def test_events_do_not_carry_user_ids(self, rodb, writer, tmp_path):
        mem = MemoryStore(tmp_path / "mem.sqlite")
        mem.remember("visitor-7f3a", "看 payments.method")
        cat = make_catalog(rodb, tmp_path)
        cat.attach(memory=mem)
        cat.sync()
        writer.execute("ALTER TABLE payments DROP COLUMN method")
        writer.commit()
        report = cat.sync()
        texts = [d for _at, _k, d in cat.store.events(cat.key)] + report.lines()
        assert not any("visitor-7f3a" in t for t in texts)

    def test_other_process_changes_become_visible(self, rodb, writer, tmp_path):
        server = make_catalog(rodb, tmp_path)
        server.attach(glossary=glossary(("支付方式", "看 payments.method")))
        server.sync()
        assert server.excluded("glossary") == set()
        cli = make_catalog(rodb, tmp_path)  # 另一个进程：同一个档案库
        cli.attach(glossary=glossary(("支付方式", "看 payments.method")))
        cli.set_description("orders", "status", "订单状态（人工）")
        writer.execute("ALTER TABLE payments DROP COLUMN method")
        writer.commit()
        cli.sync()
        server.poll(force=True)
        assert server.excluded("glossary") == {"支付方式"}
        assert server.view().description("orders", "status") == ("订单状态（人工）", "human")
        assert server.sync().changed == {}  # 结构已同步过：另一个进程看到的结果一致

    def test_count_failure_is_not_saved_as_empty_table(self, rodb, tmp_path, monkeypatch):
        cat = make_catalog(rodb, tmp_path)
        real = cat.profiler.bounded_count
        monkeypatch.setattr(cat.profiler, "bounded_count", lambda t, cap: None if t == "orders" else real(t, cap))
        report = cat.sync()
        assert "orders" not in report.profiled and "orders" not in cat.view().tables
        monkeypatch.setattr(cat.profiler, "bounded_count", real)
        assert cat.sync(force=True).profiled.count("orders") == 1  # 强制重建不受退避限制

    def test_view_built_during_bump_is_not_cached(self, rodb, tmp_path, monkeypatch):
        cat = make_catalog(rodb, tmp_path)
        cat.sync()
        real = cat.store.tables

        def bump_while_reading(key):
            out = real(key)
            cat._bump_cards()  # 读的过程中另一个线程改了档案
            return out

        monkeypatch.setattr(cat.store, "tables", bump_while_reading)
        first = cat.view()
        monkeypatch.setattr(cat.store, "tables", real)
        assert first.version != cat.cards_version
        assert cat.view().version == cat.cards_version  # 混杂的那份没被缓存

    def test_stale_card_not_attached_to_new_ddl(self, dbfile, rodb, writer, tmp_path):
        settings = agent_settings(dbfile, tmp_path)
        cat = Catalog.from_settings(settings, rodb, auto_background=False)
        cat.sync()
        llm = RoutedLLM([], default=sql_reply("SELECT COUNT(*) FROM orders"))
        agent = DeepQuery(settings, rodb, llm, catalog=cat)
        writer.execute("ALTER TABLE orders RENAME COLUMN status TO state")
        writer.commit()
        agent.ask("订单数", generate_answer=False)  # 结构刚变、还没重新建档
        prompt = llm.calls[-1][1]["content"]
        assert "orders 档案" not in prompt and "customers 档案" in prompt

    def test_catalog_store_errors_do_not_break_questions(self, dbfile, rodb, tmp_path, monkeypatch):
        settings = agent_settings(dbfile, tmp_path)
        mem = MemoryStore(settings.memory_db_path)
        mem.remember("u1", "看 orders.status")
        cat = Catalog.from_settings(settings, rodb)
        cat.sync()
        agent = DeepQuery(settings, rodb, RoutedLLM([], default=sql_reply("SELECT COUNT(*) FROM orders")), memory=mem, catalog=cat)

        def locked(*a, **k):
            raise sqlite3.OperationalError("database is locked")

        for name in ("tables", "items", "flagged", "versions", "descriptions"):
            monkeypatch.setattr(cat.store, name, locked)
        cat._view = None
        cat._last_poll = 0.0
        out = agent.ask("订单数", generate_answer=False, user_id="u1")
        assert out.status == "ok" and "locked" in cat.last_error

    def test_question_path_never_queries_business_db_for_catalog(self, dbfile, rodb, writer, tmp_path, monkeypatch):
        settings = agent_settings(dbfile, tmp_path)
        mem = MemoryStore(settings.memory_db_path)
        cat = Catalog.from_settings(settings, rodb, auto_background=False)
        agent = DeepQuery(settings, rodb, RoutedLLM([], default=sql_reply("SELECT 1 FROM orders")), memory=mem, catalog=cat)
        cat.sync()
        agent.maybe_refresh_schema()
        mem.remember("u1", "看 orders.status = 'completed' 和 payments.method IN ('card', 'alipay', 'nope')")
        queries, introspect = [], []
        real_q, real_s = rodb.run_query, rodb.schema_by_table
        monkeypatch.setattr(rodb, "run_query", lambda sql: queries.append(sql) or real_q(sql))
        monkeypatch.setattr(rodb, "schema_by_table", lambda *a, **k: introspect.append(1) or real_s(*a, **k))
        agent._build_schema_context("订单数", agent._snap, user_id="u1")
        assert queries == []  # 新记忆的取值依赖先记为待核对
        item = cat.store.items(cat.key, "memory")[0]
        assert item.cand and not item.vals
        cat.set_description("orders", "", "订单（人工）")  # 档案变了：只重新附档案，不再自省业务库
        agent.maybe_refresh_schema()
        assert introspect == [] and "订单（人工）" in agent._snap.catalog["orders"]
        monkeypatch.setattr(rodb, "run_query", real_q)
        cat.sync()  # 后台 / 下次同步再核对：库里有的才跟踪
        item = cat.store.items(cat.key, "memory")[0]
        assert not item.cand and ["payments", "method", "nope"] not in item.vals
        assert ["orders", "status", "completed"] in item.vals

    def test_text_deps_ignore_files_and_urls(self, rodb):
        st = read_structures(rodb)
        deps = text_deps("数据导出到 orders.xlsx，说明见 https://customers.example.com/orders.md，联系 a@payments.io", st)
        assert not any(r.startswith("orders.") or r.startswith("customers.") or r.startswith("payments.") for r in deps.refs)

    def test_negated_filters_are_not_value_deps(self, rodb):
        st = read_structures(rodb)
        deps = sql_deps("SELECT COUNT(*) FROM orders WHERE status NOT IN ('cancelled') AND NOT (status = 'pending')", st)
        assert deps.values == set()

    def test_transient_dry_run_failure_does_not_flag(self, rodb, writer, tmp_path, monkeypatch):
        from deepquery.tools.contract import QueryResult

        cat = make_catalog(rodb, tmp_path)
        cat.attach(examples=examples(("订单数", "SELECT COUNT(*) FROM orders")))
        cat.sync()
        writer.execute("ALTER TABLE orders ADD COLUMN channel TEXT")
        writer.commit()
        real = rodb.run_query

        def flaky(sql):
            if "LIMIT 0" in sql:
                return QueryResult(ok=False, error_kind="timeout", error_message="interrupted")
            return real(sql)

        monkeypatch.setattr(rodb, "run_query", flaky)
        cat.sync()
        assert cat.excluded("example") == set()

    def test_relation_repointed_when_master_dropped(self, rodb, writer, tmp_path):
        writer.execute('CREATE TABLE schools ("CDSCode" TEXT NOT NULL PRIMARY KEY)')
        writer.execute('CREATE TABLE frpm ("CDSCode" TEXT NOT NULL PRIMARY KEY)')
        writer.execute("CREATE TABLE satscores (cds TEXT NOT NULL PRIMARY KEY)")
        codes = [f"{i:06d}" for i in range(100)]
        writer.executemany("INSERT INTO schools VALUES (?)", [(c,) for c in codes])
        writer.executemany("INSERT INTO frpm VALUES (?)", [(c,) for c in codes[:80]])
        writer.executemany("INSERT INTO satscores VALUES (?)", [(c,) for c in codes[:50]])
        writer.commit()
        cat = make_catalog(rodb, tmp_path)
        cat.sync()
        assert [r.ref_table for r in cat.view().tables["satscores"].relations] == ["schools"]
        writer.execute("DROP TABLE schools")
        writer.commit()
        cat.sync()
        assert [r.ref_table for r in cat.view().tables["satscores"].relations] == ["frpm"]

    def test_human_description_survives_drop_and_recreate(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.sync()
        cat.set_description("payments", "method", "支付渠道（人工）")
        writer.execute("ALTER TABLE payments DROP COLUMN method")
        writer.commit()
        cat.sync()
        writer.execute("ALTER TABLE payments ADD COLUMN method TEXT")
        writer.commit()
        cat.sync()
        assert cat.view().description("payments", "method") == ("支付渠道（人工）", "human")

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_background_worker_resets_after_base_exception(self, rodb, tmp_path, monkeypatch):
        cat = make_catalog(rodb, tmp_path)

        def die(force=False):
            raise SystemExit("线程里的 BaseException")

        monkeypatch.setattr(cat, "sync", die)
        cat.sync_in_background()
        cat.wait(5)
        assert cat._worker is None  # 复位了，后面的任务还能起来
        monkeypatch.undo()
        cat.sync_in_background()
        cat.wait(10)
        assert cat.status()["profiled"] == 6


class TestSecondReview:
    def test_ambiguous_column_after_change_flags_example(self, rodb, writer, tmp_path):
        cat = make_catalog(rodb, tmp_path)
        cat.attach(examples=examples(("各城市订单数", "SELECT city, COUNT(*) FROM orders JOIN customers ON orders.customer_id = customers.id GROUP BY city")))
        cat.sync()
        writer.execute("ALTER TABLE orders ADD COLUMN city TEXT")  # 之后 city 有歧义
        writer.commit()
        cat.sync()
        assert cat.excluded("example") == {"各城市订单数"}

    def test_one_visitors_memory_churn_does_not_invalidate_everyones_cache(self, dbfile, rodb, tmp_path):
        settings = agent_settings(dbfile, tmp_path)
        mem = MemoryStore(settings.memory_db_path)
        cat = Catalog.from_settings(settings, rodb, auto_background=False)
        agent = DeepQuery(settings, rodb, RoutedLLM([], default=sql_reply("SELECT 1 FROM orders")), memory=mem, catalog=cat)
        cat.sync()
        token = agent.maybe_refresh_schema()
        nid = mem.remember("mallory", "随便记一条")
        agent._build_schema_context("x", agent._snap, user_id="mallory")
        mem.forget("mallory", nid)
        mem.remember("mallory", "看 orders.nope")  # 引用不存在的列：只影响 mallory 自己
        agent._build_schema_context("x", agent._snap, user_id="mallory")
        cat.sync()
        assert agent.maybe_refresh_schema() == token

    def test_memory_review_state_is_part_of_that_users_cache_scope(self, dbfile, rodb, writer, tmp_path):
        from deepquery.server import memory_scope

        settings = agent_settings(dbfile, tmp_path)
        mem = MemoryStore(settings.memory_db_path)
        mem.remember("alice", "看 payments.method")
        cat = Catalog.from_settings(settings, rodb, auto_background=False)
        agent = DeepQuery(settings, rodb, MockLLM(["x"]), memory=mem, catalog=cat)
        cat.sync()
        before = memory_scope(agent, "alice")
        writer.execute("ALTER TABLE payments DROP COLUMN method")
        writer.commit()
        cat.sync()
        assert memory_scope(agent, "alice") != before

    def test_concurrent_sync_from_another_process_is_skipped(self, rodb, tmp_path):
        a, b = make_catalog(rodb, tmp_path), make_catalog(rodb, tmp_path)
        assert a.store.acquire_lease(a.key, a._owner, 60)  # a 正在同步
        report = b.sync()
        assert report.skipped and report.lines() == ["另一个进程正在同步档案，这次跳过"]
        a.store.release_lease(a.key, a._owner)
        assert not b.sync().skipped

    def test_structure_only_change_reloads_prompt_ddl(self, dbfile, rodb, writer, tmp_path):
        """同步发现结构变化后，提问用的建表语句整份重读（MySQL/PG 只改主外键时提问指纹看不到）。"""
        settings = agent_settings(dbfile, tmp_path)
        cat = Catalog.from_settings(settings, rodb, auto_background=False)
        agent = DeepQuery(settings, rodb, MockLLM(["x"]), catalog=cat)
        cat.sync()
        agent.maybe_refresh_schema()
        rodb.schema_fingerprint = lambda: "frozen"  # 模拟提问指纹看不到这次变化
        agent._snap = replace(agent._snap, fingerprint="frozen")
        writer.execute("ALTER TABLE orders ADD COLUMN channel TEXT")
        writer.commit()
        cat.sync()
        cat._last_poll = 0.0
        agent.maybe_refresh_schema()
        assert "channel" in agent._snap.raw_docs["orders"]

    def test_old_catalog_file_is_migrated(self, tmp_path):
        path = tmp_path / "old.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE catalog_items (id INTEGER PRIMARY KEY AUTOINCREMENT, db_key TEXT NOT NULL, kind TEXT NOT NULL, "
            "key TEXT NOT NULL, user_id TEXT NOT NULL DEFAULT '', text TEXT NOT NULL, sql TEXT NOT NULL DEFAULT '', "
            "refs TEXT NOT NULL DEFAULT '[]', vals TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active', "
            "reason TEXT NOT NULL DEFAULT '', reason_kind TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL, "
            "UNIQUE (db_key, kind, key))"
        )
        conn.commit()
        conn.close()
        store = CatalogStore(path)
        assert store.items("k") == []

    def test_bare_domains_and_real_columns_named_like_extensions(self, rodb, writer):
        writer.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, log TEXT)")
        writer.commit()
        st = read_structures(rodb)
        assert text_deps("参考 customers.example.com 上的说明", st).refs == set()
        assert "events.log" in text_deps("看 events.log 字段", st).refs  # 真实存在的列照常认
