import { defineStore } from "pinia";
import {
  addMemory,
  askStream,
  deleteMemory,
  fetchEnv,
  fetchMemory,
  fetchSchema,
  ping,
  setAccessCode,
  type EnvInfo,
  type FinalPayload,
  type MemoryNote,
  type SchemaTable,
} from "../lib/api";

export type MsgStatus = "running" | "done" | "blocked" | "cached" | "stopped" | "failed";

export interface Step {
  label: string;
  thought?: string;
  err?: string;
  state: "run" | "ok" | "error";
}

export interface UserMsg { id: string; role: "user"; text: string }

export interface AiMsg {
  id: string;
  role: "ai";
  q: string;
  status: MsgStatus;
  steps: Step[];
  sql?: string | null;
  answer?: string;
  blockedText?: string;
  columns?: string[];
  rows?: (string | number | null)[][];
  rowCount?: number;
  chartUrl?: string | null;
  chartError?: string | null;
  attempts?: { sql: string; ok: boolean; error_kind?: string | null; error_message?: string | null }[];
  usage?: { calls: number; tokens: number; cost: number };
  latencyMs?: number;
  selectedTables?: string[] | null;
  contextUsed?: { glossary: string[]; examples: string[]; memories: string[] } | null;
  chart?: boolean; // 本次提问是否请求了图表
}

export type Msg = UserMsg | AiMsg;

interface Convo { id: string; title: string; msgs: Msg[] }

const NODE_LABELS: Record<string, string> = {
  generate_sql: "生成 SQL",
  execute: "守卫执行",
  repair: "修正并重试",
  chart: "生成图表",
  summarize: "归纳回答",
  fallback: "降级收尾",
};

const CONVOS_KEY = "ia2_convos";
const THEME_KEY = "ia2_theme";

function loadConvos(): Convo[] {
  try {
    return JSON.parse(localStorage.getItem(CONVOS_KEY) || "[]");
  } catch {
    return [];
  }
}

