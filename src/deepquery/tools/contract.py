"""工具统一契约。

铁律：① 输出必须截断/分页 ② 错误必须结构化（error_kind 供上游分类处理）
③ 绝不拼接字符串执行。
"""

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

# 统一错误分类：自纠错节点按这里的取值决定重写策略
ERROR_KINDS = (
    "syntax_error",  # SQL 语法错误（解析或执行期）
    "no_such_table",
    "no_such_column",
    "timeout",
    "guard_rejected",  # 守卫拒绝（越权/非 SELECT/多语句等）
    "empty_result",  # 执行成功但 0 行（可能条件写错，也可能确实没数据）
    "execution_error",  # 其他执行错误
)


@dataclass
class QueryResult:
    ok: bool
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    latency_ms: int = 0
    error_kind: str | None = None
    error_message: str | None = None

    def preview(self, max_rows: int = 10, max_cell: int = 80) -> str:
        """给 LLM 看的紧凑预览：列头 + 前若干行，单元格截断。"""
        if not self.ok:
            return f"[{self.error_kind}] {self.error_message}"
        lines = [" | ".join(self.columns)]
        for row in self.rows[:max_rows]:
            cells = []
            for cell in row:
                text = "NULL" if cell is None else str(cell)
                cells.append(text[:max_cell] + "…" if len(text) > max_cell else text)
            lines.append(" | ".join(cells))
        if self.row_count > max_rows:
            lines.append(f"…（共 {self.row_count} 行，仅展示前 {max_rows} 行）")
        if self.truncated:
            lines.append("（结果已按行数上限截断）")
        return "\n".join(lines)


def plain_value(value):
    """把驱动返回的值统一成 JSON 能直接序列化、数字校验能识别的基本类型。

    MySQL/PostgreSQL 的 SUM/NUMERIC 返回 Decimal、日期列返回 date/datetime：
    不转换的话 SSE 序列化会报错（钱已经花了却拿不到结果），数字溯源校验也认不出
    Decimal 里的数字，会把正确回答误判为幻觉。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() and abs(value) < 2**53 else float(value)
    if isinstance(value, (datetime.date, datetime.time)):  # datetime 是 date 的子类
        return value.isoformat(sep=" ") if isinstance(value, datetime.datetime) else value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)  # timedelta、UUID 等
