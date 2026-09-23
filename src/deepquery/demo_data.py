"""确定性电商演示库生成器。

固定随机种子：同一版本代码生成的库逐字节内容一致（时间戳字符串固定生成），
评测数字因此可复现。运行：`python -m deepquery.demo_data` 或 `make demo-db`。
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

SEED = 42
DEFAULT_PATH = Path("data/demo/ecommerce.sqlite")

# 给访客看的数据说明（网页空状态展示）。数字由 tests/test_demo_description.py 对照实际生成的库校验
DESCRIPTION = (
    "一家虚构电商 2024 年 1 月至 2025 年 6 月的经营数据：240 位客户分布在 10 个城市，"
    "6 个品类共 36 个商品，1500 笔订单及其明细与支付记录。"
)

_CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "南京", "重庆"]
_CATEGORIES = ["手机数码", "家用电器", "服饰鞋包", "美妆个护", "食品生鲜", "图书文娱"]
_PRODUCT_WORDS = {
    "手机数码": ["旗舰手机", "无线耳机", "智能手表", "平板电脑", "移动电源", "机械键盘"],
    "家用电器": ["变频空调", "滚筒洗衣机", "扫地机器人", "空气炸锅", "电饭煲", "净水器"],
    "服饰鞋包": ["羽绒服", "跑步鞋", "双肩包", "牛仔裤", "针织衫", "冲锋衣"],
    "美妆个护": ["精华液", "防晒霜", "电动牙刷", "洗发水", "面膜", "香水"],
    "食品生鲜": ["坚果礼盒", "进口牛排", "有机牛奶", "咖啡豆", "车厘子", "大米"],
    "图书文娱": ["科幻小说", "编程图书", "拼装积木", "桌游", "画具套装", "吉他"],
}
_BRANDS = ["星驰", "云澜", "沐野", "极光", "山海", "皓月", "青柠", "启明"]
_PRICE_RANGES = {
    "手机数码": (199, 8999),
    "家用电器": (299, 9999),
    "服饰鞋包": (59, 1999),
    "美妆个护": (29, 699),
    "食品生鲜": (19, 399),
    "图书文娱": (25, 899),
}
_STATUSES = ["completed", "completed", "completed", "completed", "shipped", "pending", "cancelled"]
_PAY_METHODS = ["alipay", "wechat", "card"]

_SCHEMA = """
CREATE TABLE customers (                -- 客户表：客户姓名、所在城市、注册日期、会员等级
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,                 -- 客户姓名
    city TEXT NOT NULL,                 -- 所在城市
    signup_date TEXT NOT NULL,          -- 注册日期 YYYY-MM-DD
    vip_level INTEGER NOT NULL DEFAULT 0 -- 会员等级: 0 普通, 1 银卡, 2 金卡, 3 钻石
);
CREATE TABLE categories (               -- 商品品类表
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL                  -- 品类名称
);
CREATE TABLE products (                 -- 商品表：所属品类、售价、成本
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,                 -- 商品名称
    category_id INTEGER NOT NULL REFERENCES categories(id),
    price REAL NOT NULL,                -- 售价（目录价）
    cost REAL NOT NULL                  -- 成本
);
CREATE TABLE orders (                   -- 订单表：下单客户、下单日期、订单状态
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date TEXT NOT NULL,           -- 下单日期 YYYY-MM-DD
    status TEXT NOT NULL                -- 订单状态: completed / shipped / pending / cancelled
);
CREATE TABLE order_items (              -- 订单明细表：每单购买的商品、数量与成交单价
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    quantity INTEGER NOT NULL,          -- 购买数量
    unit_price REAL NOT NULL            -- 成交单价（可能有折扣，不等于 products.price）
);
CREATE TABLE payments (                 -- 支付流水表：支付金额、支付方式、支付时间
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount REAL NOT NULL,               -- 支付金额
    method TEXT NOT NULL,               -- 支付方式: alipay / wechat / card
    paid_at TEXT NOT NULL               -- 支付日期 YYYY-MM-DD
);
"""


def build(db_path: str | Path = DEFAULT_PATH) -> Path:
    rng = random.Random(SEED)
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)

        conn.executemany(
            "INSERT INTO categories (id, name) VALUES (?, ?)",
            list(enumerate(_CATEGORIES, start=1)),
        )

        products = []
        pid = 0
        for cat_id, cat in enumerate(_CATEGORIES, start=1):
            for word in _PRODUCT_WORDS[cat]:
                pid += 1
                low, high = _PRICE_RANGES[cat]
                price = round(rng.uniform(low, high), 2)
                cost = round(price * rng.uniform(0.45, 0.8), 2)
                products.append((pid, f"{rng.choice(_BRANDS)}{word}", cat_id, price, cost))
        conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?)", products)

        customers = []
        # 注册日上限收在订单窗口终点（2025-06-30）之前，保证任何订单日都落在窗口内
        signup_span = (date(2025, 6, 29) - date(2024, 1, 1)).days + 1
        for cid in range(1, 241):
            signup = date(2024, 1, 1) + timedelta(days=rng.randrange(0, signup_span))
            vip = rng.choices([0, 1, 2, 3], weights=[55, 25, 15, 5])[0]
            customers.append(
                (cid, f"用户{cid:04d}", rng.choice(_CITIES), signup.isoformat(), vip)
            )
        conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers)

        orders, items, payments = [], [], []
        item_id = pay_id = 0
        # 固定抽取一批"注册后从未下单"的沉默客户，让留存类问题有意义
        inactive_ids = set(rng.sample(range(1, 241), 18))
        active_customers = [c for c in customers if c[0] not in inactive_ids]
        for oid in range(1, 1501):
            customer = rng.choice(active_customers)
            # 订单不早于注册日、不晚于窗口终点（注册日上限已保证 span >= 1）
            signup = date.fromisoformat(customer[3])
            latest = date(2025, 6, 30)
            span = (latest - signup).days
            order_date = signup + timedelta(days=rng.randrange(0, span))
            status = rng.choice(_STATUSES)
            orders.append((oid, customer[0], order_date.isoformat(), status))

            total = 0.0
            for _ in range(rng.randint(1, 4)):
                item_id += 1
                prod = rng.choice(products)
                qty = rng.randint(1, 3)
                unit_price = round(prod[3] * rng.uniform(0.8, 1.0), 2)
                items.append((item_id, oid, prod[0], qty, unit_price))
                total += qty * unit_price
            if status in ("completed", "shipped"):
                pay_id += 1
                paid_at = order_date + timedelta(days=rng.randrange(0, 3))
                payments.append(
                    (pay_id, oid, round(total, 2), rng.choice(_PAY_METHODS), paid_at.isoformat())
                )

        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?)", orders)
        conn.executemany("INSERT INTO order_items VALUES (?, ?, ?, ?, ?)", items)
        conn.executemany("INSERT INTO payments VALUES (?, ?, ?, ?, ?)", payments)
        conn.commit()
    finally:
        conn.close()
    return path


# ---------- MySQL / PostgreSQL 演示库导出 ----------

_DDL_MYSQL = """
CREATE TABLE customers (
    id INT PRIMARY KEY,
    name VARCHAR(64) NOT NULL COMMENT '客户姓名',
    city VARCHAR(32) NOT NULL COMMENT '所在城市',
    signup_date DATE NOT NULL COMMENT '注册日期',
    vip_level INT NOT NULL DEFAULT 0 COMMENT '会员等级: 0 普通, 1 银卡, 2 金卡, 3 钻石'
) COMMENT='客户表：客户姓名、所在城市、注册日期、会员等级';
CREATE TABLE categories (
    id INT PRIMARY KEY,
    name VARCHAR(32) NOT NULL COMMENT '品类名称'
) COMMENT='商品品类表';
CREATE TABLE products (
    id INT PRIMARY KEY,
    name VARCHAR(64) NOT NULL COMMENT '商品名称',
    category_id INT NOT NULL COMMENT '所属品类',
    price DECIMAL(10,2) NOT NULL COMMENT '售价（目录价）',
    cost DECIMAL(10,2) NOT NULL COMMENT '成本'
) COMMENT='商品表：所属品类、售价、成本';
CREATE TABLE orders (
    id INT PRIMARY KEY,
    customer_id INT NOT NULL COMMENT '下单客户',
    order_date DATE NOT NULL COMMENT '下单日期',
    status VARCHAR(16) NOT NULL COMMENT '订单状态: completed / shipped / pending / cancelled'
) COMMENT='订单表：下单客户、下单日期、订单状态';
CREATE TABLE order_items (
    id INT PRIMARY KEY,
    order_id INT NOT NULL,
    product_id INT NOT NULL,
    quantity INT NOT NULL COMMENT '购买数量',
    unit_price DECIMAL(10,2) NOT NULL COMMENT '成交单价（可能有折扣，不等于 products.price）'
) COMMENT='订单明细表：每单购买的商品、数量与成交单价';
CREATE TABLE payments (
    id INT PRIMARY KEY,
    order_id INT NOT NULL,
    amount DECIMAL(12,2) NOT NULL COMMENT '支付金额',
    method VARCHAR(16) NOT NULL COMMENT '支付方式: alipay / wechat / card',
    paid_at DATE NOT NULL COMMENT '支付日期'
) COMMENT='支付流水表：支付金额、支付方式、支付时间';
"""

_DDL_POSTGRES = """
CREATE TABLE customers (
    id INT PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    city VARCHAR(32) NOT NULL,
    signup_date DATE NOT NULL,
    vip_level INT NOT NULL DEFAULT 0
);
COMMENT ON TABLE customers IS '客户表：客户姓名、所在城市、注册日期、会员等级';
COMMENT ON COLUMN customers.city IS '所在城市';
COMMENT ON COLUMN customers.vip_level IS '会员等级: 0 普通, 1 银卡, 2 金卡, 3 钻石';
CREATE TABLE categories (id INT PRIMARY KEY, name VARCHAR(32) NOT NULL);
COMMENT ON TABLE categories IS '商品品类表';
CREATE TABLE products (
    id INT PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    category_id INT NOT NULL,
    price NUMERIC(10,2) NOT NULL,
    cost NUMERIC(10,2) NOT NULL
);
COMMENT ON TABLE products IS '商品表：所属品类、售价（目录价）、成本';
CREATE TABLE orders (
    id INT PRIMARY KEY,
    customer_id INT NOT NULL,
    order_date DATE NOT NULL,
    status VARCHAR(16) NOT NULL
);
COMMENT ON TABLE orders IS '订单表：下单客户、下单日期、订单状态';
COMMENT ON COLUMN orders.status IS '订单状态: completed / shipped / pending / cancelled';
CREATE TABLE order_items (
    id INT PRIMARY KEY,
    order_id INT NOT NULL,
    product_id INT NOT NULL,
    quantity INT NOT NULL,
    unit_price NUMERIC(10,2) NOT NULL
);
COMMENT ON TABLE order_items IS '订单明细表：每单购买的商品、数量与成交单价';
COMMENT ON COLUMN order_items.unit_price IS '成交单价（可能有折扣，不等于 products.price）';
CREATE TABLE payments (
    id INT PRIMARY KEY,
    order_id INT NOT NULL,
    amount NUMERIC(12,2) NOT NULL,
    method VARCHAR(16) NOT NULL,
    paid_at DATE NOT NULL
);
COMMENT ON TABLE payments IS '支付流水表：支付金额、支付方式、支付时间';
COMMENT ON COLUMN payments.method IS '支付方式: alipay / wechat / card';
"""


def dump_sql(dialect: str) -> str:
    """导出演示库为 MySQL/PostgreSQL 初始化脚本（DDL 带注释 + 全量数据）。"""
    import sqlite3
    import tempfile

    if dialect not in ("mysql", "postgres"):
        raise ValueError(f"不支持的方言: {dialect}（mysql / postgres）")

    with tempfile.TemporaryDirectory() as tmp:
        db_file = Path(tmp) / "demo.sqlite"
        build(db_file)
        conn = sqlite3.connect(db_file)
        try:
            lines = ["-- deepquery 演示库（确定性生成，seed=42）", ""]
            lines.append(_DDL_MYSQL if dialect == "mysql" else _DDL_POSTGRES)
            for table in ("categories", "customers", "products", "orders", "order_items", "payments"):
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()
                for start in range(0, len(rows), 500):
                    chunk = rows[start : start + 500]
                    values = ",\n".join(
                        "(" + ", ".join(_sql_literal(v) for v in row) + ")" for row in chunk
                    )
                    lines.append(f"INSERT INTO {table} VALUES\n{values};")
            return "\n".join(lines) + "\n"
        finally:
            conn.close()


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="演示库生成/导出")
    parser.add_argument("target", nargs="?", default=str(DEFAULT_PATH))
    parser.add_argument("--dump", choices=["mysql", "postgres"], help="导出为该方言的初始化 SQL（打印到 stdout）")
    args = parser.parse_args()
    if args.dump:
        print(dump_sql(args.dump))
    else:
        out = build(args.target)
        print(f"演示库已生成: {out}")