export const useAppStore = defineStore("app", {
  state: () => ({
    env: null as EnvInfo | null,
    schema: [] as SchemaTable[],
    mems: [] as MemoryNote[],
    convos: loadConvos(),
    curConvo: "" as string,
    msgs: [] as Msg[],
    draft: "",
    chartOn: false,
    running: false,
    panelId: null as string | null,
    theme: (localStorage.getItem(THEME_KEY) || "light") as "light" | "dark",
    stream: null as EventSource | null,
  }),

  getters: {
    panelMsg(state): AiMsg | null {
      const m = state.msgs.find((x) => x.id === state.panelId && x.role === "ai");
      return (m as AiMsg) || null;
    },
    lastAiId(state): string | null {
      const m = [...state.msgs].reverse().find((x) => x.role === "ai");
      return m ? m.id : null;
    },
    title(state): string {
      const c = state.convos.find((x) => x.id === state.curConvo);
      return state.msgs.length ? c?.title || "新的提问" : "新的提问";
    },
  },

  actions: {
    async init() {
      document.body.dataset.theme = this.theme;
      const env = await fetchEnv().catch(() => null);
      if (env) this.env = env;
      // 演示部署开启口令保护时：先验已存口令，不对再询问（最多三次，取消即放弃）
      if (env?.protected && !(await ping())) {
        for (let i = 0; i < 3; i++) {
          const input = window.prompt("本站为演示部署，请输入访问口令");
          if (input === null) break;
          setAccessCode(input.trim());
          if (await ping()) break;
        }
      }
      const [schema, mems] = await Promise.allSettled([fetchSchema(), fetchMemory()]);
      if (schema.status === "fulfilled") this.schema = schema.value;
      if (mems.status === "fulfilled") this.mems = mems.value;
    },

    toggleTheme() {
      this.theme = this.theme === "light" ? "dark" : "light";
      document.body.dataset.theme = this.theme;
      localStorage.setItem(THEME_KEY, this.theme);
    },

    insertToken(token: string) {
      this.draft = this.draft ? `${this.draft} ${token}` : token;
    },

    // ---- 会话管理（本地持久化）----
    persist() {
      const convo = this.convos.find((c) => c.id === this.curConvo);
      if (convo) convo.msgs = this.msgs;
      try {
        localStorage.setItem(CONVOS_KEY, JSON.stringify(this.convos.slice(0, 30)));
      } catch { /* 空间不足时放弃持久化 */ }
    },

    newChat() {
      this.stop();
      this.curConvo = "";
      this.msgs = [];
      this.panelId = null;
      this.draft = "";
    },

    pickConvo(id: string) {
      this.stop();
      const convo = this.convos.find((c) => c.id === id);
      if (!convo) return;
      this.curConvo = id;
      this.msgs = convo.msgs;
      this.panelId = null;
    },

    renameConvo(id: string, title: string) {
      const t = title.trim();
      const c = this.convos.find((x) => x.id === id);
      if (c && t) {
        c.title = t.slice(0, 40);
        this.persist();
      }
    },

    delConvo(id: string) {
      if (id === this.curConvo) {
        this.stop();
        this.curConvo = "";
        this.msgs = [];
        this.panelId = null;
        this.draft = "";
      }
      this.convos = this.convos.filter((c) => c.id !== id);
      this.persist();
    },

    // ---- 记忆 ----
    async addMem(note: string) {
      await addMemory(note);
      this.mems = await fetchMemory();
    },
    async delMem(id: number) {
      await deleteMemory(id);
      this.mems = this.mems.filter((m) => m.id !== id);
    },

    // ---- 提问主流程 ----
    ask(question: string, fresh = false) {
      const q = question.trim();
      if (!q || this.running) return;

      if (!this.curConvo) {
        const convo: Convo = { id: String(Date.now()), title: q.slice(0, 16), msgs: [] };
        this.convos.unshift(convo);
        this.curConvo = convo.id;
      }

      const aiId = "a" + Date.now();
      const ai: AiMsg = { id: aiId, role: "ai", q, status: "running", steps: [], chart: this.chartOn };
      this.msgs.push({ id: "u" + Date.now(), role: "user", text: q });
      this.msgs.push(ai);
      this.draft = "";
      this.running = true;
      // 运行过程在消息内下拉展示；右栏只有已打开时才跟随到新一次运行
      if (this.panelId) this.panelId = aiId;

      const patch = (obj: Partial<AiMsg>) => {
        const m = this.msgs.find((x) => x.id === aiId) as AiMsg | undefined;
        if (m) Object.assign(m, obj);
      };

      this.stream = askStream(q, this.chartOn, {
        onDelta: (text) => {
          const m = this.msgs.find((x) => x.id === aiId) as AiMsg;
          if (m.status === "running") m.answer = text;
        },
        onNode: (e) => {
          const m = this.msgs.find((x) => x.id === aiId) as AiMsg;
          m.steps.push({
            label: NODE_LABELS[e.node] || e.label,
            thought: e.thought,
            err: e.ok === false ? `${e.error_kind || ""}${e.error_message ? "：" + e.error_message : ""}` : undefined,
            state: e.ok === false ? "error" : "ok",
          });
        },
        onFinal: (p: FinalPayload) => {
          patch({
            status: p.cached ? "cached" : p.hallucination_blocked ? "blocked" : p.status.startsWith("ok") ? "done" : "failed",
            sql: p.sql,
            answer: p.hallucination_blocked ? undefined : p.answer,
            blockedText: p.hallucination_blocked ? p.answer : undefined,
            columns: p.columns,
            rows: p.rows,
            rowCount: p.row_count,
            chartUrl: p.chart_url,
            chartError: p.chart_error,
            attempts: p.attempts,
            selectedTables: p.selected_tables,
            contextUsed: p.context_used,
            usage: { calls: p.usage.llm_calls, tokens: p.usage.total_tokens, cost: p.usage.cost },
            latencyMs: p.latency_ms,
          });
          this.running = false;
          this.stream = null;
          this.persist();
        },
        onError: () => {
          patch({ status: "failed" });
          const m = this.msgs.find((x) => x.id === aiId) as AiMsg;
          m.steps.push({ label: "连接中断", state: "error", err: "SSE 连接中断" });
          this.running = false;
          this.stream = null;
          this.persist();
        },
      }, "default", fresh);
    },

    stop() {
      if (!this.running) return;
      this.stream?.close();
      this.stream = null;
      const m = [...this.msgs].reverse().find((x) => x.role === "ai" && (x as AiMsg).status === "running") as AiMsg | undefined;
      if (m) {
        m.status = "stopped";
        m.steps.push({ label: "已被用户停止", state: "error" });
      }
      this.running = false;
      this.persist();
    },
  },
});

/**
 * 运行中"正在进行的一步"。后端在节点完成时才推事件，所以只能由最后完成的一步推断：
 * 直接显示最后一条会把"守卫执行 ✓"误标成"正在守卫执行"，而此时其实在归纳回答。
 */
export function nextStepOf(m: AiMsg): string {
  if (m.answer) return "归纳回答"; // 已经在逐字输出回答
  const last = m.steps[m.steps.length - 1];
  if (!last) return "生成 SQL";
  if (last.label === "守卫执行") {
    if (last.state === "error") return "修正并重试";
    return m.chart ? "生成图表" : "归纳回答";
  }
  if (last.label === "生成图表") return "归纳回答";
  return "守卫执行"; // 生成 SQL / 修正并重试之后都是执行
}

export function pillOf(status: MsgStatus) {
  return {
    running: { t: "正在运行", i: "", c: "var(--accdeep)", bg: "var(--accbg)" },
    done: { t: "运行过程", i: "✓", c: "var(--acc2deep)", bg: "var(--acc2bg)" },
    blocked: { t: "已拦截", i: "!", c: "var(--err)", bg: "var(--errbg)" },
    cached: { t: "运行过程", i: "≡", c: "var(--accink)", bg: "var(--accbg)" },
    stopped: { t: "已停止", i: "×", c: "var(--warn)", bg: "var(--warnbg)" },
    failed: { t: "未完成", i: "×", c: "var(--err)", bg: "var(--errbg)" },
  }[status];
}
