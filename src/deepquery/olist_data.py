"""Olist 巴西电商公开数据集 → SQLite（真实数据的演示库）。

Olist 是巴西的电商平台，在 Kaggle 上公开了 2016–2018 年约 10 万笔真实订单（已匿名化），
许可 CC BY-NC-SA 4.0（非商业使用，需注明出处）：
https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce

导入时做了三件事，让中文提问更顺：
1. 列名去掉冗余前缀、修正原数据的拼写（product_name_lenght 等），并在建表语句里写中文注释
   ——注释会随 schema 一起交给模型；
2. 补充品类中文名（categories.name_zh）和州中文名 / 所属大区（states 表）；
3. 给外键建索引：几张 10 万行的表做 JOIN 时不建索引会慢到超时。
地理坐标表（100 万行、与查询关系不大）不导入。

    uv run python -m deepquery.olist_data                 # 从 Kaggle 下载并生成 data/olist/olist.sqlite
    uv run python -m deepquery.olist_data --src 压缩包.zip  # 已手动下载时
"""

from __future__ import annotations

import csv
import io
import sqlite3
import sys
import urllib.request
import zipfile
from pathlib import Path

SOURCE_URL = "https://www.kaggle.com/api/v1/datasets/download/olistbr/brazilian-ecommerce"
DEFAULT_PATH = Path("data/olist/olist.sqlite")

DESCRIPTION = (
    "巴西电商平台 Olist 公开的真实订单数据（已匿名化）：2016 年 9 月至 2018 年 10 月约 10 万笔订单，"
    "9.6 万位客户、3 千多个卖家、3.3 万个商品，含支付、物流时间和买家评分；金额单位为巴西雷亚尔。"
)

SCHEMA = """
CREATE TABLE states (                    -- 巴西的州（地区维表）
    code TEXT PRIMARY KEY,               -- 州缩写，如 SP；customers.state 与 sellers.state 引用它
    name TEXT NOT NULL,                  -- 州名（葡萄牙语），如 São Paulo
    name_zh TEXT NOT NULL,               -- 州名（中文），如 圣保罗州
    region_zh TEXT NOT NULL              -- 所属大区：北部 / 东北部 / 中西部 / 东南部 / 南部
);
CREATE TABLE categories (                -- 商品品类维表
    name TEXT PRIMARY KEY,               -- 品类原名（葡萄牙语），products.category 引用它
    name_en TEXT NOT NULL,               -- 品类英文名
    name_zh TEXT NOT NULL                -- 品类中文名，展示品类时用它
);
CREATE TABLE products (                  -- 商品表
    id TEXT PRIMARY KEY,
    category TEXT,                       -- 品类原名 → categories.name；约 600 个商品没有品类（NULL）
    weight_g INTEGER,                    -- 重量（克）
    length_cm INTEGER,
    height_cm INTEGER,
    width_cm INTEGER,
    photos_qty INTEGER                   -- 商品图片数
);
CREATE TABLE customers (                 -- 客户表。注意 id 是"订单级"的客户 ID：每个订单一个，
                                         -- 同一个人下多次单会有多个 id；统计客户人数要用 unique_id 去重
    id TEXT PRIMARY KEY,                 -- 订单级客户 ID，orders.customer_id 引用它（一对一）
    unique_id TEXT NOT NULL,             -- 真实客户 ID：同一个人的多个订单相同
    zip_prefix TEXT,                     -- 邮编前 5 位
    city TEXT NOT NULL,                  -- 城市，小写葡萄牙语且无重音，如 sao paulo、rio de janeiro
    state TEXT NOT NULL                  -- 州缩写 → states.code
);
CREATE TABLE sellers (                   -- 卖家表（平台上的商家）
    id TEXT PRIMARY KEY,
    zip_prefix TEXT,
    city TEXT NOT NULL,                  -- 城市，小写葡萄牙语且无重音
    state TEXT NOT NULL                  -- 州缩写 → states.code
);
CREATE TABLE orders (                    -- 订单表
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,           -- → customers.id
    status TEXT NOT NULL,                -- 订单状态: delivered 已送达 / shipped 已发货 / canceled 已取消 /
                                         -- unavailable 缺货取消 / invoiced 已开票 / processing 处理中 /
                                         -- created 已创建 / approved 已审核
    purchased_at TEXT NOT NULL,          -- 下单时间 'YYYY-MM-DD HH:MM:SS'
    approved_at TEXT,                    -- 付款审核通过时间
    shipped_at TEXT,                     -- 交给物流的时间
    delivered_at TEXT,                   -- 买家签收时间（未送达为 NULL）
    estimated_delivery_at TEXT           -- 平台承诺的送达日期
);
CREATE TABLE order_items (               -- 订单明细：一件商品一行，同一商品买 2 件就是 2 行
    order_id TEXT NOT NULL,              -- → orders.id
    item_no INTEGER NOT NULL,            -- 订单内序号 1, 2, 3…
    product_id TEXT NOT NULL,            -- → products.id
    seller_id TEXT NOT NULL,             -- → sellers.id
    shipping_limit_at TEXT,              -- 卖家最晚发货时间
    price REAL NOT NULL,                 -- 商品成交价（雷亚尔）
    freight REAL NOT NULL,               -- 该件商品分摊的运费（雷亚尔）
    PRIMARY KEY (order_id, item_no)
);
CREATE TABLE payments (                  -- 支付记录：一个订单可以分多笔支付（如信用卡 + 代金券）
    order_id TEXT NOT NULL,              -- → orders.id
    seq INTEGER NOT NULL,                -- 该订单的第几笔支付
    type TEXT NOT NULL,                  -- 支付方式: credit_card 信用卡 / boleto 银行票据 /
                                         -- voucher 代金券 / debit_card 借记卡 / not_defined 未知
    installments INTEGER,                -- 分期期数（1 = 不分期）
    amount REAL NOT NULL,                -- 支付金额（雷亚尔，含运费）
    PRIMARY KEY (order_id, seq)
);
CREATE TABLE reviews (                   -- 买家评价：多数订单一条，少数订单有多条
    id TEXT NOT NULL,
    order_id TEXT NOT NULL,              -- → orders.id
    score INTEGER NOT NULL,              -- 评分 1–5 星
    title TEXT,                          -- 评价标题（葡萄牙语，多数为空）
    comment TEXT,                        -- 评价内容（葡萄牙语，多数为空）
    created_at TEXT,                     -- 评价时间
    answered_at TEXT                     -- 平台回复时间
);
"""

