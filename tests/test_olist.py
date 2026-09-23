"""Olist 真实数据导入与内置数据集切换：用几行 CSV 模拟 Kaggle 压缩包，不联网。"""

import csv
import io
import json
import sqlite3
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from deepquery import datasets, olist_data
from deepquery.config import Settings
from deepquery.tools.database import ReadOnlyDatabase

O1, O2, O3 = "o1", "o2", "o3"
FIXTURE = {
    "olist_customers_dataset.csv": [
        ["customer_id", "customer_unique_id", "customer_zip_code_prefix", "customer_city", "customer_state"],
        ["c1", "u1", "01037", "sao paulo", "SP"],
        ["c2", "u1", "01037", "sao paulo", "SP"],  # 同一个人的第二个订单
        ["c3", "u2", "", "rio de janeiro", "RJ"],
    ],
    "olist_sellers_dataset.csv": [
        ["seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"],
        ["s1", "13023", "campinas", "SP"],
    ],
    "olist_products_dataset.csv": [
        ["product_id", "product_category_name", "product_name_lenght", "product_description_lenght",
         "product_photos_qty", "product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm"],
        ["p1", "beleza_saude", "40", "287", "1", "225", "16", "10", "14"],
        ["p2", "pc_gamer", "40", "287", "2", "1000.0", "30", "18", "20"],  # 官方对照表里缺的品类
        ["p3", "", "", "", "", "", "", "", ""],  # 没有品类的商品
    ],
    "olist_orders_dataset.csv": [
        ["order_id", "customer_id", "order_status", "order_purchase_timestamp", "order_approved_at",
         "order_delivered_carrier_date", "order_delivered_customer_date", "order_estimated_delivery_date"],
        [O1, "c1", "delivered", "2017-10-02 10:56:33", "2017-10-02 11:07:15", "2017-10-04 19:55:00",
         "2017-10-20 21:25:13", "2017-10-18 00:00:00"],  # 延迟送达
        [O2, "c2", "delivered", "2018-01-05 09:00:00", "2018-01-05 10:00:00", "2018-01-06 10:00:00",
         "2018-01-10 10:00:00", "2018-01-20 00:00:00"],
        [O3, "c3", "canceled", "2018-02-01 09:00:00", "", "", "", "2018-02-20 00:00:00"],
    ],
    "olist_order_items_dataset.csv": [
        ["order_id", "order_item_id", "product_id", "seller_id", "shipping_limit_date", "price", "freight_value"],
        [O1, "1", "p1", "s1", "2017-10-06 11:07:15", "58.90", "13.29"],
        [O1, "2", "p1", "s1", "2017-10-06 11:07:15", "58.90", "13.29"],
        [O2, "1", "p2", "s1", "2018-01-08 10:00:00", "239.90", "19.93"],
        [O3, "1", "p3", "s1", "2018-02-05 10:00:00", "10.00", "5.00"],
    ],
    "olist_order_payments_dataset.csv": [
        ["order_id", "payment_sequential", "payment_type", "payment_installments", "payment_value"],
        [O1, "1", "credit_card", "8", "100.00"],
        [O1, "2", "voucher", "1", "44.38"],
        [O2, "1", "boleto", "1", "259.83"],
    ],
    "olist_order_reviews_dataset.csv": [
        ["review_id", "order_id", "review_score", "review_comment_title", "review_comment_message",
         "review_creation_date", "review_answer_timestamp"],
        ["r1", O1, "1", "", "atrasou", "2017-10-21 00:00:00", "2017-10-22 10:00:00"],
        ["r2", O2, "5", "", "", "2018-01-11 00:00:00", "2018-01-12 10:00:00"],
    ],
    "product_category_name_translation.csv": [
        ["product_category_name", "product_category_name_english"],
        ["beleza_saude", "health_beauty"],
    ],
}


def _csv(rows) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue()


