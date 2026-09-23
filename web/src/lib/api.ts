// 后端 API 封装。契约见仓库 docs/frontend-spec.md

// ---- 演示部署访问口令（后端配了 DEMO_ACCESS_CODE 时启用）----
// 提问放在 POST 请求体里；其余 GET 接口走 code 查询参数。
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
  /** 数据说明：告诉访客这份数据是什么（内置数据集自动提供） */
  dataset_note?: string;
  dataset_source?: string; // 数据来源与许可（真实数据集才有）
  /** 首页示例问题，随数据集切换；tag 是可选的小标签 */
  samples?: { q: string; tag?: string }[];
}

export interface SchemaColumn { name: string; type: string }
export interface SchemaTable { name: string; columns: SchemaColumn[] }

export interface MemoryNote { id: number; note: string; created_at: string }

export interface NodeEvent {
  node: string;
  label: string;
  thought?: string;
  sql?: string; // generate_sql / repair 节点生成的 SQL
  detail?: string; // 补充说明，如"展开 orders 的完整定义；查看 orders.status 的真实取值"
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

/** Agent 拿不准时先向用户确认：question 是要问的话，options 是可选的理解（可能为空） */
export interface Clarification {
  question: string;
  term: string; // 需要确认的口径词，如"最好的客户"；数据缺失类确认时为空
  options: string[];
}

export interface FinalPayload {
  // ok_meta：问的是口径 / 表结构，依据 schema 直接回答，没有查询数据
  status: "ok" | "ok_empty" | "ok_meta" | "failed" | "budget_exceeded" | "needs_clarification";
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
  source_tables?: string[]; // 回答依据的数据来自哪几张表
  numbers_verified?: number; // 回答中核对过出处的数字个数
  chart_url: string | null;
  chart_error: string | null;
  clarification?: Clarification | null;
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
  /** 连接失败或中途断开；message 是能直接展示给用户的原因（没有时用默认说法） */
  onError: (message?: string) => void;
}

/** 同一会话里之前的一轮：让 Agent 听懂"那按月呢""这些用了哪些字段"这类追问 */
export interface HistoryTurn {
  question: string;
  sql?: string;
  answer?: string;
}

export interface AskRequest {
  question: string;
  chart: boolean;
  fresh?: boolean; // 跳过缓存强制重跑
  clarify?: boolean; // 回答过澄清的追问传 false，避免 Agent 反复追问
  history?: HistoryTurn[]; // 旧的在前，最多 3 轮
}

export interface AskHandle {
  close: () => void;
}

/**
 * 提问：POST + 流式读取 SSE。
 * 不用 EventSource：它只能发 GET，带上之前几轮的 SQL 后 URL 会超过 nginx 默认的请求行上限。
 */
export function askStream(req: AskRequest, callbacks: AskCallbacks, user = userId): AskHandle {
  const ctrl = new AbortController();
  let finished = false;

  const dispatch = (block: string) => {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue; // ": ping" 是服务端心跳
      const i = line.indexOf(":");
      const field = i < 0 ? line : line.slice(0, i);
      const value = i < 0 ? "" : line.slice(i + 1).replace(/^ /, "");
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    if (!data.length) return;
    const payload = JSON.parse(data.join("\n"));
    if (event === "node") callbacks.onNode(payload);
    else if (event === "delta") callbacks.onDelta?.(payload.text);
    else if (event === "final") {
      finished = true;
      callbacks.onFinal(payload);
    }
  };

  (async () => {
    try {
      const resp = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ ...req, user, code: accessCode || null }),
        signal: ctrl.signal,
      });
      if (!resp.ok || !resp.body) {
        callbacks.onError(
          resp.status === 401 ? "访问口令错误或已失效，请刷新页面重新输入"
            : resp.status === 422 ? "问题太长了，请精简到 2000 字以内"
            : resp.status >= 500 ? `服务暂时不可用（HTTP ${resp.status}，可能正在重启），稍后点「重跑」再试`
            : `服务拒绝了这次请求（HTTP ${resp.status}）`,
        );
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (!finished) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let cut: number;
        while (!finished && (cut = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, cut);
          buf = buf.slice(cut + 2);
          dispatch(block);
        }
      }
      if (finished) reader.cancel().catch(() => {});
    } catch {
      /* 网络错误或被 close() 中止：下面统一判断 */
    }
    if (!finished && !ctrl.signal.aborted) callbacks.onError();
  })();

  return { close: () => ctrl.abort() };
}