INDEXES = """
CREATE INDEX idx_customers_unique ON customers(unique_id);
CREATE INDEX idx_customers_state ON customers(state);
CREATE INDEX idx_products_category ON products(category);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_purchased ON orders(purchased_at);
CREATE INDEX idx_items_product ON order_items(product_id);
CREATE INDEX idx_items_seller ON order_items(seller_id);
CREATE INDEX idx_reviews_order ON reviews(order_id);
"""

# 州：缩写 → (葡萄牙语名, 中文名, 所属大区)
STATES = {
    "AC": ("Acre", "阿克里州", "北部"),
    "AL": ("Alagoas", "阿拉戈斯州", "东北部"),
    "AM": ("Amazonas", "亚马孙州", "北部"),
    "AP": ("Amapá", "阿马帕州", "北部"),
    "BA": ("Bahia", "巴伊亚州", "东北部"),
    "CE": ("Ceará", "塞阿拉州", "东北部"),
    "DF": ("Distrito Federal", "联邦区（巴西利亚）", "中西部"),
    "ES": ("Espírito Santo", "圣埃斯皮里图州", "东南部"),
    "GO": ("Goiás", "戈亚斯州", "中西部"),
    "MA": ("Maranhão", "马拉尼昂州", "东北部"),
    "MG": ("Minas Gerais", "米纳斯吉拉斯州", "东南部"),
    "MS": ("Mato Grosso do Sul", "南马托格罗索州", "中西部"),
    "MT": ("Mato Grosso", "马托格罗索州", "中西部"),
    "PA": ("Pará", "帕拉州", "北部"),
    "PB": ("Paraíba", "帕拉伊巴州", "东北部"),
    "PE": ("Pernambuco", "伯南布哥州", "东北部"),
    "PI": ("Piauí", "皮奥伊州", "东北部"),
    "PR": ("Paraná", "巴拉那州", "南部"),
    "RJ": ("Rio de Janeiro", "里约热内卢州", "东南部"),
    "RN": ("Rio Grande do Norte", "北里奥格兰德州", "东北部"),
    "RO": ("Rondônia", "朗多尼亚州", "北部"),
    "RR": ("Roraima", "罗赖马州", "北部"),
    "RS": ("Rio Grande do Sul", "南里奥格兰德州", "南部"),
    "SC": ("Santa Catarina", "圣卡塔琳娜州", "南部"),
    "SE": ("Sergipe", "塞尔希培州", "东北部"),
    "SP": ("São Paulo", "圣保罗州", "东南部"),
    "TO": ("Tocantins", "托坎廷斯州", "北部"),
}

