// 回答文本的展示层兜底：提示词已要求只写普通句子，这里再挡一次模型偶尔输出的 Markdown。
// 表格行直接去掉——查询结果本来就以表格单独展示，重复一遍只会更乱。
// 与后端 plain_answer 规则一致（流式输出途中后端还没清理，这里保证显示一致）
export function cleanAnswer(text: string): string {
  const all = text.split("\n");
  const kept = all.filter((line) => !/^\s*\|.*\|\s*$/.test(line));
  let out = kept
    .join("\n")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .trim();
  if (kept.length < all.length) out = out.replace(/(如下|是|为)?\s*[:：]$/, "见下方结果表。");
  return out.replace(/\n{3,}/g, "\n\n").trim();
}
