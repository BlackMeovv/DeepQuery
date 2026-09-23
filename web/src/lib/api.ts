// 后端 API 封装。契约见仓库 docs/frontend-spec.md

// ---- 演示部署访问口令（后端配了 DEMO_ACCESS_CODE 时启用）----
// EventSource 无法携带自定义请求头，所以统一走 code 查询参数。
const CODE_KEY = "dq_access_code";
let accessCode = localStorage.getItem(CODE_KEY) || "";

export function setAccessCode(code: string) {
  accessCode = code;
  try {
    localStorage.setItem(CODE_KEY, code);
  } catch { /* 隐私模式下存不了就算了，本次会话内仍生效 */ }
}

function codeQS(): string {
  return accessCode ? `&code=${encodeURIComponent(accessCode)}` : "";
}

// ---- 访客 ID：公网演示时每个浏览器一份独立的记忆，互相看不到、改不了 ----
// 本机单人使用保持 "default"（与 CLI 的 `deepquery remember` 共用同一份记忆）
const UID_KEY = "dq_visitor_id";
let userId = "default";

export function useVisitorId() {
  try {
    let id = localStorage.getItem(UID_KEY);
    if (!id) {
      id = randomId();
      localStorage.setItem(UID_KEY, id);
    }
    userId = id;
  } catch {
    userId = randomId(); // 存不了就只在本次会话内隔离
  }
}

function randomId(): string {
  // getRandomValues 在 HTTP 下也可用（randomUUID 只在 HTTPS / localhost 下存在）
  const bytes = crypto.getRandomValues(new Uint8Array(10));
  return "v-" + Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

/** 校验口令是否正确（不传则用已保存的口令；口令保护未开启时恒为 true）。 */
export async function ping(code = accessCode): Promise<boolean> {
  const qs = code ? `?code=${encodeURIComponent(code)}` : "";
  return (await fetch(`/api/ping${qs}`)).ok;
}

export interface EnvInfo {
  ok: boolean;
  cache: string;
  mock: boolean;
  db: string;
  model: string;
  protected?: boolean;
}

export interface SchemaColumn { name: string; type: string }
export interface SchemaTable { name: string; columns: SchemaColumn[] }

export interface MemoryNote { id: number; note: string; created_at: string }

export interface NodeEvent {
  node: string;
  label: string;
  thought?: string;
  ok?: boolean;
  error_kind?: string;
  error_message?: string;
}

export interface Usage {
  llm_calls: number;
  total_tokens: number;
  cost: number;
}

export interface Attempt {
  sql: string;
  ok: boolean;
  error_kind?: string | null;
  error_message?: string | null;
}

export interface FinalPayload {
  status: "ok" | "ok_empty" | "failed" | "budget_exceeded";
  cached: boolean;
  answer: string;
  sql: string | null;
  predicted_sql: string | null;
  columns: string[];
  rows: (string | number | null)[][];
  row_count: number;
  attempts: Attempt[];
  selected_tables: string[] | null;
  context_used: { glossary: string[]; examples: string[]; memories: string[] } | null;
  hallucination_blocked: boolean;
  chart_url: string | null;
  chart_error: string | null;
  usage: Usage;
  latency_ms: number;
}

export async function fetchEnv(): Promise<EnvInfo> {
  return (await fetch("/healthz")).json();
}

export async function fetchSchema(): Promise<SchemaTable[]> {
  const data = await (await fetch("/api/schema")).json();
  return data.tables;
}

export async function fetchMemory(user = userId): Promise<MemoryNote[]> {
  const data = await (await fetch(`/api/memory?user=${encodeURIComponent(user)}${codeQS()}`)).json();
  return data.notes;
}

export async function addMemory(note: string, user = userId): Promise<number> {
  const resp = await fetch(accessCode ? `/api/memory?code=${encodeURIComponent(accessCode)}` : "/api/memory", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note, user }),
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.detail || "保存失败");
  return data.id;
}

export async function deleteMemory(id: number, user = userId): Promise<void> {
  await fetch(`/api/memory/${id}?user=${encodeURIComponent(user)}${codeQS()}`, { method: "DELETE" });
}

export interface AskCallbacks {
  onNode: (event: NodeEvent) => void;
  /** 回答逐字流式：text 是当前调用的累积全文（重写时整体替换即可） */
  onDelta?: (text: string) => void;
  onFinal: (payload: FinalPayload) => void;
  onError: () => void;
}

/** SSE 提问；返回的 EventSource 由调用方负责在 final/停止时 close。 */
export function askStream(
  question: string,
  chart: boolean,
  callbacks: AskCallbacks,
  user = userId,
  fresh = false,
): EventSource {
  const url =
    `/api/ask?question=${encodeURIComponent(question)}` +
    `&chart=${chart ? 1 : 0}&user=${encodeURIComponent(user)}` +
    `${fresh ? "&fresh=1" : ""}${codeQS()}`;
  const es = new EventSource(url);
  es.addEventListener("node", (e) => callbacks.onNode(JSON.parse((e as MessageEvent).data)));
  es.addEventListener("delta", (e) => callbacks.onDelta?.(JSON.parse((e as MessageEvent).data).text));
  es.addEventListener("final", (e) => {
    callbacks.onFinal(JSON.parse((e as MessageEvent).data));
    es.close();
  });
  es.onerror = () => {
    es.close();
    callbacks.onError();
  };
  return es;
}