# 品类：原名 → 中文名（英文名取自数据集自带的对照表；对照表缺的两个品类在 _EXTRA_EN 里补）
CATEGORY_ZH = {
    "agro_industria_e_comercio": "农业工商",
    "alimentos": "食品",
    "alimentos_bebidas": "食品饮料",
    "artes": "艺术品",
    "artes_e_artesanato": "手工艺品",
    "artigos_de_festas": "派对用品",
    "artigos_de_natal": "圣诞用品",
    "audio": "音频设备",
    "automotivo": "汽车用品",
    "bebes": "母婴",
    "bebidas": "饮料",
    "beleza_saude": "美妆健康",
    "brinquedos": "玩具",
    "cama_mesa_banho": "床品卫浴",
    "casa_conforto": "家居舒适",
    "casa_conforto_2": "家居舒适（二）",
    "casa_construcao": "家装建材",
    "cds_dvds_musicais": "音乐 CD/DVD",
    "cine_foto": "影音摄影",
    "climatizacao": "空调温控",
    "consoles_games": "游戏机与游戏",
    "construcao_ferramentas_construcao": "建筑工具",
    "construcao_ferramentas_ferramentas": "五金工具",
    "construcao_ferramentas_iluminacao": "照明灯具",
    "construcao_ferramentas_jardim": "建材园艺工具",
    "construcao_ferramentas_seguranca": "安防工具",
    "cool_stuff": "潮流好物",
    "dvds_blu_ray": "DVD 与蓝光碟",
    "eletrodomesticos": "家用电器",
    "eletrodomesticos_2": "家用电器（二）",
    "eletronicos": "电子产品",
    "eletroportateis": "小家电",
    "esporte_lazer": "运动休闲",
    "fashion_bolsas_e_acessorios": "箱包配饰",
    "fashion_calcados": "鞋靴",
    "fashion_esporte": "运动服饰",
    "fashion_roupa_feminina": "女装",
    "fashion_roupa_infanto_juvenil": "童装",
    "fashion_roupa_masculina": "男装",
    "fashion_underwear_e_moda_praia": "内衣泳装",
    "ferramentas_jardim": "园艺工具",
    "flores": "鲜花",
    "fraldas_higiene": "纸尿裤与卫生用品",
    "industria_comercio_e_negocios": "工商业用品",
    "informatica_acessorios": "电脑配件",
    "instrumentos_musicais": "乐器",
    "la_cuisine": "厨具",
    "livros_importados": "进口图书",
    "livros_interesse_geral": "大众图书",
    "livros_tecnicos": "专业图书",
    "malas_acessorios": "旅行箱包",
    "market_place": "综合市集",
    "moveis_colchao_e_estofado": "床垫与软体家具",
    "moveis_cozinha_area_de_servico_jantar_e_jardim": "厨房餐厅与花园家具",
    "moveis_decoracao": "家具装饰",
    "moveis_escritorio": "办公家具",
    "moveis_quarto": "卧室家具",
    "moveis_sala": "客厅家具",
    "musica": "音乐",
    "papelaria": "文具",
    "pc_gamer": "游戏电脑",
    "pcs": "电脑整机",
    "perfumaria": "香水",
    "pet_shop": "宠物用品",
    "portateis_casa_forno_e_cafe": "烤箱与咖啡机",
    "portateis_cozinha_e_preparadores_de_alimentos": "厨房料理小家电",
    "relogios_presentes": "手表礼品",
    "seguros_e_servicos": "保险与服务",
    "sinalizacao_e_seguranca": "标识与安全用品",
    "tablets_impressao_imagem": "平板与打印",
    "telefonia": "手机通讯",
    "telefonia_fixa": "固定电话",
    "utilidades_domesticas": "家居日用",
}
_EXTRA_EN = {
    "pc_gamer": "pc_gamer",
    "portateis_cozinha_e_preparadores_de_alimentos": "small_appliances_kitchen_food_preparers",
}

_FILES = {
    "customers": "olist_customers_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "orders": "olist_orders_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "translation": "product_category_name_translation.csv",
}


