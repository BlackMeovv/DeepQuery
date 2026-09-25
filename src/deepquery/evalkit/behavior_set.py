"""生成 Olist 行为评测集（eval/cases/olist-behavior.jsonl）与分析评测集（eval/cases/olist-analysis.jsonl）。

行为评测测的不是"SQL 写得对不对"，而是"该不该写 SQL"：
- clarify：说法有歧义、没有口径定义，应该先问（"哪个卖家最好"）；
- missing：库里没有这类数据，应该说明缺什么（"退货率"）；
- meta：问口径 / 表结构，应该不查数据直接回答；
- chat：寒暄，应该直接回应；
- data / followup：普通问题和追问，应该直接写 SQL——同时算执行准确率，也用来测"不该问却问了"。

    uv run python -m deepquery.evalkit.behavior_set        # 重新生成两个文件
"""

from __future__ import annotations

import json
from pathlib import Path

SALES = "ROUND(SUM(oi.price), 2)"
VALID = "o.status NOT IN ('canceled', 'unavailable')"

# (id, category, question, expect, gold_sql, history)；expect 可以是多个可接受的路径
CASES: list[tuple] = [
    # ---- 普通问题：应该直接查 ----
    ("data-01", "data", "2018 年 1 月有多少笔订单？", "sql",
     "SELECT COUNT(*) FROM orders WHERE strftime('%Y-%m', purchased_at) = '2018-01'", None),
    ("data-02", "data", "平均评分是多少？", "sql", None, None),
    ("data-03", "data", "一共有多少个卖家？", "sql", "SELECT COUNT(*) FROM sellers", None),
    ("data-04", "data", "用信用卡支付的订单有多少笔？", "sql",
     "SELECT COUNT(DISTINCT order_id) FROM payments WHERE type = 'credit_card'", None),
    ("data-05", "data", "销售额最高的 5 个品类是哪些？", "sql", None, None),
    ("data-06", "data", "圣保罗州有多少位客户？", "sql",
     "SELECT COUNT(DISTINCT unique_id) FROM customers WHERE state = 'SP'", None),
    ("data-07", "data", "一共有多少笔订单？", "sql", "SELECT COUNT(*) FROM orders", None),
    ("data-08", "data", "延迟送达的订单有多少笔？", "sql",
     "SELECT COUNT(*) FROM orders WHERE delivered_at IS NOT NULL AND date(delivered_at) > date(estimated_delivery_at)", None),

    # ---- 有歧义：应该先问口径 ----
    ("clarify-01", "clarify", "哪个卖家最好？", "clarify", None, None),
    ("clarify-02", "clarify", "最受欢迎的品类是哪个？", "clarify", None, None),
    ("clarify-03", "clarify", "表现最差的州是哪个？", "clarify", None, None),
    ("clarify-04", "clarify", "哪些客户最有价值？", "clarify", None, None),
    ("clarify-05", "clarify", "服务最好的卖家是哪个？", "clarify", None, None),
    ("clarify-06", "clarify", "哪个品类最好？", "clarify", None, None),
    ("clarify-07", "clarify", "最活跃的客户是谁？", "clarify", None, None),
    ("clarify-08", "clarify", "哪个城市的市场最好？", "clarify", None, None),

    # ---- 数据里没有：应该说明缺什么 ----
    ("missing-01", "missing", "各品类的退货率是多少？", "clarify", None, None),
    ("missing-02", "missing", "客户的年龄分布是怎样的？", "clarify", None, None),
    ("missing-03", "missing", "每个卖家的利润是多少？", "clarify", None, None),
    ("missing-04", "missing", "各商品现在的库存还剩多少？", "clarify", None, None),
    ("missing-05", "missing", "客户的性别比例是多少？", "clarify", None, None),
    ("missing-06", "missing", "广告投放带来了多少订单？", "clarify", None, None),
    ("missing-07", "missing", "各品类的毛利率是多少？", "clarify", None, None),
    ("missing-08", "missing", "各会员等级的客户各有多少？", "clarify", None, None),

    # ---- 问口径 / 表结构：应该直接回答，不查数据 ----
    ("meta-01", "meta", "销售额是怎么算的？", "meta", None, None),
    ("meta-02", "meta", "延迟送达的口径是什么？", "meta", None, None),
    ("meta-03", "meta", "orders 表有哪些字段？", "meta", None, None),
    ("meta-04", "meta", "评分字段是什么意思？", "meta", None, None),
    ("meta-05", "meta", "客单价的定义是什么？", "meta", None, None),
    ("meta-06", "meta", "GMV 和销售额有什么区别？", "meta", None, None),
    ("meta-07", "meta", "送达天数怎么计算？", "meta", None, None),
    ("meta-08", "meta", "reviews 表和 orders 表是怎么关联的？", "meta", None, None),
    ("meta-09", "meta", "订单状态有哪些取值？", ["meta", "sql"], None, None),

    # ---- 寒暄：应该直接回应 ----
    ("chat-01", "chat", "你好", "chat", None, None),
    ("chat-02", "chat", "你是谁？", "chat", None, None),
    ("chat-03", "chat", "你能做什么？", "chat", None, None),
    ("chat-04", "chat", "谢谢", "chat", None, None),
    ("chat-05", "chat", "你是用什么模型做的？", "chat", None, None),
    ("chat-06", "chat", "讲个笑话吧", "chat", None, None),
]

