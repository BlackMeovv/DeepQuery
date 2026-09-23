// SQL 轻量着色：按设计稿的四色方案（关键字/字符串/数字/标点）
const KEYWORDS = new Set([
  "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "LIMIT", "JOIN", "LEFT", "INNER",
  "ON", "AS", "AND", "OR", "DESC", "ASC", "COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND",
  "DISTINCT", "DATE", "NOT", "IN", "WITH", "HAVING", "CASE", "WHEN", "THEN", "ELSE",
  "END", "NULL", "IS", "LIKE", "BETWEEN", "EXISTS", "UNION", "STRFTIME",
]);

export interface SqlToken { t: string; c: string }

export function tokenizeSql(sql: string): SqlToken[] {
  const out: SqlToken[] = [];
  const re = /('[^']*')|(\d+(?:\.\d+)?)|([A-Za-z_一-龥][\w一-龥]*)|(\s+)|(.)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(sql))) {
    if (m[1]) out.push({ t: m[0], c: "var(--str)" });
    else if (m[2]) out.push({ t: m[0], c: "var(--num)" });
    else if (m[3]) out.push({ t: m[0], c: KEYWORDS.has(m[0].toUpperCase()) ? "var(--kw)" : "var(--ink)" });
    else if (m[4]) out.push({ t: m[0], c: "var(--ink)" });
    else out.push({ t: m[0], c: "var(--pun)" });
  }
  return out;
}

// 展示用排版：守卫改写后的 SQL 是单行的，在窄面板里只看得到开头。
// 在主要子句前换行（跳过字符串字面量），不改变语义；复制按钮仍复制原文。
const CLAUSE = /\s+((?:LEFT |RIGHT |INNER |FULL |CROSS )?(?:OUTER )?JOIN|FROM|WHERE|GROUP BY|HAVING|ORDER BY|LIMIT|UNION(?: ALL)?)\s+/gi;

export function prettySql(sql: string): string {
  if (sql.includes("\n")) return sql;
  return sql
    .split(/('[^']*')/)
    .map((part, i) => (i % 2 === 1 ? part : part.replace(CLAUSE, "\n$1 ")))
    .join("");
}