@pytest.fixture(scope="module")
def olist_zip(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("olist-src") / "brazilian-ecommerce.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, rows in FIXTURE.items():
            text = _csv(rows)
            if name.startswith("product_category"):
                text = "\ufeff" + text  # 官方对照表带 BOM
            zf.writestr(name, text)
    return path


@pytest.fixture(scope="module")
def olist_db(olist_zip, tmp_path_factory) -> Path:
    return olist_data.build(tmp_path_factory.mktemp("olist") / "olist.sqlite", src=olist_zip)


class TestImport:
    def test_tables_and_counts(self, olist_db):
        conn = sqlite3.connect(olist_db)
        count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
        assert count("states") == 27
        assert count("customers") == 3 and count("orders") == 3 and count("order_items") == 4
        assert count("payments") == 3 and count("reviews") == 2 and count("sellers") == 1
        # 客户人数必须按 unique_id 去重：3 个订单级 id 只对应 2 个人
        assert conn.execute("SELECT COUNT(DISTINCT unique_id) FROM customers").fetchone()[0] == 2

    def test_categories_get_chinese_names(self, olist_db):
        conn = sqlite3.connect(olist_db)
        rows = dict(conn.execute("SELECT name, name_zh FROM categories").fetchall())
        assert rows["beleza_saude"] == "美妆健康"
        assert rows["pc_gamer"] == "游戏电脑"  # 对照表缺失的品类也有中英文名
        en = conn.execute("SELECT name_en FROM categories WHERE name = 'pc_gamer'").fetchone()[0]
        assert en == "pc_gamer"
        assert conn.execute("SELECT category FROM products WHERE id = 'p3'").fetchone()[0] is None

    def test_empty_values_become_null_and_numbers_are_typed(self, olist_db):
        conn = sqlite3.connect(olist_db)
        assert conn.execute("SELECT zip_prefix FROM customers WHERE id = 'c3'").fetchone()[0] is None
        assert conn.execute("SELECT delivered_at FROM orders WHERE id = 'o3'").fetchone()[0] is None
        assert conn.execute("SELECT weight_g FROM products WHERE id = 'p2'").fetchone()[0] == 1000
        assert conn.execute("SELECT SUM(price) FROM order_items").fetchone()[0] == pytest.approx(367.7)

    def test_foreign_keys_are_indexed(self, olist_db):
        conn = sqlite3.connect(olist_db)
        indexed = {r[0] for r in conn.execute("SELECT tbl_name FROM sqlite_master WHERE type = 'index'")}
        assert {"orders", "order_items", "customers", "reviews", "products"} <= indexed

    def test_directory_source_matches_zip(self, olist_zip, olist_db, tmp_path):
        with zipfile.ZipFile(olist_zip) as zf:
            zf.extractall(tmp_path / "src")
        again = olist_data.build(tmp_path / "olist.sqlite", src=tmp_path / "src")
        q = "SELECT COUNT(*), SUM(amount) FROM payments"
        assert sqlite3.connect(again).execute(q).fetchone() == sqlite3.connect(olist_db).execute(q).fetchone()

    def test_schema_comments_reach_the_model(self, olist_db):
        text = ReadOnlyDatabase(olist_db, timeout_seconds=5, max_rows=50).schema_text()
        assert "统计客户人数要用 unique_id 去重" in text
        assert "delivered 已送达" in text


class TestKnowledge:
    def test_examples_run_on_the_imported_schema(self, olist_db):
        db = ReadOnlyDatabase(olist_db, timeout_seconds=5, max_rows=50)
        lines = Path("eval/knowledge/olist/examples.jsonl").read_text(encoding="utf-8").splitlines()
        examples = [json.loads(line) for line in lines if line.strip() and not line.startswith("#")]
        assert examples
        for ex in examples:
            result = db.run_query(ex["sql"])
            assert result.ok, (ex["question"], result.error_message)

    def test_delay_example_matches_fixture(self, olist_db):
        db = ReadOnlyDatabase(olist_db, timeout_seconds=5, max_rows=50)
        sql = next(
            json.loads(line)["sql"]
            for line in Path("eval/knowledge/olist/examples.jsonl").read_text(encoding="utf-8").splitlines()
            if "延迟送达" in line
        )
        assert db.run_query(sql).rows[0][0] == 1  # 只有 o1 延迟，评分 1


class TestRegistry:
    def test_recognises_datasets_by_file_name(self):
        assert datasets.for_db("data/olist/olist.sqlite").key == "olist"
        assert datasets.for_db("/any/where/ecommerce.sqlite").key == "demo"
        assert datasets.for_db("mine.sqlite") is None
        assert datasets.for_db("mysql://u:p@h:3306/olist.sqlite") is None

    def test_knowledge_follows_dataset_unless_configured(self):
        olist = Settings(_env_file=None, db_path="data/olist/olist.sqlite")
        assert datasets.knowledge_paths(olist) == (
            "eval/knowledge/olist/glossary.jsonl",
            "eval/knowledge/olist/examples.jsonl",
        )
        pinned = olist.model_copy(update={"glossary_path": "my/glossary.jsonl"})
        assert datasets.knowledge_paths(pinned)[0] == "my/glossary.jsonl"
        demo = Settings(_env_file=None, db_path="data/demo/ecommerce.sqlite")
        assert datasets.knowledge_paths(demo) == (datasets.DEFAULT_GLOSSARY, datasets.DEFAULT_EXAMPLES)

    def test_agent_loads_olist_glossary(self, olist_db, tmp_path):
        from deepquery.agent import DeepQuery
        from deepquery.llm import MockLLM

        s = Settings(_env_file=None, db_path=str(olist_db), memory_db_path=str(tmp_path / "m.sqlite"))
        agent = DeepQuery(s, ReadOnlyDatabase(olist_db, timeout_seconds=5, max_rows=50), MockLLM(["x"]))
        terms = [e.key for e in agent._glossary.entries]
        assert any("unique" in e.body for e in agent._glossary.entries), terms
        assert not any("unit_price" in e.body for e in agent._glossary.entries)  # 没混进演示库的口径

    def test_ensure_builds_missing_dataset(self, tmp_path, monkeypatch):
        calls = []

        def fake_build(path):
            calls.append(path)
            Path(path).write_bytes(b"")
            return path

        monkeypatch.setitem(datasets.DATASETS, "olist", replace(datasets.DATASETS["olist"], build=fake_build))
        target = tmp_path / "olist.sqlite"
        assert datasets.ensure(str(target)) == 0 and calls == [target]
        assert datasets.ensure(str(target)) == 0 and len(calls) == 1  # 已存在就不再生成
        assert datasets.ensure(str(tmp_path / "unknown.sqlite")) == 1
        assert datasets.ensure("postgres://u:p@h/db") == 0

    def test_ensure_reports_download_failure(self, tmp_path, monkeypatch, capsys):
        def offline(path):
            raise OSError("connection refused")

        monkeypatch.setitem(datasets.DATASETS, "olist", replace(datasets.DATASETS["olist"], build=offline))
        assert datasets.ensure(str(tmp_path / "olist.sqlite")) == 1
        assert "手动导入" in capsys.readouterr().err


class TestServer:
    def _healthz(self, db_path, tmp_path, **extra):
        from fastapi.testclient import TestClient

        from deepquery.agent import DeepQuery
        from deepquery.llm import MockLLM
        from deepquery.server import create_app

        s = Settings(
            _env_file=None,
            db_path=str(db_path),
            memory_db_path=str(tmp_path / "m.sqlite"),
            web_dist=str(tmp_path / "no-dist"),
            **extra,
        )
        agent = DeepQuery(s, ReadOnlyDatabase(db_path, timeout_seconds=5, max_rows=50), MockLLM(["x"]))
        with TestClient(create_app(agent=agent, settings=s)) as c:
            return c.get("/healthz").json()

    def test_olist_note_samples_and_source(self, olist_db, tmp_path):
        env = self._healthz(olist_db, tmp_path)
        assert env["dataset_note"] == olist_data.DESCRIPTION
        assert "CC BY-NC-SA" in env["dataset_source"]
        assert [s["q"] for s in env["samples"]][0] == "销售额最高的 5 个品类是哪些？"
        assert any(s.get("tag") for s in env["samples"])

    def test_own_database_has_no_samples(self, olist_db, tmp_path):
        own = tmp_path / "mine.sqlite"
        own.write_bytes(Path(olist_db).read_bytes())
        env = self._healthz(own, tmp_path, dataset_note="公司销售库")
        assert env["dataset_note"] == "公司销售库"
        assert env["samples"] == [] and env["dataset_source"] == ""
