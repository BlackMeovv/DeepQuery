"""Olist 真实数据评测集生成器：在真实订单数据上考察 Agent。

    python -m deepquery.evalkit.olist_set      # 或 make olist-set（需先 make olist-db）

与自建业务集（演示库）的区别：数据是真实的，分布不均、有缺失值和脏数据
（没有品类的商品、没有评价的订单、订单级客户 ID 等），更接近真实业务库。

设计：
- 参数化模板（州 / 品类 / 月份 / 支付方式 / 评分）+ 手写的多表、口径类问题；
- 金额、客户数、延迟送达等口径与 eval/knowledge/olist/glossary.jsonl 一致——题目用业务说法，
  考察的是"口径翻译"而不只是 SQL 语法；
- 列表类题目在题面写明要返回哪几列，避免"多带一列就判错"的无谓失分；
- 每条 gold 逐条执行校验：报错、空结果、结果为 0 的剔除；TOP-N 题若在截断处并列也剔除
  （并列时取哪几个都对，无法唯一判分）；
- 固定 seed 打乱后 70/30 切成 dev / holdout：调 prompt 只看 dev，holdout 只用于最终复核；
- 不收录 eval/knowledge/olist/examples.jsonl 里的例句，避免数据泄漏。
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import sqlglot
from sqlglot import exp

from ..olist_data import DEFAULT_PATH as OLIST_DB_PATH

SEED = 42
DEV_RATIO = 0.7

# 客户和卖家最多的几个州：(缩写, 中文名)
_STATES = [("SP", "圣保罗州"), ("RJ", "里约热内卢州"), ("MG", "米纳斯吉拉斯州"),
           ("RS", "南里奥格兰德州"), ("PR", "巴拉那州"), ("BA", "巴伊亚州")]
# 销售额靠前的品类：(原名, 中文名)
_CATEGORIES = [("beleza_saude", "美妆健康"), ("relogios_presentes", "手表礼品"),
               ("cama_mesa_banho", "床品卫浴"), ("esporte_lazer", "运动休闲"),
               ("informatica_acessorios", "电脑配件"), ("brinquedos", "玩具")]
_MONTHS = ["2017-03", "2017-06", "2017-09", "2017-11", "2017-12", "2018-01", "2018-04", "2018-07"]
_PAYMENTS = [("credit_card", "信用卡"), ("boleto", "银行票据"), ("voucher", "代金券"), ("debit_card", "借记卡")]

# 口径片段（与 glossary 一致）
_VALID = "o.status NOT IN ('canceled', 'unavailable')"  # 销售额排除已取消和缺货取消
_LATE = "o.delivered_at IS NOT NULL AND date(o.delivered_at) > date(o.estimated_delivery_at)"
_DAYS = "julianday(o.delivered_at) - julianday(o.purchased_at)"


def _month_cn(ym: str) -> str:
    y, m = ym.split("-")
    return f"{y} 年 {int(m)} 月"


def _templates() -> list[tuple[str, str, list[str]]]:
    """返回 (question, gold_sql, tags)。"""
    out: list[tuple[str, str, list[str]]] = []

    for code, zh in _STATES:
        out += [
            (f"{zh}有多少位客户？",
             f"SELECT COUNT(DISTINCT c.unique_id) FROM customers c WHERE c.state = '{code}'",
             ["filter", "jargon"]),
            (f"{zh}有多少个卖家？",
             f"SELECT COUNT(*) FROM sellers s WHERE s.state = '{code}'",
             ["filter"]),
            (f"{zh}的客户一共下了多少笔订单？",
             f"SELECT COUNT(*) FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.state = '{code}'",
             ["join"]),
            (f"{zh}的客户贡献了多少销售额？",
             "SELECT SUM(oi.price) FROM order_items oi JOIN orders o ON o.id = oi.order_id "
             f"JOIN customers c ON c.id = o.customer_id WHERE c.state = '{code}' AND {_VALID}",
             ["join", "money", "jargon"]),
            (f"寄往{zh}的订单平均送达天数是多少？",
             f"SELECT AVG({_DAYS}) FROM orders o JOIN customers c ON c.id = o.customer_id "
             f"WHERE c.state = '{code}' AND o.delivered_at IS NOT NULL",
             ["join", "date", "jargon"]),
        ]

    for cat, zh in _CATEGORIES:
        out += [
            (f"{zh}品类有多少个商品？",
             f"SELECT COUNT(*) FROM products p WHERE p.category = '{cat}'",
             ["filter"]),
            (f"{zh}品类的销售额是多少？",
             "SELECT SUM(oi.price) FROM order_items oi JOIN orders o ON o.id = oi.order_id "
             f"JOIN products p ON p.id = oi.product_id WHERE p.category = '{cat}' AND {_VALID}",
             ["join", "money", "jargon"]),
            (f"{zh}商品一共被下单了多少件？",
             "SELECT COUNT(*) FROM order_items oi JOIN products p ON p.id = oi.product_id "
             f"WHERE p.category = '{cat}'",
             ["join"]),
            (f"{zh}商品的平均重量是多少克？",
             f"SELECT AVG(p.weight_g) FROM products p WHERE p.category = '{cat}'",
             ["filter"]),
            (f"有多少位客户下单买过{zh}商品？",
             "SELECT COUNT(DISTINCT c.unique_id) FROM customers c JOIN orders o ON o.customer_id = c.id "
             "JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id "
             f"WHERE p.category = '{cat}'",
             ["join", "jargon"]),
        ]

    for ym in _MONTHS:
        cn = _month_cn(ym)
        out += [
            (f"{cn}有多少笔订单？",
             f"SELECT COUNT(*) FROM orders o WHERE strftime('%Y-%m', o.purchased_at) = '{ym}'",
             ["date"]),
            (f"{cn}的销售额是多少？",
             "SELECT SUM(oi.price) FROM order_items oi JOIN orders o ON o.id = oi.order_id "
             f"WHERE strftime('%Y-%m', o.purchased_at) = '{ym}' AND {_VALID}",
             ["join", "date", "money", "jargon"]),
            (f"{cn}下单的订单里，有多少笔延迟送达？",
             f"SELECT COUNT(*) FROM orders o WHERE strftime('%Y-%m', o.purchased_at) = '{ym}' AND {_LATE}",
             ["date", "jargon"]),
        ]

    for ptype, zh in _PAYMENTS:
        out += [
            (f"用{zh}支付的总金额是多少？",
             f"SELECT SUM(p.amount) FROM payments p WHERE p.type = '{ptype}'",
             ["filter", "money"]),
            (f"{zh}支付一共有多少笔？",
             f"SELECT COUNT(*) FROM payments p WHERE p.type = '{ptype}'",
             ["filter"]),
        ]

    for score in range(1, 6):
        out.append((f"{score} 星的评价有多少条？",
                    f"SELECT COUNT(*) FROM reviews r WHERE r.score = {score}",
                    ["filter"]))

    out += _handwritten()
    return out


def _handwritten() -> list[tuple[str, str, list[str]]]:
    """口径、多表关联、边界情况：模板覆盖不到的真实业务问法。"""
    return [
        ("平台一共有多少位客户？",
         "SELECT COUNT(DISTINCT unique_id) FROM customers", ["jargon"]),
        ("客单价是多少？",
         "SELECT SUM(oi.price) / COUNT(DISTINCT oi.order_id) FROM order_items oi "
         f"JOIN orders o ON o.id = oi.order_id WHERE {_VALID}", ["join", "money", "jargon"]),
        ("复购客户有多少人？",
         "SELECT COUNT(*) FROM (SELECT c.unique_id FROM customers c JOIN orders o ON o.customer_id = c.id "
         "GROUP BY c.unique_id HAVING COUNT(DISTINCT o.id) >= 2)", ["join", "jargon"]),
        ("2017 年有多少位客户下过单？",
         "SELECT COUNT(DISTINCT c.unique_id) FROM customers c JOIN orders o ON o.customer_id = c.id "
         "WHERE strftime('%Y', o.purchased_at) = '2017'", ["join", "date", "jargon"]),
        ("已取消的订单有多少笔？",
         "SELECT COUNT(*) FROM orders WHERE status = 'canceled'", ["filter"]),
        ("因为缺货被取消的订单有多少笔？",
         "SELECT COUNT(*) FROM orders WHERE status = 'unavailable'", ["filter"]),
        ("状态是已发货的订单有多少笔？",
         "SELECT COUNT(*) FROM orders WHERE status = 'shipped'", ["filter"]),
        ("延迟送达的订单有多少笔？",
         f"SELECT COUNT(*) FROM orders o WHERE {_LATE}", ["jargon"]),
        ("延迟送达的订单占已送达订单的百分之多少？",
         "SELECT 100.0 * SUM(date(delivered_at) > date(estimated_delivery_at)) / COUNT(*) "
         "FROM orders WHERE delivered_at IS NOT NULL", ["jargon"]),
        ("已送达订单的平均送达天数是多少？",
         f"SELECT AVG({_DAYS}) FROM orders o WHERE o.delivered_at IS NOT NULL", ["date", "jargon"]),
        ("东南部大区有多少位客户？",
         "SELECT COUNT(DISTINCT c.unique_id) FROM customers c JOIN states s ON s.code = c.state "
         "WHERE s.region_zh = '东南部'", ["join", "jargon"]),
        ("南部大区有多少个卖家？",
         "SELECT COUNT(*) FROM sellers se JOIN states s ON s.code = se.state WHERE s.region_zh = '南部'",
         ["join"]),
        ("2017 年 11 月 24 日（黑色星期五）当天有多少笔订单？",
         "SELECT COUNT(*) FROM orders WHERE date(purchased_at) = '2017-11-24'", ["date"]),
        ("2018 年 1 月到 3 月的销售额是多少？",
         "SELECT SUM(oi.price) FROM order_items oi JOIN orders o ON o.id = oi.order_id "
         f"WHERE o.purchased_at >= '2018-01-01' AND o.purchased_at < '2018-04-01' AND {_VALID}",
         ["join", "date", "money", "jargon"]),
        ("一个订单最多包含多少件商品？",
         "SELECT MAX(n) FROM (SELECT COUNT(*) AS n FROM order_items GROUP BY order_id)", ["agg"]),
        ("有多少笔订单用了不止一种支付方式？",
         "SELECT COUNT(*) FROM (SELECT order_id FROM payments GROUP BY order_id "
         "HAVING COUNT(DISTINCT type) > 1)", ["agg"]),
        ("有多少笔订单用到了代金券？",
         "SELECT COUNT(DISTINCT order_id) FROM payments WHERE type = 'voucher'", ["filter"]),
        ("信用卡支付平均分几期？",
         "SELECT AVG(installments) FROM payments WHERE type = 'credit_card'", ["filter"]),
        ("信用卡分期最多分了多少期？",
         "SELECT MAX(installments) FROM payments WHERE type = 'credit_card'", ["filter"]),
        ("分期超过 6 期的信用卡支付有多少笔？",
         "SELECT COUNT(*) FROM payments WHERE type = 'credit_card' AND installments > 6", ["filter"]),
        ("支付总金额是多少？",
         "SELECT SUM(amount) FROM payments", ["money", "jargon"]),
        ("平均每笔支付的金额是多少？",
         "SELECT AVG(amount) FROM payments", ["money"]),
        ("运费一共收了多少？",
         "SELECT SUM(freight) FROM order_items", ["money", "jargon"]),
        ("没有品类的商品有多少个？",
         "SELECT COUNT(*) FROM products WHERE category IS NULL", ["filter"]),
        ("重量超过 10 公斤的商品有多少个？",
         "SELECT COUNT(*) FROM products WHERE weight_g > 10000", ["filter"]),
        ("图片最多的商品有几张图？",
         "SELECT MAX(photos_qty) FROM products", ["agg"]),
        ("所有评价的平均评分是多少？",
         "SELECT AVG(score) FROM reviews", ["agg"]),
        ("差评一共有多少条？",
         "SELECT COUNT(*) FROM reviews WHERE score <= 2", ["jargon"]),
        ("好评占全部评价的百分之多少？",
         "SELECT 100.0 * SUM(score >= 4) / COUNT(*) FROM reviews", ["jargon"]),
        ("写了评论内容的评价有多少条？",
         "SELECT COUNT(*) FROM reviews WHERE comment IS NOT NULL", ["filter"]),
        ("没有任何评价的订单有多少笔？",
         "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM reviews r WHERE r.order_id = o.id)",
         ["join"]),
        ("给了 1 星评价的订单里，状态是已送达的有多少笔？",
         "SELECT COUNT(DISTINCT o.id) FROM orders o JOIN reviews r ON r.order_id = o.id "
         "WHERE r.score = 1 AND o.status = 'delivered'", ["join", "filter"]),
        ("卖家和客户在同一个州的订单明细有多少条？",
         "SELECT COUNT(*) FROM order_items oi JOIN sellers se ON se.id = oi.seller_id "
         "JOIN orders o ON o.id = oi.order_id JOIN customers c ON c.id = o.customer_id "
         "WHERE se.state = c.state", ["join"]),
        ("圣保罗州的卖家卖出的销售额是多少？",
         "SELECT SUM(oi.price) FROM order_items oi JOIN sellers se ON se.id = oi.seller_id "
         f"JOIN orders o ON o.id = oi.order_id WHERE se.state = 'SP' AND {_VALID}",
         ["join", "money", "jargon"]),
        ("卖家最多的城市是哪个？",
         "SELECT city FROM sellers GROUP BY city ORDER BY COUNT(*) DESC LIMIT 1", ["sort"]),
        ("客户最多的城市是哪个？",
         "SELECT city FROM customers GROUP BY city ORDER BY COUNT(DISTINCT unique_id) DESC LIMIT 1",
         ["sort", "jargon"]),
        ("销售额最高的卖家 id 是什么？",
         "SELECT oi.seller_id FROM order_items oi JOIN orders o ON o.id = oi.order_id "
         f"WHERE {_VALID} GROUP BY oi.seller_id ORDER BY SUM(oi.price) DESC LIMIT 1",
         ["join", "sort", "money", "jargon"]),
        ("列出销售额最高的 5 个品类（中文名）及其销售额",
         "SELECT c.name_zh, SUM(oi.price) AS sales FROM order_items oi JOIN orders o ON o.id = oi.order_id "
         "JOIN products p ON p.id = oi.product_id JOIN categories c ON c.name = p.category "
         f"WHERE {_VALID} GROUP BY c.name_zh ORDER BY sales DESC LIMIT 5",
         ["join", "sort", "money", "jargon"]),
        ("列出客户最多的 3 个州（中文名）及其客户数",
         "SELECT s.name_zh, COUNT(DISTINCT c.unique_id) AS n FROM customers c JOIN states s ON s.code = c.state "
         "GROUP BY s.name_zh ORDER BY n DESC LIMIT 3",
         ["join", "sort", "jargon"]),
        ("按客户所在州，列出平均送达天数最长的 3 个州（中文名）及其平均送达天数",
         f"SELECT s.name_zh, AVG({_DAYS}) AS days FROM orders o JOIN customers c ON c.id = o.customer_id "
         "JOIN states s ON s.code = c.state WHERE o.delivered_at IS NOT NULL "
         "GROUP BY s.name_zh ORDER BY days DESC LIMIT 3",
         ["join", "sort", "date", "jargon"]),
        ("列出 2018 年 1 月到 6 月每个月的订单数（月份写成 2018-01 这样）",
         "SELECT strftime('%Y-%m', purchased_at) AS month, COUNT(*) AS n FROM orders "
         "WHERE purchased_at >= '2018-01-01' AND purchased_at < '2018-07-01' GROUP BY month ORDER BY month",
         ["date", "sort"]),
    ]


def _ties_at_cut(conn: sqlite3.Connection, sql: str) -> bool:
    """TOP-N 题：第 N 名与第 N+1 名的排序键相同 → 取哪个都对，无法唯一判分。

    排序键不一定在输出列里（如"卖家最多的城市"只返回城市名），所以把 ORDER BY 的表达式
    追加到 SELECT 列表、LIMIT 放宽一行后再比较；别名引用先还原成它指向的表达式。
    """
    tree = sqlglot.parse_one(sql, read="sqlite")
    limit, order = tree.args.get("limit"), tree.args.get("order")
    if limit is None or order is None:
        return False
    k = int(limit.expression.name)
    aliases = {e.alias: e.this for e in tree.expressions if isinstance(e, exp.Alias)}
    keys = []
    for ordered in order.expressions:
        key = ordered.this
        if isinstance(key, exp.Column) and not key.table and key.name in aliases:
            key = aliases[key.name]
        keys.append(key.copy())
    probe = tree.copy().select(*[e.as_(f"_k{i}") for i, e in enumerate(keys)], append=True).limit(k + 1)
    rows = conn.execute(probe.sql(dialect="sqlite")).fetchall()
    if len(rows) <= k:
        return False
    n = len(keys)
    return rows[k - 1][-n:] == rows[k][-n:]


def generate(
    db_path: str | Path = OLIST_DB_PATH,
    out_dir: str | Path = "eval/cases",
    seed: int = SEED,
) -> dict:
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Olist 库不存在: {db_path}（先运行 make olist-db）")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    valid, dropped = [], []
    try:
        for question, gold_sql, tags in _templates():
            try:
                rows = conn.execute(gold_sql).fetchmany(1)
                tie = _ties_at_cut(conn, gold_sql)
            except sqlite3.Error as e:
                dropped.append(f"{question} → SQL 错误: {e}")
                continue
            if not rows or all(v in (None, 0) for v in rows[0]):
                dropped.append(f"{question} → 空结果或 0")
                continue
            if tie:
                dropped.append(f"{question} → 截断处并列")
                continue
            valid.append({"question": question, "gold_sql": gold_sql, "tags": tags})
    finally:
        conn.close()

    rng = random.Random(seed)
    rng.shuffle(valid)
    for i, case in enumerate(valid, start=1):
        case["id"] = f"olist-{i:04d}"
    split_at = int(len(valid) * DEV_RATIO)
    dev, holdout = valid[:split_at], valid[split_at:]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, cases in (("olist-dev", dev), ("olist-holdout", holdout)):
        path = out_dir / f"{name}.jsonl"
        header = (
            f"# Olist 真实数据评测集 {name}：{len(cases)} 条（seed={seed}，70/30 切分，gold 逐条执行校验）\n"
            "# 库：data/olist/olist.sqlite（make olist-db）；口径：eval/knowledge/olist/glossary.jsonl\n"
            "# ⚠️ holdout 只用于最终复核，调 prompt/检索期间绝不允许跑\n"
        )
        ordered = [
            {"id": c["id"], "question": c["question"], "gold_sql": c["gold_sql"], "tags": c["tags"]}
            for c in cases
        ]
        path.write_text(
            header + "\n".join(json.dumps(c, ensure_ascii=False) for c in ordered) + "\n",
            encoding="utf-8",
        )
        files[name] = str(path)
    return {"total": len(valid), "dev": len(dev), "holdout": len(holdout), "dropped": dropped, "files": files}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="生成 Olist 真实数据评测集")
    parser.add_argument("--db", default=str(OLIST_DB_PATH))
    args = parser.parse_args()
    info = generate(args.db)
    print(f"共 {info['total']} 条（dev {info['dev']} / holdout {info['holdout']}），已写入 {info['files']}")
    for d in info["dropped"]:
        print(f"  [剔除] {d}")
