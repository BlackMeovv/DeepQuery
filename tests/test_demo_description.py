"""网页上给访客看的数据说明必须与实际生成的演示库一致。"""

from deepquery.demo_data import DESCRIPTION


def test_description_matches_generated_db(db):
    count = lambda sql: db.run_query(sql).rows[0][0]  # noqa: E731
    assert f"{count('SELECT COUNT(*) FROM customers')} 位客户" in DESCRIPTION
    assert f"{count('SELECT COUNT(DISTINCT city) FROM customers')} 个城市" in DESCRIPTION
    assert f"{count('SELECT COUNT(*) FROM categories')} 个品类" in DESCRIPTION
    assert f"共 {count('SELECT COUNT(*) FROM products')} 个商品" in DESCRIPTION
    assert f"{count('SELECT COUNT(*) FROM orders')} 笔订单" in DESCRIPTION
    first, last = db.run_query("SELECT MIN(order_date), MAX(order_date) FROM orders").rows[0]
    assert f"{int(first[:4])} 年 {int(first[5:7])} 月" in DESCRIPTION
    assert f"{int(last[:4])} 年 {int(last[5:7])} 月" in DESCRIPTION


def test_healthz_exposes_note_for_demo_db(settings, db):
    from fastapi.testclient import TestClient

    from deepquery.agent import DeepQuery
    from deepquery.llm import MockLLM
    from deepquery.server import create_app

    app = create_app(agent=DeepQuery(settings, db, MockLLM(["x"])), settings=settings)
    with TestClient(app) as c:
        assert c.get("/healthz").json()["dataset_note"] == DESCRIPTION
    custom = settings.model_copy(update={"dataset_note": "自定义说明"})
    app = create_app(agent=DeepQuery(custom, db, MockLLM(["x"])), settings=custom)
    with TestClient(app) as c:
        assert c.get("/healthz").json()["dataset_note"] == "自定义说明"
