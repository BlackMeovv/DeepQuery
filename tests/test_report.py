from deepquery.evalkit.report import compare, paired_between, render_markdown


def fake_report(label, ids_trials: dict[str, list[bool]]):
    flat = [x for t in ids_trials.values() for x in t]
    return {
        "summary": {
            "label": label,
            "model": "m",
            "cases": len(ids_trials),
            "repeats": max(len(t) for t in ids_trials.values()),
            "ex_accuracy": sum(flat) / len(flat),
            "total_cost": 0.01,
            "avg_latency_ms": 100,
        },
        "results": [{"id": i, "ex_by_repeat": t} for i, t in ids_trials.items()],
    }


class TestReport:
    def test_compare_rows(self):
        rows = compare([fake_report("a", {"1": [True], "2": [False]})])
        assert rows[0]["label"] == "a" and "50.0%" in rows[0]["ex"]
        assert rows[0]["deff"] == "1.00"  # repeats=1：无聚类，设计效应恒为 1

    def test_ci_recomputed_from_results_not_summary(self):
        # 历史 JSON 的 summary 里是朴素区间：report 必须从逐题对错重算，而不是照抄
        rep = fake_report("a", {str(i): [i % 4 != 0] * 3 for i in range(40)})
        rep["summary"]["wilson_low"], rep["summary"]["wilson_high"] = 0.74, 0.76
        row = compare([rep])[0]
        assert "74.0%" not in row["ex"]
        assert float(row["deff"]) > 2.5  # 每题三次结果完全一致 → 设计效应≈3

    def test_paired_between_uses_common_items(self):
        a = fake_report("a", {"1": [True], "2": [False], "3": [False]})
        b = fake_report("b", {"1": [True], "2": [True], "3": [True], "4": [True]})  # 4 不重叠
        result, skipped = paired_between(a, b)
        assert result.n_items == 3 and skipped == 1
        assert result.improved == 2 and result.regressed == 0

    def test_markdown_output(self):
        md = render_markdown(compare([fake_report("baseline", {"1": [True]})]), "note")
        assert "| baseline |" in md and "按题配对比较" in md and "设计效应" in md
