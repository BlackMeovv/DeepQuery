"""评测统计：Wilson 置信区间、题目聚类校正、按题配对比较。

为什么需要：单次跑分的"提升了 7 个点"可能只是抖动。
- Wilson 区间回答"这个准确率的不确定度有多大"（小样本下比正态近似稳健）；
- 题目聚类校正：同一道题重复跑 3 次，三次结果高度相关（难题总是错、易题总是对），
  不能当成 3 倍的独立样本——否则区间被算窄。用设计效应把样本量折算回有效样本量；
- 按题配对比较回答"两个配置在同一批题上的差异是否真实"：逐题取成功率之差，
  给出平均差的置信区间，并对"变好/变差"的题做符号检验（repeats=1 时即精确 McNemar）。
纯标准库实现，不引入 scipy。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb, sqrt


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """成功率的 Wilson score 置信区间（默认 95%）。返回 (low, high)。"""
    if total > 0 and (successes < 0 or successes > total):
        raise ValueError(f"successes 必须在 [0, {total}] 内，实际 {successes}")
    return wilson_interval_float(successes, total, z)


@dataclass
class ClusteredInterval:
    """按题目聚类校正后的准确率区间。"""

    accuracy: float  # 各题成功率的平均（= 合并准确率，各题重复次数相同时）
    low: float
    high: float
    design_effect: float  # 1 = 重复之间完全独立；越大说明重复越"抱团"
    n_items: int
    n_trials: int
    n_effective: float  # 折算后的有效独立样本量


def clustered_interval(item_trials: list[list[bool]], z: float = 1.96) -> ClusteredInterval:
    """重复评测下的准确率区间：按题目聚类估设计效应，再用有效样本量算 Wilson 区间。

    设计效应 deff = 题目成功率的实测方差 / 独立假设下的二项方差（= 1+(m-1)·ICC），
    有效样本量 = 总试验数 / deff。repeats=1 时 deff≡1，退化为普通 Wilson 区间。
    """
    items = [t for t in item_trials if t]
    if not items:
        return ClusteredInterval(0.0, 0.0, 0.0, 1.0, 0, 0, 0.0)
    n = len(items)
    rates = [sum(t) / len(t) for t in items]
    n_trials = sum(len(t) for t in items)
    m = n_trials / n
    p = sum(rates) / n
    deff = 1.0
    if m > 1:
        if n > 1 and 0 < p < 1:
            var_items = sum((r - p) ** 2 for r in rates) / (n - 1)
            deff = min(m, max(1.0, var_items / (p * (1 - p) / m)))
        else:
            # 全对/全错时方差为 0，设计效应无法估计：保守地假设重复完全相关
            deff = m
    n_eff = n_trials / deff
    low, high = wilson_interval_float(p * n_eff, n_eff, z)
    return ClusteredInterval(p, low, high, deff, n, n_trials, n_eff)


def wilson_interval_float(successes: float, total: float, z: float = 1.96) -> tuple[float, float]:
    """允许非整数样本量的 Wilson 区间（有效样本量折算后通常不是整数）。"""
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    z2 = z * z
    denom = 1 + z2 / total
    center = (p + z2 / (2 * total)) / denom
    margin = (z / denom) * sqrt(p * (1 - p) / total + z2 / (4 * total * total))
    return (max(0.0, center - margin), min(1.0, center + margin))


@dataclass
class PairedComparison:
    """同一批题上 B 相对 A 的按题配对比较。"""

    mean_diff: float  # 按题成功率之差的平均（B - A）
    low: float
    high: float
    n_items: int
    improved: int  # B 比 A 成功率高的题数
    regressed: int  # B 比 A 成功率低的题数
    sign_p: float  # 改进/退步题数的双侧精确符号检验（repeats=1 时即精确 McNemar）

    @property
    def excludes_zero(self) -> bool:
        return self.low > 0 or self.high < 0

    def describe(self) -> str:
        return (
            f"按题配对差 {self.mean_diff * 100:+.2f}pt，95% CI "
            f"[{self.low * 100:+.2f}, {self.high * 100:+.2f}]"
            f"（{'不含 0' if self.excludes_zero else '含 0，无法区分'}）；"
            f"变好 {self.improved} 题 / 变差 {self.regressed} 题 / 共 {self.n_items} 题，"
            f"符号检验 p={self.sign_p:.4f}"
        )


def paired_comparison(a_rates: list[float], b_rates: list[float], z: float = 1.96) -> PairedComparison:
    """按题配对比较：输入同一批题（顺序对应）上两个配置的逐题成功率。

    相比"每题多数票折成对错再做 McNemar"，保留了重复内的部分对错信息，功效更高。
    平均差区间用正态近似（题数 ≥ 30 时可靠）。
    """
    if len(a_rates) != len(b_rates):
        raise ValueError(f"两组长度不一致: {len(a_rates)} vs {len(b_rates)}")
    n = len(a_rates)
    if n == 0:
        return PairedComparison(0.0, 0.0, 0.0, 0, 0, 0, 1.0)
    diffs = [b - a for a, b in zip(a_rates, b_rates)]
    mean = sum(diffs) / n
    se = sqrt(sum((d - mean) ** 2 for d in diffs) / (n - 1) / n) if n > 1 else 0.0
    up = sum(1 for d in diffs if d > 0)
    down = sum(1 for d in diffs if d < 0)
    return PairedComparison(mean, mean - z * se, mean + z * se, n, up, down, _sign_test(up, down))


def _sign_test(up: int, down: int) -> float:
    n = up + down
    if n == 0:
        return 1.0
    k = min(up, down)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / (2**n))


@dataclass
class McNemarResult:
    b: int  # A 对、B 错的题数
    c: int  # A 错、B 对的题数
    p_value: float  # 精确二项检验（双侧）
    significant_05: bool

    def describe(self) -> str:
        return (
            f"不一致题数 b(A对B错)={self.b}, c(A错B对)={self.c}, "
            f"精确双侧 p={self.p_value:.4f}"
            f"（{'显著' if self.significant_05 else '不显著'} @0.05）"
        )


def mcnemar_exact(a_correct: list[bool], b_correct: list[bool]) -> McNemarResult:
    """精确 McNemar 检验（二项版本，适合不一致题数较少的场景）。

    输入为同一批题上两个配置的逐题对错（顺序必须对应同一题）。
    """
    if len(a_correct) != len(b_correct):
        raise ValueError(f"两组长度不一致: {len(a_correct)} vs {len(b_correct)}")
    b = sum(1 for x, y in zip(a_correct, b_correct) if x and not y)
    c = sum(1 for x, y in zip(a_correct, b_correct) if not x and y)
    n = b + c
    if n == 0:
        return McNemarResult(b=b, c=c, p_value=1.0, significant_05=False)
    k = min(b, c)
    # 双侧精确 p：P(X <= k) * 2，X ~ Binomial(n, 0.5)，封顶 1
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2**n)
    p = min(1.0, 2 * tail)
    return McNemarResult(b=b, c=c, p_value=p, significant_05=p < 0.05)
