"""内置数据集：程序生成的演示库，以及 Olist 巴西电商真实数据。

按数据库文件名识别，给出首页数据说明、示例问题和配套的业务口径文件；
换库只需要改 DB_PATH，说明、示例和口径会一起切换。

    uv run python -m deepquery.datasets ensure          # DB_PATH 指向的库不存在时自动生成 / 下载（容器启动时调用）
    uv run python -m deepquery.datasets build olist     # 手动生成某个数据集
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import demo_data, olist_data

# 未单独配置时使用的口径文件（也是演示库与评测使用的那一份）
DEFAULT_GLOSSARY = "eval/knowledge/glossary.jsonl"
DEFAULT_EXAMPLES = "eval/knowledge/examples.jsonl"


@dataclass(frozen=True)
class Dataset:
    key: str
    default_path: Path
    description: str
    build: Callable[[Path], Path]
    # 首页示例问题：{"q": 问题, "tag": 可选的小标签}
    samples: tuple[dict, ...] = ()
    glossary_path: str = DEFAULT_GLOSSARY
    examples_path: str = DEFAULT_EXAMPLES
    source: str = ""  # 数据来源与许可


DATASETS: dict[str, Dataset] = {
    "demo": Dataset(
        key="demo",
        default_path=demo_data.DEFAULT_PATH,
        description=demo_data.DESCRIPTION,
        build=demo_data.build,
        samples=(
            {"q": "各品类的成交金额分别是多少？"},
            {"q": "下单次数最多的前5名客户是谁？"},
            {"q": "哪个客户最好？", "tag": "会先问你口径"},
            {"q": "各城市的退货率是多少？", "tag": "库里没有退货数据"},
        ),
    ),
    "olist": Dataset(
        key="olist",
        default_path=olist_data.DEFAULT_PATH,
        description=olist_data.DESCRIPTION,
        build=olist_data.build,
        samples=(
            {"q": "销售额最高的 5 个品类是哪些？"},
            {"q": "延迟送达的订单，评分会比准时的低多少？"},
            {"q": "哪个卖家最好？", "tag": "会先问你口径"},
            {"q": "各品类的退货率是多少？", "tag": "数据里没有退货记录"},
        ),
        glossary_path="eval/knowledge/olist/glossary.jsonl",
        examples_path="eval/knowledge/olist/examples.jsonl",
        source="Olist，Kaggle 公开数据集，CC BY-NC-SA 4.0",
    ),
}


def for_db(db_path: str) -> Dataset | None:
    """按数据库文件名识别内置数据集；服务器引擎（mysql:// 等）或其他文件返回 None。"""
    if "://" in db_path:
        return None
    name = Path(db_path).name
    return next((d for d in DATASETS.values() if d.default_path.name == name), None)


def knowledge_paths(settings) -> tuple[str, str]:
    """业务口径与例句文件：显式配置的优先；保持默认值时跟随当前数据集。"""
    ds = for_db(settings.db_path)
    glossary, examples = settings.glossary_path, settings.examples_path
    if ds is not None:
        if glossary == DEFAULT_GLOSSARY:
            glossary = ds.glossary_path
        if examples == DEFAULT_EXAMPLES:
            examples = ds.examples_path
    return glossary, examples


def ensure(db_path: str) -> int:
    """库文件不存在时按文件名生成对应的内置数据集。返回进程退出码。"""
    if "://" in db_path or Path(db_path).exists():
        return 0
    ds = for_db(db_path)
    if ds is None:
        print(f"数据库文件不存在：{db_path}（也不是内置数据集，请检查 DB_PATH）", file=sys.stderr)
        return 1
    try:
        out = ds.build(Path(db_path))
    except OSError as e:  # 下载失败（含 URLError）：给出可操作的提示，而不是一屏堆栈
        print(
            f"生成数据集 {ds.key} 失败：{e}\n"
            "服务器在国内时通常访问不了 Kaggle 的文件存储，可以在本机下载压缩包后手动导入，见 docs/DEPLOY.md「选择数据」",
            file=sys.stderr,
        )
        return 1
    print(f"已生成数据集 {ds.key}: {out}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="内置数据集")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ensure", help="DB_PATH 指向的库不存在时自动生成")
    b = sub.add_parser("build", help="生成指定数据集")
    b.add_argument("name", choices=sorted(DATASETS))
    b.add_argument("--out", help="输出路径（默认用数据集自己的路径）")
    args = parser.parse_args(argv)

    if args.cmd == "ensure":
        from .config import get_settings

        return ensure(get_settings().db_path)
    ds = DATASETS[args.name]
    out = ds.build(Path(args.out) if args.out else ds.default_path)
    print(f"已生成数据集 {ds.key}: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
