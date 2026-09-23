from deepquery.guard import validate

TABLES = {"customers", "orders", "products", "categories", "order_items", "payments"}


def ok(sql, **kw):
    verdict = validate(sql, allowed_tables=TABLES, max_rows=kw.pop("max_rows", 200))
    assert verdict.allowed, f"应放行但被拒: {verdict.reason}"
    return verdict


def rejected(sql, kind=None):
    verdict = validate(sql, allowed_tables=TABLES, max_rows=200)
    assert not verdict.allowed, f"应拒绝但放行: {verdict.sql}"
    if kind:
        assert verdict.error_kind == kind, f"错误分类应为 {kind}，实际 {verdict.error_kind}"
    return verdict


class TestAllowed:
    def test_simple_select(self):
        ok("SELECT * FROM customers")

    def test_join_and_group(self):
        ok(
            "SELECT c.city, COUNT(*) FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.city"
        )

    def test_cte(self):
        ok("WITH t AS (SELECT customer_id FROM orders) SELECT COUNT(*) FROM t")

    def test_subquery(self):
        ok("SELECT AVG(cnt) FROM (SELECT SUM(quantity) AS cnt FROM order_items GROUP BY order_id)")

    def test_union(self):
        ok("SELECT id FROM customers UNION SELECT id FROM products")

    def test_trailing_semicolon(self):
        ok("SELECT * FROM customers;")


class TestLimitEnforcement:
    def test_limit_injected(self):
        verdict = ok("SELECT * FROM customers")
        assert "LIMIT 200" in verdict.sql.upper()

    def test_oversized_limit_clamped(self):
        verdict = ok("SELECT * FROM customers LIMIT 99999")
        assert "99999" not in verdict.sql
        assert "LIMIT 200" in verdict.sql.upper()

    def test_small_limit_kept(self):
        verdict = ok("SELECT * FROM customers LIMIT 5")
        assert "LIMIT 5" in verdict.sql.upper()


class TestNoTable:
    """不读任何表的 SQL：数字是模型写进去的，不是查出来的，不能当查询结果。"""

    def test_constant_select(self):
        v = rejected("SELECT 1 + 1", "guard_rejected")
        assert "没有读取任何数据表" in v.reason

    def test_constant_union(self):
        rejected("SELECT '销售额' AS 指标, 'orders' AS 表 UNION ALL SELECT '评分', 'reviews'", "guard_rejected")

    def test_values_cte(self):
        rejected("WITH t(a) AS (VALUES (1), (2)) SELECT a FROM t", "guard_rejected")

    def test_constant_cte(self):
        rejected("WITH t AS (SELECT 42 AS n) SELECT n FROM t", "guard_rejected")

    def test_table_in_subquery_counts(self):
        ok("SELECT (SELECT COUNT(*) FROM orders) AS n")

    def test_exists_counts(self):
        ok("SELECT 1 AS has_orders WHERE EXISTS (SELECT 1 FROM orders)")


class TestRejected:
    def test_insert(self):
        rejected("INSERT INTO customers (id, name) VALUES (1, 'x')", "guard_rejected")

    def test_update(self):
        rejected("UPDATE customers SET name = 'x'", "guard_rejected")

    def test_delete(self):
        rejected("DELETE FROM customers", "guard_rejected")

    def test_drop(self):
        rejected("DROP TABLE customers", "guard_rejected")

    def test_pragma(self):
        rejected("PRAGMA table_info(customers)", "guard_rejected")

    def test_multi_statement_stacked_write(self):
        rejected("SELECT * FROM customers; DROP TABLE customers", "guard_rejected")

    def test_unknown_table(self):
        rejected("SELECT * FROM users", "guard_rejected")

    def test_sqlite_master(self):
        rejected("SELECT * FROM sqlite_master", "guard_rejected")

    def test_schema_prefix(self):
        rejected("SELECT * FROM main.customers", "guard_rejected")

    def test_table_valued_function(self):
        rejected("SELECT * FROM pragma_table_info('customers')", "guard_rejected")

    def test_syntax_error(self):
        rejected("SELEC * FROM customers", "syntax_error")

    def test_empty(self):
        rejected("", "guard_rejected")

    def test_write_hidden_in_cte(self):
        # sqlite 不支持 CTE 里写操作，但守卫要在解析层就拒绝
        rejected("WITH t AS (DELETE FROM customers) SELECT 1")


class TestMalformedSql:
    """BIRD 真实跑批抓到的崩溃回归：词法级畸形 SQL（未闭合反引号）必须被
    守卫拒绝并归类为 syntax_error，而不是让 TokenError 冲出守卫砸停评测。"""

    def test_unterminated_backtick_rejected_not_raised(self):
        verdict = validate("SELECT `Lead FROM atom", {"atom"})
        assert not verdict.allowed
        assert verdict.error_kind == "syntax_error"

    def test_unterminated_quote_rejected(self):
        verdict = validate("SELECT 'abc FROM t", {"t"})
        assert not verdict.allowed