# 追问：(id, 上一问, 上一问的 SQL, 追问, expect, 追问的标准 SQL)
FOLLOWUPS: list[tuple] = [
    ("followup-01", "每个州有多少位客户？",
     "SELECT s.name_zh AS 州, COUNT(DISTINCT c.unique_id) AS 客户数 FROM customers c JOIN states s ON s.code = c.state "
     "GROUP BY s.name_zh ORDER BY 客户数 DESC",
     "那只看东南部的呢？", "sql",
     "SELECT s.name_zh AS 州, COUNT(DISTINCT c.unique_id) AS 客户数 FROM customers c JOIN states s ON s.code = c.state "
     "WHERE s.region_zh = '东南部' GROUP BY s.name_zh ORDER BY 客户数 DESC"),
    ("followup-02", "2017 年每个月有多少笔订单？",
     "SELECT strftime('%Y-%m', purchased_at) AS 月份, COUNT(*) AS 订单数 FROM orders "
     "WHERE strftime('%Y', purchased_at) = '2017' GROUP BY 月份 ORDER BY 月份",
     "那 2018 年呢？", "sql",
     "SELECT strftime('%Y-%m', purchased_at) AS 月份, COUNT(*) AS 订单数 FROM orders "
     "WHERE strftime('%Y', purchased_at) = '2018' GROUP BY 月份 ORDER BY 月份"),
    ("followup-03", "销售额最高的 5 个品类是哪些？",
     f"SELECT c.name_zh AS 品类, {SALES} AS 销售额 FROM order_items oi JOIN orders o ON o.id = oi.order_id "
     f"JOIN products p ON p.id = oi.product_id JOIN categories c ON c.name = p.category WHERE {VALID} "
     "GROUP BY c.name_zh ORDER BY 销售额 DESC LIMIT 5",
     "那最低的 5 个呢？", "sql",
     f"SELECT c.name_zh AS 品类, {SALES} AS 销售额 FROM order_items oi JOIN orders o ON o.id = oi.order_id "
     f"JOIN products p ON p.id = oi.product_id JOIN categories c ON c.name = p.category WHERE {VALID} "
     "GROUP BY c.name_zh ORDER BY 销售额 ASC LIMIT 5"),
    ("followup-04", "圣保罗州有多少笔订单？",
     "SELECT COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.state = 'SP'",
     "里约热内卢州呢？", "sql",
     "SELECT COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.state = 'RJ'"),
    ("followup-05", "用信用卡支付的订单有多少笔？",
     "SELECT COUNT(DISTINCT order_id) FROM payments WHERE type = 'credit_card'",
     "那用银行票据的呢？", "sql",
     "SELECT COUNT(DISTINCT order_id) FROM payments WHERE type = 'boleto'"),
    ("followup-06", "平均评分是多少？", "SELECT ROUND(AVG(score), 2) FROM reviews",
     "延迟送达的订单呢？", "sql",
     "SELECT ROUND(AVG(r.score), 2) FROM reviews r JOIN orders o ON o.id = r.order_id "
     "WHERE o.delivered_at IS NOT NULL AND date(o.delivered_at) > date(o.estimated_delivery_at)"),
    ("followup-07", "5 星评价有多少条？", "SELECT COUNT(*) FROM reviews WHERE score = 5",
     "1 星呢？", "sql", "SELECT COUNT(*) FROM reviews WHERE score = 1"),
    ("followup-08", "各支付方式各被用了多少次？",
     "SELECT type AS 支付方式, COUNT(*) AS 次数 FROM payments GROUP BY type ORDER BY 次数 DESC",
     "按金额呢？", "sql",
     "SELECT type AS 支付方式, ROUND(SUM(amount), 2) AS 金额 FROM payments GROUP BY type ORDER BY 金额 DESC"),
    ("followup-09", "每个大区的平均送达天数是多少？",
     "SELECT s.region_zh AS 大区, ROUND(AVG(julianday(o.delivered_at) - julianday(o.purchased_at)), 1) AS 平均送达天数 "
     "FROM orders o JOIN customers c ON c.id = o.customer_id JOIN states s ON s.code = c.state "
     "WHERE o.delivered_at IS NOT NULL GROUP BY s.region_zh ORDER BY 平均送达天数 DESC",
     "东北部各州分别是多少？", "sql",
     "SELECT s.name_zh AS 州, ROUND(AVG(julianday(o.delivered_at) - julianday(o.purchased_at)), 1) AS 平均送达天数 "
     "FROM orders o JOIN customers c ON c.id = o.customer_id JOIN states s ON s.code = c.state "
     "WHERE o.delivered_at IS NOT NULL AND s.region_zh = '东北部' GROUP BY s.name_zh ORDER BY 平均送达天数 DESC"),
    ("followup-10", "2017 年有多少笔订单？",
     "SELECT COUNT(*) FROM orders WHERE strftime('%Y', purchased_at) = '2017'",
     "其中已送达的有多少？", "sql",
     "SELECT COUNT(*) FROM orders WHERE strftime('%Y', purchased_at) = '2017' AND status = 'delivered'"),
    ("followup-11", "卖家最多的 3 个州是哪些？",
     "SELECT s.name_zh AS 州, COUNT(*) AS 卖家数 FROM sellers se JOIN states s ON s.code = se.state "
     "GROUP BY s.name_zh ORDER BY 卖家数 DESC LIMIT 3",
     "这 3 个州各有多少位客户？", "sql",
     "SELECT s.name_zh AS 州, COUNT(DISTINCT c.unique_id) AS 客户数 FROM customers c JOIN states s ON s.code = c.state "
     "WHERE c.state IN (SELECT state FROM sellers GROUP BY state ORDER BY COUNT(*) DESC LIMIT 3) "
     "GROUP BY s.name_zh ORDER BY 客户数 DESC"),
    ("followup-12", "2018 年 1 月的销售额是多少？",
     f"SELECT {SALES} FROM order_items oi JOIN orders o ON o.id = oi.order_id "
     f"WHERE strftime('%Y-%m', o.purchased_at) = '2018-01' AND {VALID}",
     "2 月呢？", "sql",
     f"SELECT {SALES} FROM order_items oi JOIN orders o ON o.id = oi.order_id "
     f"WHERE strftime('%Y-%m', o.purchased_at) = '2018-02' AND {VALID}"),
    # 追问口径：接着上一问问"怎么算的"，应该直接回答
    ("followup-13", "销售额最高的 5 个品类是哪些？",
     f"SELECT c.name_zh AS 品类, {SALES} AS 销售额 FROM order_items oi JOIN orders o ON o.id = oi.order_id "
     f"JOIN products p ON p.id = oi.product_id JOIN categories c ON c.name = p.category WHERE {VALID} "
     "GROUP BY c.name_zh ORDER BY 销售额 DESC LIMIT 5",
     "这里的销售额是按什么口径算的？", "meta", None),
    ("followup-14", "平均评分是多少？", "SELECT ROUND(AVG(score), 2) FROM reviews",
     "上面这个用了哪些表和字段？", "meta", None),
]