def download(dest: Path, url: str = SOURCE_URL) -> Path:
    """下载 Kaggle 上的公开压缩包（约 45MB）；先写临时文件，完整后再改名。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"正在下载 Olist 数据集：{url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while chunk := resp.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / total:6.1%}  {done >> 20} / {total >> 20} MB", end="", file=sys.stderr)
    print(file=sys.stderr)
    tmp.replace(dest)
    return dest


def _rows(src: Path, key: str):
    """按表读 CSV：src 可以是 Kaggle 的压缩包，也可以是解压后的目录。"""
    name = _FILES[key]
    if src.is_dir():
        with open(src / name, encoding="utf-8-sig", newline="") as f:
            yield from csv.DictReader(f)
    else:
        with zipfile.ZipFile(src) as zf, zf.open(name) as raw:
            yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


def _s(value: str) -> str | None:
    value = (value or "").strip()
    return value or None


def _i(value: str) -> int | None:
    value = (value or "").strip()
    return int(float(value)) if value else None


def build(db_path: str | Path = DEFAULT_PATH, src: str | Path | None = None) -> Path:
    """生成 Olist SQLite 库。src 为空时自动从 Kaggle 下载（生成后删除压缩包）。"""
    out = Path(db_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    downloaded = src is None
    source = download(out.parent / "olist.zip") if downloaded else Path(src)

    tmp = out.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO states VALUES (?, ?, ?, ?)",
            [(code, *info) for code, info in sorted(STATES.items())],
        )

        english = {r["product_category_name"]: r["product_category_name_english"] for r in _rows(source, "translation")}
        english.update(_EXTRA_EN)
        products = [
            (
                r["product_id"],
                _s(r["product_category_name"]),
                _i(r["product_weight_g"]),
                _i(r["product_length_cm"]),
                _i(r["product_height_cm"]),
                _i(r["product_width_cm"]),
                _i(r["product_photos_qty"]),
            )
            for r in _rows(source, "products")
        ]
        used = sorted({p[1] for p in products if p[1]} | set(english))
        conn.executemany(
            "INSERT INTO categories VALUES (?, ?, ?)",
            [(c, english.get(c, c), CATEGORY_ZH.get(c, c)) for c in used],
        )
        conn.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?)", products)

        conn.executemany(
            "INSERT INTO customers VALUES (?, ?, ?, ?, ?)",
            (
                (r["customer_id"], r["customer_unique_id"], _s(r["customer_zip_code_prefix"]),
                 r["customer_city"], r["customer_state"])
                for r in _rows(source, "customers")
            ),
        )
        conn.executemany(
            "INSERT INTO sellers VALUES (?, ?, ?, ?)",
            (
                (r["seller_id"], _s(r["seller_zip_code_prefix"]), r["seller_city"], r["seller_state"])
                for r in _rows(source, "sellers")
            ),
        )
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (r["order_id"], r["customer_id"], r["order_status"], r["order_purchase_timestamp"],
                 _s(r["order_approved_at"]), _s(r["order_delivered_carrier_date"]),
                 _s(r["order_delivered_customer_date"]), _s(r["order_estimated_delivery_date"]))
                for r in _rows(source, "orders")
            ),
        )
        conn.executemany(
            "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (r["order_id"], int(r["order_item_id"]), r["product_id"], r["seller_id"],
                 _s(r["shipping_limit_date"]), float(r["price"]), float(r["freight_value"]))
                for r in _rows(source, "order_items")
            ),
        )
        conn.executemany(
            "INSERT INTO payments VALUES (?, ?, ?, ?, ?)",
            (
                (r["order_id"], int(r["payment_sequential"]), r["payment_type"],
                 _i(r["payment_installments"]), float(r["payment_value"]))
                for r in _rows(source, "payments")
            ),
        )
        conn.executemany(
            "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (r["review_id"], r["order_id"], int(r["review_score"]), _s(r["review_comment_title"]),
                 _s(r["review_comment_message"]), _s(r["review_creation_date"]), _s(r["review_answer_timestamp"]))
                for r in _rows(source, "reviews")
            ),
        )
        conn.executescript(INDEXES)
        conn.commit()
        conn.execute("ANALYZE")  # 给查询规划器统计信息，JOIN 顺序选得更好
        conn.commit()
    finally:
        conn.close()
    tmp.replace(out)
    if downloaded:
        source.unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="导入 Olist 巴西电商公开数据集")
    parser.add_argument("target", nargs="?", default=str(DEFAULT_PATH))
    parser.add_argument("--src", help="已下载的 Kaggle 压缩包或解压后的目录；不填则自动下载")
    args = parser.parse_args()
    out = build(args.target, args.src)
    print(f"Olist 数据库已生成: {out}")
