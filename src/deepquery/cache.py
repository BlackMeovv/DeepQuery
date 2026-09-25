"""问题级结果缓存：同一问题（+库+模型+参数）直接复用成功结果。

后端：配置 REDIS_URL 且安装 redis 包时用 Redis（多实例共享、重启不丢），
否则用进程内 LRU + TTL。只缓存成功结果（ok / ok_empty），失败永远重算。
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


def cache_key(
    question: str, *, db_path: str, model: str, chart: bool, schema: str = "", mode: str = "ask", history: str = ""
) -> str:
    # schema 指纹入 key：建/改表后旧缓存整体作废（数据增删的时效性由 TTL 兜底）
    fields = {"q": question.strip(), "db": db_path, "model": model, "chart": chart, "schema": schema}
    # 模式、对话上下文是独立字段，不拼进问题文本：否则问题里写"…|analyze"就能撞上别的模式的缓存。
    # 取默认值时不写入，已有缓存的键保持不变
    if mode != "ask":
        fields["mode"] = mode
    if history:
        fields["history"] = history
    payload = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return "deepquery:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BaseCache:
    backend = "none"

    def get(self, key: str) -> dict | None:
        raise NotImplementedError

    def set(self, key: str, value: dict) -> None:
        raise NotImplementedError


class MemoryCache(BaseCache):
    backend = "memory"

    def __init__(self, ttl_seconds: int = 600, max_entries: int = 256):
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._store: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        # 服务的多个请求线程并发读写：过期删除与淘汰交错时会 KeyError，所以整体加锁
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            expires_at, value = item
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: dict) -> None:
        with self._lock:
            self._store[key] = (time.monotonic() + self.ttl, value)
            self._store.move_to_end(key)
            while len(self._store) > self.max_entries:
                self._store.popitem(last=False)


class RedisCache(BaseCache):
    backend = "redis"

    def __init__(self, url: str, ttl_seconds: int = 600):
        import redis  # 可选依赖：uv sync --extra cache

        self.ttl = ttl_seconds
        self._client = redis.Redis.from_url(url, decode_responses=True)
        self._client.ping()

    def get(self, key: str) -> dict | None:
        try:
            raw = self._client.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None  # 缓存故障绝不阻断查询

    def set(self, key: str, value: dict) -> None:
        try:
            self._client.setex(key, self.ttl, json.dumps(value, ensure_ascii=False))
        except Exception:
            pass


def build_cache(settings: "Settings") -> BaseCache:
    if settings.redis_url:
        try:
            return RedisCache(settings.redis_url, settings.cache_ttl_seconds)
        except ImportError:
            print(
                "[deepquery] 配置了 REDIS_URL 但未安装 redis 包，"
                "已回退进程内缓存。启用：uv sync --extra cache",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"[deepquery] Redis 连接失败，已回退进程内缓存: {e}", file=sys.stderr)
    return MemoryCache(settings.cache_ttl_seconds)
