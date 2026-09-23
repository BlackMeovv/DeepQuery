import pytest

from deepquery.evalkit.stats import mcnemar_exact, wilson_interval


class TestWilson:
    def test_known_value(self):
        # 15/20 = 75%，Wilson 95% 区间约 (0.531, 0.888)
        low, high = wilson_interval(15, 20)
        assert low == pytest.approx(0.531, abs=0.01)
        assert high == pytest.approx(0.888, abs=0.01)

    def test_bounds(self):
        low, high = wilson_interval(0, 10)
        assert low == 0.0 and 0 < high < 0.35
        low, high = wilson_interval(10, 10)
        assert 0.65 < low < 1 and high == 1.0

    def test_empty(self):
        assert wilson_interval(0, 0) == (0.0, 0.0)

    def test_narrower_with_more_samples(self):
        w20 = wilson_interval(15, 20)
        w200 = wilson_interval(150, 200)
        assert (w200[1] - w200[0]) < (w20[1] - w20[0])

    def test_invalid(self):
        with pytest.raises(ValueError):
            wilson_interval(11, 10)


class TestMcNemar:
    def test_no_disagreement(self):
        result = mcnemar_exact([True, False], [True, False])
        assert result.p_value == 1.0 and not result.significant_05

    def test_symmetric_disagreement_not_significant(self):
        a = [True, False] * 5
        b = [False, True] * 5
        result = mcnemar_exact(a, b)
        assert result.b == 5 and result.c == 5
        assert result.p_value > 0.05

    def test_onesided_improvement_significant(self):
        # B 在 8 道题上翻对、0 道翻错：p = 2 * (1/2^8) = 0.0078
        a = [False] * 8 + [True] * 12
        b = [True] * 20
        result = mcnemar_exact(a, b)
        assert result.b == 0 and result.c == 8
        assert result.p_value == pytest.approx(2 / 256)
        assert result.significant_05

    def test_length_mismatch(self):
        with pytest.raises(ValueError):
            mcnemar_exact([True], [True, False])


class TestClusteredInterval:
    def test_single_repeat_equals_plain_wilson(self):
        from deepquery.evalkit.stats import clustered_interval

        trials = [[True]] * 70 + [[False]] * 30
        ci = clustered_interval(trials)
        low, high = wilson_interval(70, 100)
        assert ci.design_effect == 1.0
        assert abs(ci.low - low) < 1e-9 and abs(ci.high - high) < 1e-9

    def test_perfectly_correlated_repeats_collapse_to_item_count(self):
        from deepquery.evalkit.stats import clustered_interval

        # 每题三次结果完全相同：信息量只相当于 100 道题，而不是 300 次独立试验
        trials = [[True] * 3] * 70 + [[False] * 3] * 30
        ci = clustered_interval(trials)
        assert abs(ci.design_effect - 3.0) < 0.05
        naive = wilson_interval(210, 300)
        assert (ci.high - ci.low) > (naive[1] - naive[0])  # 校正后区间更宽
        assert abs(ci.n_effective - 100) < 2

    def test_independent_repeats_keep_deff_near_one(self):
        from deepquery.evalkit.stats import clustered_interval

        # 每题恰好 2 对 1 错：题与题之间无差异 → 方差低于二项方差 → 截断为 1
        ci = clustered_interval([[True, True, False]] * 50)
        assert ci.design_effect == 1.0

    def test_all_correct_is_stable(self):
        from deepquery.evalkit.stats import clustered_interval

        # 全对时设计效应不可估：保守按完全相关处理，有效样本 = 题数而不是试验数
        ci = clustered_interval([[True] * 3] * 20)
        assert ci.accuracy == 1.0 and ci.design_effect == 3.0
        assert abs(ci.n_effective - 20) < 1e-9 and abs(ci.high - 1.0) < 1e-9
        assert ci.low < wilson_interval(60, 60)[0]  # 比把 60 次当独立更保守


class TestPairedComparison:
    def test_binary_sign_test_equals_mcnemar(self):
        from deepquery.evalkit.stats import paired_comparison

        a = [True] * 10 + [False] * 5 + [True] * 2 + [False] * 3
        b = [True] * 10 + [True] * 5 + [False] * 2 + [False] * 3
        pc = paired_comparison([float(x) for x in a], [float(x) for x in b])
        mc = mcnemar_exact(a, b)
        assert (pc.improved, pc.regressed) == (mc.c, mc.b)
        assert abs(pc.sign_p - mc.p_value) < 1e-12

    def test_ci_and_direction(self):
        from deepquery.evalkit.stats import paired_comparison

        a = [2 / 3] * 30 + [1.0] * 30
        b = [1.0] * 30 + [1.0] * 30
        pc = paired_comparison(a, b)
        assert pc.mean_diff > 0 and pc.improved == 30 and pc.regressed == 0
        assert pc.excludes_zero

    def test_no_difference_ci_contains_zero(self):
        from deepquery.evalkit.stats import paired_comparison

        a = [1.0, 0.0] * 20
        b = [0.0, 1.0] * 20
        pc = paired_comparison(a, b)
        assert not pc.excludes_zero and abs(pc.mean_diff) < 1e-12
