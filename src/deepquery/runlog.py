"""运行记录与用户反馈：每次提问留一条记录（也是审计日志），用户的 👍/👎 挂在具体那次运行上。

用途：
- 审计：谁在什么时候问了什么、执行了哪条 SQL、花了多少钱；
- 数据飞轮：差评连同问题、上下文、SQL 一起导出成待标注的评测用例，
  补上标准答案后并入评测集——线上遇到的问题变成以后每次改动都要过的回归题。

存问题、SQL、回答文本（最多 2000 字）和用量，不单独存查询结果；但回答被降级为"结果预览"时
（数字对不上出处、生成回答失败），这段预览会作为回答文本一起存下。按条数上限滚动删除最旧的记录。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    user TEXT NOT NULL,
    question TEXT NOT NULL,
    history TEXT,          -- 追问时带的之前几轮（JSON）
    mode TEXT NOT NULL,    -- ask / analyze
    status TEXT NOT NULL,
    cached INTEGER NOT NULL DEFAULT 0,
    sql TEXT,              -- 模型原始 SQL（没有守卫注入的 LIMIT）
    answer TEXT,
    row_count INTEGER,
    attempts INTEGER,
    detail TEXT,           -- 分析模式各步骤等补充信息（JSON）
    latency_ms INTEGER,
    tokens INTEGER,
    cost REAL,
    model TEXT
);
CREATE INDEX IF NOT EXISTS runs_created ON runs (created_at);
CREATE TABLE IF NOT EXISTS feedback (
    run_id TEXT PRIMARY KEY REFERENCES runs (id) ON DELETE CASCADE,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    reason TEXT,
    created_at TEXT NOT NULL
);
"""


class RunLog:
    def __init__(self, db_path: str | Path, keep: int = 20000):
        self.db_path = Path(db_path)
        self.keep = keep
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def record(
        self,
        *,
        user: str,
        question: str,
        payload: dict,
        history: list[dict] | None = None,
        mode: str = "ask",
        model: str = "",
        detail: dict | None = None,
    ) -> str:
        """记一次运行，返回 run_id（交给前端，反馈时带回来）。"""
        run_id = uuid.uuid4().hex
        usage = payload.get("usage") or {}
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO runs (id, created_at, user, question, history, mode, status, cached, sql, answer,"
                " row_count, attempts, detail, latency_ms, tokens, cost, model)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    user,
                    question,
                    json.dumps(history, ensure_ascii=False) if history else None,
                    mode,
                    payload.get("status", ""),
                    1 if payload.get("cached") else 0,
                    payload.get("predicted_sql") or payload.get("sql"),
                    (payload.get("answer") or "")[:2000],
                    payload.get("row_count"),
                    len(payload.get("attempts") or []),
                    json.dumps(detail, ensure_ascii=False) if detail else None,
                    payload.get("latency_ms"),
                    usage.get("total_tokens"),
                    usage.get("cost"),
                    model,
                ),
            )
            # 滚动删除最旧的记录（反馈随之级联删除）
            conn.execute(
                "DELETE FROM runs WHERE id IN (SELECT id FROM runs ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (self.keep,),
            )
        return run_id

    def feedback(self, run_id: str, user: str, rating: str, reason: str = "") -> bool:
        """给一次运行打分；同一次运行再打一次会覆盖。只能给自己的运行打分（不存在或不是自己的都返回 False）。"""
        if rating not in ("up", "down"):
            raise ValueError("rating 只能是 up 或 down")
        with self._connect() as conn:
            owner = conn.execute("SELECT user FROM runs WHERE id = ?", (run_id,)).fetchone()
            if not owner or owner[0] != user:
                return False
            conn.execute(
                "INSERT INTO feedback (run_id, rating, reason, created_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (run_id) DO UPDATE SET rating = excluded.rating, reason = excluded.reason,"
                " created_at = excluded.created_at",
                (run_id, rating, (reason or "").strip()[:200] or None, time.strftime("%Y-%m-%d %H:%M:%S")),
            )
        return True

    def stats(self, days: int = 7) -> dict:
        """最近 N 天的概况：各状态次数、缓存命中、延迟、花费、好评差评。"""
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * 86400))
        with self._connect() as conn:
            by_status = dict(conn.execute(
                "SELECT status, COUNT(*) FROM runs WHERE created_at >= ? GROUP BY status ORDER BY 2 DESC", (since,)
            ).fetchall())
            total, cached, cost = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(cached), 0), COALESCE(SUM(cost), 0) FROM runs WHERE created_at >= ?",
                (since,),
            ).fetchone()
            latencies = [r[0] for r in conn.execute(
                "SELECT latency_ms FROM runs WHERE created_at >= ? AND cached = 0 AND latency_ms IS NOT NULL"
                " ORDER BY latency_ms", (since,)
            )]
            ratings = dict(conn.execute(
                "SELECT f.rating, COUNT(*) FROM feedback f JOIN runs r ON r.id = f.run_id"
                " WHERE r.created_at >= ? GROUP BY f.rating", (since,)
            ).fetchall())
        p50 = latencies[len(latencies) // 2] if latencies else None
        p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
        return {
            "days": days, "runs": total, "cached": cached, "cost": round(cost, 6),
            "by_status": by_status, "latency_p50_ms": p50, "latency_p95_ms": p95,
            "up": ratings.get("up", 0), "down": ratings.get("down", 0),
        }

    def downvoted(self, limit: int = 500) -> list[dict]:
        """差评的运行（最新的在前），同一个问题 + 同一条 SQL 只留一条。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.id, r.created_at, r.question, r.history, r.mode, r.status, r.sql, r.answer, f.reason"
                " FROM feedback f JOIN runs r ON r.id = f.run_id WHERE f.rating = 'down'"
                " ORDER BY f.created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out, seen = [], set()
        for run_id, created_at, question, history, mode, status, sql, answer, reason in rows:
            key = (question, sql or "")
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "run_id": run_id, "created_at": created_at, "question": question,
                "history": json.loads(history) if history else [], "mode": mode, "status": status,
                "predicted_sql": sql, "answer": answer, "reason": reason,
            })
        return out

    def export_cases(self, path: str | Path) -> int:
        """差评导出成待标注的评测用例（jsonl）：补上 gold_sql 后即可并入评测集。返回条数。"""
        cases = self.downvoted()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# 线上差评导出的待标注用例：逐条核对问题，补上 gold_sql（能跑通的正确 SQL），\n")
            f.write("# 再把整行挪进对应的评测集。predicted_sql 是当时模型写的 SQL，reason 是用户说的原因。\n")
            for c in cases:
                case = {
                    "id": f"fb-{c['run_id'][:8]}", "question": c["question"], "gold_sql": None,
                    "history": c["history"], "predicted_sql": c["predicted_sql"], "status": c["status"],
                    "reason": c["reason"], "answer": c["answer"], "created_at": c["created_at"],
                }
                f.write(json.dumps(case, ensure_ascii=False) + "\n")
        return len(cases)
