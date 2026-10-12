"""Schema RAG：混合检索（BM25 + 可选向量）选表，附业务字典与 few-shot 例句检索。

为什么需要：库一大（BIRD 的库常有十几张表、几百列），把全部建表语句塞进
prompt 既贵又降准确率（Lost in the Middle）。检索只喂相关的表。

设计：
- BM25 纯标准库实现（中文按字 + 相邻双字组合，英文按词），离线可测、零依赖；
- 向量检索可选：配置 EMBED_* 环境变量后启用（任何 OpenAI 兼容 embeddings 接口），
  未配置时退化为纯 BM25，功能完整；
- 两路结果用 RRF（Reciprocal Rank Fusion）融合，避免调分数权重。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:
    from .config import Settings

# ---------- 分词与 BM25 ----------

_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")
_HAN = re.compile(r"[一-鿿]")


def tokenize(text: str) -> list[str]:
    """中英混合分词：英文/数字按词，中文按单字 + 相邻双字组合。"""
    words = [w.lower() for w in _ASCII_WORD.findall(text or "")]
    han = _HAN.findall(text or "")
    bigrams = [a + b for a, b in zip(han, han[1:])]
    return words + han + bigrams


class BM25:
    """Okapi BM25，纯标准库实现。"""

    def __init__(self, docs: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [list(d) for d in docs]
        self.doc_len = [len(d) for d in self.docs]
        self.avgdl = (sum(self.doc_len) / len(self.docs)) if self.docs else 0.0
        self.tf: list[dict[str, int]] = []
        df: dict[str, int] = {}
        for doc in self.docs:
            counts: dict[str, int] = {}
            for token in doc:
                counts[token] = counts.get(token, 0) + 1
            self.tf.append(counts)
            for token in counts:
                df[token] = df.get(token, 0) + 1
        n = len(self.docs)
        self.idf = {t: math.log((n - d + 0.5) / (d + 0.5) + 1) for t, d in df.items()}

    def scores(self, query_tokens: Sequence[str]) -> list[float]:
        out = []
        for i in range(len(self.docs)):
            score = 0.0
            denom_norm = self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl) if self.avgdl else 0.0
            for token in query_tokens:
                tf = self.tf[i].get(token, 0)
                if tf == 0:
                    continue
                score += self.idf.get(token, 0.0) * tf * (self.k1 + 1) / (tf + denom_norm)
            out.append(score)
        return out

    def ranking(self, query: str) -> list[int]:
        """返回按相关度降序的文档下标（0 分的排在最后但仍返回）。"""
        scores = self.scores(tokenize(query))
        return sorted(range(len(scores)), key=lambda i: -scores[i])


def rrf_fuse(rankings: Sequence[Sequence[int]], k: int = 60) -> list[int]:
    """Reciprocal Rank Fusion：融合多路排序，免调权重。"""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            score[idx] = score.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return sorted(score, key=lambda i: -score[i])


# ---------- 可选向量检索 ----------

Embedder = Callable[[list[str]], list[list[float]]]


def build_embedder(settings: "Settings") -> Embedder | None:
    """OpenAI 兼容 embeddings 接口；未配置 EMBED_* 时返回 None（纯 BM25）。"""
    if not (settings.embed_api_key and settings.embed_model):
        return None
    from openai import OpenAI

    client = OpenAI(
        api_key=settings.embed_api_key,
        base_url=settings.embed_base_url or None,
        timeout=settings.llm_timeout_seconds,
    )

    def embed(texts: list[str]) -> list[list[float]]:
        resp = client.embeddings.create(model=settings.embed_model, input=texts)
        return [item.embedding for item in resp.data]

    return embed


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ---------- 面向业务的三个索引 ----------

class SchemaRetriever:
    """选表：question → 最相关的 top-k 张表（BM25 + 可选向量，RRF 融合）。"""

    def __init__(self, table_docs: dict[str, str], embedder: Embedder | None = None):
        self.names = list(table_docs)
        self.docs = [f"{name}\n{text}" for name, text in table_docs.items()]
        self._bm25 = BM25([tokenize(d) for d in self.docs])
        self._embedder = embedder
        self._doc_vectors = embedder(self.docs) if embedder else None

    def top_tables(self, question: str, k: int) -> list[str]:
        rankings = [self._bm25.ranking(question)]
        if self._embedder and self._doc_vectors:
            try:
                qv = self._embedder([question])[0]
                sims = [_cosine(qv, dv) for dv in self._doc_vectors]
                rankings.append(sorted(range(len(sims)), key=lambda i: -sims[i]))
            except Exception:
                pass  # 向量检索失败退化为纯 BM25，绝不阻断查询
        fused = rrf_fuse(rankings)
        return [self.names[i] for i in fused[:k]]


@dataclass
class TextEntry:
    key: str  # 展示用（术语名 / 例句问题）
    body: str  # 检索与注入用全文
    sql: str = ""  # 例句的 SQL（建档时据此登记依赖、复核时试编译）


class TextIndex:
    """业务字典 / few-shot 例句共用的轻量 BM25 索引。"""

    def __init__(self, entries: list[TextEntry]):
        self.entries = entries
        self._bm25 = BM25([tokenize(f"{e.key} {e.body}") for e in entries]) if entries else None

    def top(self, question: str, n: int, exclude: set[str] | None = None) -> list[TextEntry]:
        """exclude：不参与召回的条目 key（待复核的例句 / 口径），先排除再取前 n 条。"""
        if not self._bm25 or n <= 0:
            return []
        query_tokens = tokenize(question)
        scores = self._bm25.scores(query_tokens)
        ranked = sorted(range(len(scores)), key=lambda i: -scores[i])
        if exclude:
            ranked = [i for i in ranked if self.entries[i].key not in exclude]
        return [self.entries[i] for i in ranked[:n] if scores[i] > 0]


def load_glossary(path) -> TextIndex:
    """业务字典 jsonl：{"term": "GMV", "definition": "……"}"""
    import json
    from pathlib import Path

    entries = []
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                item = json.loads(line)
                entries.append(TextEntry(key=item["term"], body=f"{item['term']}：{item['definition']}"))
    return TextIndex(entries)


def load_examples(path) -> TextIndex:
    """few-shot 例句 jsonl：{"question": "……", "sql": "SELECT ……"}"""
    import json
    from pathlib import Path

    entries = []
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                item = json.loads(line)
                entries.append(
                    TextEntry(
                        key=item["question"],
                        body=f"问：{item['question']}\n```sql\n{item['sql']}\n```",
                        sql=item["sql"],
                    )
                )
    return TextIndex(entries)
