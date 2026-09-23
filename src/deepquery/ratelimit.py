"""公网演示的两道费用防线：按访客限流 + 全站每日花费上限。

单次提问已有 token/金额预算熔断，但次数不受限——访问口令一旦外传，
账单就没有天花板。这里补上次数与总量两个维度。进程内实现，单实例部署足够；
多实例需要换成 Redis 计数。上限为 0 表示关闭。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone


class SlidingWindowLimiter:
    """每个 key 在最近 window 秒内最多 limit 次。"""

    def __init__(self, limit: int, window_seconds: float = 60.0, clock=time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """记一次请求；超限返回 False（超限的请求不计入）。"""
        if self.limit <= 0:
            return True
        now = self._clock()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] >= self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 10_000:  # 防止海量一次性 key 撑内存：清掉已过期的
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window]:
                    del self._hits[k]
            return True


class DailyBudget:
    """全站每日模型花费上限（按 UTC 日期滚动）。

    检查在开跑前、记账在跑完后，所以上限最多被在途请求超出
    "并发数 × 单次预算"——单次预算本身有熔断，超出量是有界的。
    """

    def __init__(self, limit: float, today=lambda: datetime.now(timezone.utc).date()):
        self.limit = limit
        self._today = today
        self._day = today()
        self._spent = 0.0
        self._lock = threading.Lock()

    def _roll(self) -> None:
        day = self._today()
        if day != self._day:
            self._day, self._spent = day, 0.0

    def exceeded(self) -> bool:
        if self.limit <= 0:
            return False
        with self._lock:
            self._roll()
            return self._spent >= self.limit

    def add(self, cost: float) -> None:
        with self._lock:
            self._roll()
            self._spent += max(0.0, cost)

    @property
    def spent(self) -> float:
        with self._lock:
            self._roll()
            return self._spent
