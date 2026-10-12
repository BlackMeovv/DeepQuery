"""数据库自动建档与记忆同步。

    profiler  只读地读结构、采集取值特征（枚举取值 / 日期范围 / 格式样例 / 关联关系）
    store     档案库（DeepQuery 自己的 SQLite）：表卡片、表列说明、依赖索引、变更记录
    deps      例句 / 口径 / 用户记忆依赖了哪些表、列、取值
    enrich    用模型给缺注释的表和列补中文说明（标"待确认"，人工改过的不覆盖）
    manager   Catalog：按表级指纹增量同步、依赖复核与自动恢复、取值档案刷新、后台线程
"""

from .manager import Catalog, CatalogView, SyncReport, db_identity
from .store import CatalogStore

__all__ = ["Catalog", "CatalogView", "CatalogStore", "SyncReport", "db_identity"]
