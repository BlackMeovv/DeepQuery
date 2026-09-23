"""消融对比报告：把多个带 label 的评测结果汇成一张表。

    python -m deepquery.evalkit.report eval/results/a.json eval/results/b.json \
        --out eval/results/report.md

这张表就是消融实验结论的原始材料：每行一个配置，EX 带按题目聚类校正的
95% 置信区间，成本与延迟并列；传入两个及以上报告时，对前两个做按题配对比较
（平均差区间 + 符号检验）。区间直接从各题逐次对错重算，历史结果 JSON 同样适用。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .stats import clustered_interval, paired_comparison

console = Console()


def load_report(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _item_trials(report: dict) -> dict[str, list[bool]]:
    """逐题的逐次对错（repeats=1 时每题一个值）。"""
    out = {}
    for r in report["results"]:
        trials = r.get("ex_by_repeat")
        if trials is None and "ex" in r:
            trials = [bool(r["ex"])]
        if trials:
            out[r["id"]] = [bool(x) for x in trials]
    return out


def compare(reports: list[dict]) -> list[dict]:
    rows = []
    for report in reports:
        s = report["summary"]
        ci = clustered_interval(list(_item_trials(report).values()))
        rows.append(
            {
                "label": s.get("label", "?"),
                "model": s.get("model") or "-",
                "ex": f"{ci.accuracy:.1%} [{ci.low:.1%}, {ci.high:.1%}]",
                "trials": f"{s.get('repeats', 1)}×{s['cases']}",
                "deff": f"{ci.design_effect:.2f}",
                "cost": f"{s.get('total_cost', 0):.4f}",
                "latency": f"{s.get('avg_latency_ms', 0)} ms",
            }
        )
    return rows


def paired_between(a: dict, b: dict):
    """B 相对 A 的按题配对比较（只用两份报告共有的题）。"""
    a_map, b_map = _item_trials(a), _item_trials(b)
    common = sorted(a_map.keys() & b_map.keys())
    if not common:
        raise ValueError("两个报告没有共同的 case id，无法做配对比较")
    skipped = (len(a_map) - len(common)) + (len(b_map) - len(common))
    rate = lambda t: sum(t) / len(t)  # noqa: E731
    result = paired_comparison([rate(a_map[i]) for i in common], [rate(b_map[i]) for i in common])
    return result, skipped


def render_markdown(rows: list[dict], paired_note: str = "") -> str:
    lines = [
        "# 评测对比报告",
        "",
        "EX 区间按题目聚类校正（同一题的重复不计为独立样本）；设计效应 1 表示重复之间独立。",
        "",
        "| 配置 | 模型 | EX（95% CI） | 重复×题数 | 设计效应 | 总成本 | 平均延迟 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['label']} | {r['model']} | {r['ex']} | {r['trials']} | {r['deff']} "
            f"| {r['cost']} | {r['latency']} |"
        )
    if paired_note:
        lines += ["", f"**按题配对比较（第二个相对第一个）**：{paired_note}"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="评测对比报告生成器")
    parser.add_argument("reports", nargs="+", help="runner 产出的结果 JSON 文件")
    parser.add_argument("--out", default=None, help="markdown 输出路径")
    args = parser.parse_args()

    reports = [load_report(p) for p in args.reports]
    rows = compare(reports)

    table = Table(title="评测对比（EX 区间按题目聚类校正）")
    for col in ("配置", "模型", "EX（95% CI）", "重复×题数", "设计效应", "总成本", "平均延迟"):
        table.add_column(col)
    for r in rows:
        table.add_row(r["label"], r["model"], r["ex"], r["trials"], r["deff"], r["cost"], r["latency"])
    console.print(table)

    note = ""
    if len(reports) >= 2:
        result, skipped = paired_between(reports[0], reports[1])
        note = result.describe() + (f"（{skipped} 条不重叠已跳过）" if skipped else "")
        console.print(f"按题配对比较（第二个相对第一个）：{note}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(render_markdown(rows, note), encoding="utf-8")
        console.print(f"已写入 {args.out}")


if __name__ == "__main__":
    main()
