"""公网演示费用防线：按访客限流 + 全站每日花费上限。"""

from datetime import date

from deepquery.ratelimit import DailyBudget, SlidingWindowLimiter


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestSlidingWindowLimiter:
    def test_blocks_after_limit_and_recovers(self):
        clock = FakeClock()
        lim = SlidingWindowLimiter(3, 60.0, clock=clock)
        assert [lim.hit("a") for _ in range(4)] == [True, True, True, False]
        clock.t += 59
        assert lim.hit("a") is False  # 窗口内仍超限
        clock.t += 2
        assert lim.hit("a") is True  # 最早的请求滑出窗口

    def test_keys_are_isolated(self):
        lim = SlidingWindowLimiter(1, 60.0, clock=FakeClock())
        assert lim.hit("a") and lim.hit("b")
        assert not lim.hit("a")

    def test_zero_means_disabled(self):
        lim = SlidingWindowLimiter(0)
        assert all(lim.hit("a") for _ in range(100))


class TestDailyBudget:
    def test_exceeded_then_resets_next_day(self):
        day = {"d": date(2026, 9, 1)}
        budget = DailyBudget(1.0, today=lambda: day["d"])
        assert not budget.exceeded()
        budget.add(0.6)
        assert not budget.exceeded()
        budget.add(0.5)
        assert budget.exceeded()
        day["d"] = date(2026, 9, 2)
        assert not budget.exceeded() and budget.spent == 0.0

    def test_zero_means_disabled(self):
        budget = DailyBudget(0.0)
        budget.add(1e9)
        assert not budget.exceeded()
