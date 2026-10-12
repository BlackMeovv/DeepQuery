"""deepquery：企业数据分析 Agent。

最小用法：
    from deepquery import build_agent
    agent = build_agent()
    outcome = agent.ask("上海的客户一共有多少个？")
"""

import logging

from .agent import DeepQuery, RunOutcome
from .config import Settings, get_settings


def build_agent(settings: Settings | None = None, *, wait_catalog: bool = False) -> DeepQuery:
    """按 .env 配置组装 agent 实例（含可选的 Langfuse 追踪）。

    LLM_MOCK=1 时使用循环 MockLLM（演示/压测服务链路用，不调真实模型）——
    绝不能在评测里使用。
    wait_catalog：CATALOG_ENABLED 时，是否等建档 / 同步跑完再返回。命令行一问一答的进程
    要等（不然进程退出时后台线程还没建完）；常驻服务不等，建档在后台跑，提问照常进行。
    """
    from .llm import LLMClient, MockLLM
    from .memory import MemoryStore
    from .tools.engines import open_database
    from .tracing import build_tracer

    settings = settings or get_settings()
    db = open_database(
        settings.db_path,
        timeout_seconds=settings.sql_timeout_seconds,
        max_rows=settings.sql_max_rows,
    )
    if settings.llm_mock:
        llm = MockLLM(
            [
                "mock 演示。\n```sql\nSELECT status, COUNT(*) AS cnt FROM orders GROUP BY status\n```",
                "（mock 模式）各状态订单数量见结果表。",
            ],
            cycle=True,
        )
    else:
        llm = LLMClient(settings)
    memory = MemoryStore(settings.memory_db_path)
    catalog = _build_catalog(settings, db, None if settings.llm_mock else llm)
    agent = DeepQuery(
        settings,
        db,
        llm,
        tracer=build_tracer(settings),
        memory=memory,
        catalog=catalog,
    )
    if catalog is not None:
        if wait_catalog:
            # 一问一答的进程：后台线程活不到任务做完，改为当场同步、到期的取值当场刷新
            catalog.auto_background = False
            try:
                catalog.sync(wait_lock=60)  # 服务正在同步时等它做完，而不是跳过
                if catalog.due_tables():
                    catalog.refresh_values()
            except Exception:  # noqa: BLE001 —— 建档失败不影响提问，退回只用建表语句
                logging.getLogger("deepquery.catalog").warning("建档失败，本次只用建表语句", exc_info=True)
        else:
            catalog.sync_in_background()  # 启动时对一遍：服务停机期间表结构可能变过
    return agent


def _build_catalog(settings: Settings, db, llm):
    if not settings.catalog_enabled:
        return None
    from .catalog import Catalog

    try:
        return Catalog.from_settings(settings, db, llm=llm)
    except Exception:  # noqa: BLE001 —— 档案库打不开（路径不可写等）：关掉建档，不影响提问
        logging.getLogger("deepquery.catalog").warning(
            "档案库打不开，本次不建档：%s", settings.catalog_db_path, exc_info=True
        )
        return None


__all__ = ["DeepQuery", "RunOutcome", "Settings", "get_settings", "build_agent"]
