import sqlite3

import pytest

from deepquery.tools.database import ReadOnlyDatabase


class TestReadOnly:
    def test_write_denied_even_without_guard(self, db):
        """守卫被绕过时，数据库层（第二道防线）也必须拒绝写入。"""
        result = db.run_query("UPDATE customers SET name = 'hacked'")
        assert not result.ok
        assert result.error_kind in ("guard_rejected", "execution_error")
        # 确认数据未被修改
        check = db.run_query("SELECT COUNT(*) FROM customers WHERE name = 'hacked'")
        assert check.error_kind == "empty_result" or check.rows[0][0] == 0

    def test_query_only_pragma_via_direct_conn(self, demo_db_path):
        conn = sqlite3.connect(f"file:{demo_db_path}?mode=ro", uri=True)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO categories (id, name) VALUES (99, 'x')")
        conn.close()


class TestQuery:
    def test_basic(self, db):
        result = db.run_query("SELECT id, name FROM customers ORDER BY id LIMIT 3")
        assert result.ok and result.row_count == 3
        assert result.columns == ["id", "name"]

    def test_truncation(self, demo_db_path):
        small = ReadOnlyDatabase(demo_db_path, max_rows=10)
        result = small.run_query("SELECT id FROM orders")
        assert result.ok and result.row_count == 10 and result.truncated

    def test_empty_result_classified(self, db):
        result = db.run_query("SELECT * FROM customers WHERE city = '不存在的城市'")
        assert not result.ok and result.error_kind == "empty_result"

    def test_no_such_column(self, db):
        result = db.run_query("SELECT nonexist FROM customers")
        assert not result.ok and result.error_kind == "no_such_column"

    def test_no_such_table(self, db):
        result = db.run_query("SELECT * FROM ghosts")
        assert not result.ok and result.error_kind == "no_such_table"

    def test_syntax_error(self, db):
        result = db.run_query("SELECT FROM WHERE")
        assert not result.ok and result.error_kind == "syntax_error"

    def test_timeout(self, demo_db_path):
        fast = ReadOnlyDatabase(demo_db_path, timeout_seconds=0.1)
        # 无终止条件的递归 CTE 会一直跑，必须被超时中断
        result = fast.run_query(
            "WITH RECURSIVE r(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM r) "
            "SELECT COUNT(*) FROM r"
        )
        assert not result.ok and result.error_kind == "timeout"


class TestSchema:
    def test_table_names(self, db):
        assert set(db.table_names()) == {
            "customers",
            "categories",
            "products",
            "orders",
            "order_items",
            "payments",
        }

    def test_schema_text_contains_ddl_and_samples(self, db):
        text = db.schema_text()
        assert "CREATE TABLE customers" in text
        assert "样例行" in text


class TestValueSizeLimit:
    def test_huge_value_rejected(self, db):
        # 回归：行数有上限但单元格没有时，一条查询就能吃掉几百 MB 内存
        result = db.run_query("SELECT hex(randomblob(2000000))")
        assert not result.ok and "too big" in (result.error_message or "")

    def test_normal_values_unaffected(self, db):
        assert db.run_query("SELECT group_concat(name) FROM customers").ok
