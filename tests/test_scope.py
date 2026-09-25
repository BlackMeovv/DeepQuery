"""口径说明：SQL → 一行看得懂的"筛选 / 分组 / 排序 / 条数"，不经过模型。"""

from deepquery import scope
from deepquery.agent import DeepQuery
from deepquery.llm import MockLLM

DOCS = {
    "orders": """CREATE TABLE orders (                    -- 订单表
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,           -- → customers.id
    status TEXT NOT NULL,                -- 订单状态: delivered 已送达 / shipped 已发货 / canceled 已取消 /
                                         -- unavailable 缺货取消
    purchased_at TEXT NOT NULL,          -- 下单时间 'YYYY-MM-DD HH:MM:SS'
    delivered_at TEXT,                   -- 买家签收时间（未送达为 NULL）
    estimated_delivery_at TEXT           -- 平台承诺的送达日期
);
-- orders 样例行:
--   id, status""",
    "order_items": """CREATE TABLE order_items (               -- 订单明细
    order_id TEXT NOT NULL,              -- → orders.id
    category TEXT,                       -- 品类
    price REAL NOT NULL                  -- 商品成交价（雷亚尔）
);""",
}
LABELS = scope.column_labels(DOCS)


def describe(sql):
    return scope.describe(sql, labels=LABELS)


class TestLabels:
    def test_column_names_and_enum_values(self):
        label, values = LABELS[("orders", "status")]
        assert label == "订单状态"
        assert values["canceled"] == "已取消" and values["unavailable"] == "缺货取消"  # 跨行注释也读到了
        assert LABELS[("orders", "customer_id")][0] == ""  # 只是外键说明，不当中文名
        assert LABELS[("orders", "*")][0] == "订单"


class TestDescribe:
    def test_sales_by_category(self):
        sql = (
            "SELECT oi.category, SUM(oi.price) AS 销售额 FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.status NOT IN ('canceled', 'unavailable') GROUP BY oi.category ORDER BY 销售额 DESC LIMIT 5"
        )
        assert describe(sql) == [
            "筛选：订单状态 不是 已取消、缺货取消",
            "按 品类 分组",
            "按 销售额 从高到低",
            "取前 5 条",
        ]

    def test_dates_read_as_earlier_later(self):
        sql = (
            "SELECT COUNT(*) FROM orders o WHERE strftime('%Y-%m', o.purchased_at) = '2018-04' "
            "AND o.delivered_at IS NOT NULL AND date(o.delivered_at) > date(o.estimated_delivery_at)"
        )
        assert describe(sql) == [
            "筛选：下单时间的年月 为 2018-04，买家签收时间 不为空，买家签收时间 晚于 平台承诺的送达日期"
        ]

    def test_order_by_aggregate_and_or(self):
        sql = (
            "SELECT category FROM order_items WHERE category = 'a' OR category LIKE '%b%' "
            "GROUP BY category ORDER BY SUM(price) DESC LIMIT 1"
        )
        assert describe(sql) == [
            "筛选：（品类 为 a 或 品类 包含 b）", "按 品类 分组", "按 商品成交价的总和 从高到低", "取前 1 条"
        ]

    def test_inner_grouping_and_having(self):
        sql = (
            "SELECT COUNT(*) FROM (SELECT o.customer_id FROM orders o GROUP BY o.customer_id "
            "HAVING COUNT(DISTINCT o.id) >= 2)"
        )
        assert describe(sql) == ["先按 customer_id 分组，只保留 不重复的订单数 不小于 2 的"]

    def test_exists_and_joins(self):
        sql = (
            "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS "
            "(SELECT 1 FROM order_items oi WHERE oi.order_id = o.id AND oi.price > 100)"
        )
        # EXISTS 里的条件是比较用的，不算统计范围；连接条件 o.id = oi.order_id 不是筛选
        assert describe(sql) == ["筛选：没有对应的 order_items 记录"]

    def test_unparseable_or_empty(self):
        assert describe("SELEC nope (") == []
        assert describe(None) == []
        assert describe("SELECT 1 UNION SELECT 2") == ["合并了多个查询的结果"]


class TestOutcome:
    def test_summary_uses_model_sql_not_guard_limit(self, settings, db):
        sql = "SELECT city, COUNT(*) AS 客户数 FROM customers GROUP BY city ORDER BY 客户数 DESC"
        llm = MockLLM([f"思路。\n```sql\n{sql}\n```"])
        outcome = DeepQuery(settings, db, llm).ask("各城市客户数？", generate_answer=False)
        assert "LIMIT 200" in outcome.final_sql.upper()
        assert outcome.sql_summary[-1] == "按 客户数 从高到低"  # 不会出现"取前 200 条"
        assert any(part.startswith("按 ") and part.endswith("分组") for part in outcome.sql_summary)