ANALYSIS = [
    "2018 年 3 月的销售额比 2 月是涨还是跌？主要是哪些品类带来的？",
    "延迟送达对评分的影响有多大？哪些州的延迟最严重？",
    "为什么 2017 年 11 月的订单量特别高？",
    "圣保罗州和里约热内卢州的客户，在客单价和评分上有什么差别？",
    "哪些品类的差评率最高？可能是什么原因？",
    "2018 年上半年各月销售额的走势如何？哪个月增长最快？",
]


def cases() -> list[dict]:
    out = []
    for cid, category, question, expect, gold, history in CASES:
        out.append({"id": cid, "category": category, "question": question,
                    "expect": expect if isinstance(expect, list) else [expect],
                    "gold_sql": gold, "history": history or []})
    for cid, prev_q, prev_sql, question, expect, gold in FOLLOWUPS:
        out.append({"id": cid, "category": "followup", "question": question,
                    "expect": expect if isinstance(expect, list) else [expect],
                    "gold_sql": gold, "history": [{"question": prev_q, "sql": prev_sql}]})
    return out


def main() -> int:
    root = Path("eval/cases")
    with open(root / "olist-behavior.jsonl", "w", encoding="utf-8") as f:
        f.write("# Olist 行为评测集：测\"该不该写 SQL\"（先问 / 说明缺数据 / 直接回答口径 / 寒暄 / 查数据与追问）。\n")
        f.write("# expect 是可接受的路径；gold_sql 非空的题同时算执行准确率。生成脚本：deepquery/evalkit/behavior_set.py\n")
        for c in cases():
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    with open(root / "olist-analysis.jsonl", "w", encoding="utf-8") as f:
        f.write("# Olist 分析模式评测题：没有标准答案，自动统计结论能否逐句核对出处、各步成功率、步数、耗时与花费。\n")
        for i, q in enumerate(ANALYSIS, 1):
            f.write(json.dumps({"id": f"analysis-{i:02d}", "question": q}, ensure_ascii=False) + "\n")
    print(f"已生成 {len(cases())} 条行为用例、{len(ANALYSIS)} 条分析题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
