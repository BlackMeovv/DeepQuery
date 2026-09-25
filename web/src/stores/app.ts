import { defineStore } from "pinia";
import {
  addMemory,
  askStream,
  deleteMemory,
  fetchEnv,
  fetchMemory,
  fetchSchema,
  ping,
  sendFeedback,
  setAccessCode,
  useVisitorId,
  type AnalysisStep,
  type AskHandle,
  type Clarification,
  type EnvInfo,
  type FinalPayload,
  type HistoryTurn,
  type MemoryNote,
  type SchemaTable,
} from "../lib/api";

export type MsgStatus = "running" | "done" | "blocked" | "cached" | "stopped" | "failed" | "clarify";

export interface Step {
  label: string;
  thought?: string;
  detail?: string; // 如"展开了哪些表、查了哪些列的真实取值"
  sql?: string;
  err?: string;
  state: "run" | "ok" | "error";
}

export interface UserMsg { id: string; role: "user"; text: string }

/** 分析模式里的一步：state 由服务端的 status 映射而来，运行中为 pending */
export interface PlanStep {
  no: number;
  question: string;
  purpose?: string;
  state: "pending" | "ok" | "empty" | "error" | "skipped";
  sql?: string | null;
  summary?: string[];
  columns?: string[];
  rows?: (string | number | null)[][];
  rowCount?: number;
  error?: string | null;
}

function toPlanStep(s: AnalysisStep): PlanStep {
  const state = !s.status || s.status === "pending" ? "pending"
    : s.status === "ok" ? "ok"
    : s.status === "ok_empty" ? "empty"
    : s.status === "skipped" ? "skipped" : "error";
  return {
    no: s.no, question: s.question, purpose: s.purpose, state,
    sql: s.predicted_sql || s.sql, summary: s.summary, columns: s.columns, rows: s.rows,
    rowCount: s.row_count, error: s.error,
  };
}

export interface AiMsg {
  id: string;
  role: "ai";
  q: string;
  status: MsgStatus;
  steps: Step[];
  sql?: string | null;
  rawSql?: string | null; // 模型原始 SQL（没有守卫注入的 LIMIT），追问时作为上下文
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
  sourceTables?: string[];
  numbersVerified?: number;
  sqlSummary?: string[]; // 口径说明：筛选了什么、怎么分组排序、取多少条
  runId?: string | null; // 服务端这次运行的编号（打分用）
  rating?: "up" | "down"; // 用户给这次回答的评价
  ratingReason?: string;
  clarification?: Clarification | null; // Agent 拿不准时向用户提的确认
  clarifyAnswered?: string; // 用户对这次确认给出的回答
  noClarify?: boolean; // 这是回答确认后的追问：不再允许反问（重跑时沿用）
  clarifySkipped?: boolean; // 用户选择跳过确认、改问别的
  meta?: boolean; // 问的是口径 / 表结构：依据 schema 直接回答，没有查询数据
  chat?: boolean; // 打招呼、问"你是谁"这类闲聊：直接回应，不查数据、不算进追问上下文
  mode?: "analyze"; // 分析模式：拆成几步查询、写带出处的结论
  plan?: PlanStep[];
  planThought?: string;
  reviewNote?: string;
}

export type Msg = UserMsg | AiMsg;

interface Convo { id: string; title: string; msgs: Msg[] }

const NODE_LABELS: Record<string, string> = {
  browse_schema: "浏览表目录",
  generate_sql: "生成 SQL",
  execute: "守卫执行",
  repair: "修正并重试",
  chart: "生成图表",
  summarize: "归纳回答",
  fallback: "降级收尾",
  clarify: "需要向你确认",
  explain: "依据表结构回答",
  reply: "直接回复",
};

// 追问时带给后端的上下文：最近两轮已完成的问答（与后端 HistoryTurn 的长度上限一致）
const HISTORY_TURNS = 2;

function historyOf(msgs: Msg[]): HistoryTurn[] {
  const turns: HistoryTurn[] = [];
  for (const m of msgs) {
    if (m.role !== "ai" || !["done", "cached", "blocked"].includes(m.status) || m.chat) continue;
    if (!m.sql && !m.answer) continue;
    turns.push({
      question: m.q.slice(0, 500),
      // 用模型原始 SQL：守卫改写版末尾的 LIMIT 200 会被模型照抄，盖过"名单默认前 10"
      sql: (m.rawSql || m.sql || "").slice(0, 4000),
      answer: (m.answer || "").slice(0, 600),
    });
  }
  return turns.slice(-HISTORY_TURNS);
}

interface AskOptions {
  fresh?: boolean; // 跳过缓存强制重跑
  display?: string; // 对话里显示的用户消息（默认就是问题本身）
  clarify?: boolean; // 是否允许 Agent 先反问确认（默认允许）
  history?: HistoryTurn[]; // 之前几轮对话（默认取当前会话里最近完成的几轮）
  mode?: "ask" | "analyze"; // 默认跟随输入框的"深度分析"开关
}

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
    analyzeOn: false, // 深度分析：拆成几步查询再下结论
    running: false,
    panelId: null as string | null,
    theme: (localStorage.getItem(THEME_KEY) || "light") as "light" | "dark",
    stream: null as AskHandle | null,
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
    /** 最近一条回答是否正在等用户确认（输入框据此把输入当作回答） */
    pendingClarify(state): AiMsg | null {
      const last = state.msgs[state.msgs.length - 1];
      return last?.role === "ai" && last.status === "clarify" && !last.clarifyAnswered && !last.clarifySkipped ? last : null;
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
      if (env?.protected) useVisitorId(); // 公网演示：记忆按浏览器隔离
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
      try {
        await addMemory(note);
      } catch (e) {
        window.alert(e instanceof Error ? e.message : "保存失败");
      }
      this.mems = await fetchMemory();
    },
    async delMem(id: number) {
      await deleteMemory(id);
      this.mems = this.mems.filter((m) => m.id !== id);
    },

    // ---- 提问主流程 ----
    ask(question: string, opts: AskOptions = {}) {
      const q = question.trim();
      if (!q || this.running) return;
      const clarify = opts.clarify ?? true;
      const history = opts.history ?? historyOf(this.msgs);
      const mode = opts.mode ?? (this.analyzeOn ? "analyze" : "ask");

      if (!this.curConvo) {
        const convo: Convo = { id: String(Date.now()), title: q.slice(0, 16), msgs: [] };
        this.convos.unshift(convo);
        this.curConvo = convo.id;
      }

      const aiId = "a" + Date.now();
      const ai: AiMsg = {
        id: aiId, role: "ai", q, status: "running", steps: [], noClarify: !clarify,
        chart: mode === "ask" && this.chartOn, mode: mode === "analyze" ? "analyze" : undefined,
      };
      this.msgs.push({ id: "u" + Date.now(), role: "user", text: opts.display?.trim() || q });
      this.msgs.push(ai);
      this.draft = "";
      this.running = true;
      // 运行过程在消息内下拉展示；右栏只有已打开时才跟随到新一次运行
      if (this.panelId) this.panelId = aiId;

      const patch = (obj: Partial<AiMsg>) => {
        const m = this.msgs.find((x) => x.id === aiId) as AiMsg | undefined;
        if (m) Object.assign(m, obj);
      };

      this.stream = askStream({ question: q, chart: !!ai.chart, fresh: opts.fresh ?? false, clarify, history, mode }, {
        onDelta: (text) => {
          const m = this.msgs.find((x) => x.id === aiId) as AiMsg;
          if (m.status === "running") m.answer = text;
        },
        onNode: (e) => {
          const m = this.msgs.find((x) => x.id === aiId) as AiMsg;
          if (m.mode === "analyze") {
            // 分析模式：计划 → 每一步完成 → 追加的下钻步骤，都更新到 m.plan 上
            if (e.node === "plan") {
              m.plan = (e.steps || []).map(toPlanStep);
              m.planThought = e.thought;
            } else if (e.node === "run_step" && e.step) {
              const done = toPlanStep(e.step);
              const i = (m.plan || []).findIndex((s) => s.no === done.no);
              if (i >= 0) m.plan![i] = done;
              else m.plan = [...(m.plan || []), done];
            } else if (e.node === "review") {
              m.reviewNote = e.thought;
              if (e.added?.length) m.plan = [...(m.plan || []), ...e.added.map(toPlanStep)];
            }
            return;
          }
          if (e.node === "generate_sql" || e.node === "repair") {
            // 一次模型调用里先想后写：拆成"思考"和"生成 SQL"两步展示；只想不写（要向你确认）时没有第二步
            const first = e.node === "repair" ? "分析失败原因" : "理解问题";
            // 补充说明：修复前的观察（查了哪些取值、补看了哪些表）跟着"分析失败原因"；
            // 写 SQL 时的投票情况跟着"生成 SQL"
            const onSql = e.node === "generate_sql" && !!e.sql;
            if (e.thought || (e.detail && !onSql) || !e.sql) m.steps.push({ label: first, thought: e.thought, detail: onSql ? undefined : e.detail, state: "ok" });
            if (e.sql) m.steps.push({ label: e.node === "repair" ? "改写 SQL" : "生成 SQL", sql: e.sql, detail: onSql ? e.detail : undefined, state: "ok" });
            return;
          }
          m.steps.push({
            label: NODE_LABELS[e.node] || e.label,
            thought: e.thought,
            detail: e.detail,
            err: e.ok === false ? `${e.error_kind || ""}${e.error_message ? "：" + e.error_message : ""}` : undefined,
            state: e.ok === false ? "error" : "ok",
          });
        },
        onFinal: (p: FinalPayload) => {
          const needsClarify = p.status === "needs_clarification" && !!p.clarification;
          patch({
            status: needsClarify ? "clarify"
              : p.cached ? "cached"
              : p.hallucination_blocked ? "blocked"
              : p.status.startsWith("ok") ? "done" : "failed",
            clarification: needsClarify ? p.clarification : null,
            meta: p.status === "ok_meta",
            chat: p.status === "ok_chat",
            sql: p.sql,
            rawSql: p.predicted_sql,
            // 以服务端为准：打开分析模式时说"你好"，服务端会按普通回复处理
            mode: p.mode === "analyze" ? "analyze" : undefined,
            ...(p.mode === "analyze"
              ? { plan: (p.steps || []).map(toPlanStep), planThought: p.plan_thought, reviewNote: p.review_note }
              : {}),
            answer: p.hallucination_blocked || needsClarify ? undefined : p.answer,
            blockedText: p.hallucination_blocked ? p.answer : undefined,
            columns: p.columns,
            rows: p.rows,
            rowCount: p.row_count,
            chartUrl: p.chart_url,
            chartError: p.chart_error,
            attempts: p.attempts,
            selectedTables: p.selected_tables,
            contextUsed: p.context_used,
            sourceTables: p.source_tables ?? [],
            numbersVerified: p.numbers_verified ?? 0,
            sqlSummary: p.sql_summary ?? [],
            runId: p.run_id ?? null,
            usage: { calls: p.usage.llm_calls, tokens: p.usage.total_tokens, cost: p.usage.cost },
            latencyMs: p.latency_ms,
          });
          this.running = false;
          this.stream = null;
          this.persist();
        },
        onError: (message) => {
          patch({ status: "failed" });
          const m = this.msgs.find((x) => x.id === aiId) as AiMsg;
          m.steps.push({
            label: message ? "请求失败" : "连接中断",
            state: "error",
            err: message || "与服务器的连接断开了（网络波动或代理超时），点「重跑」再试一次",
          });
          this.running = false;
          this.stream = null;
          this.persist();
        },
      });
    },

    /** 重跑：不走缓存，并沿用这条消息当时的对话上下文（它之前的那几轮），而不是现在最新的 */
    rerun(msgId: string) {
      const i = this.msgs.findIndex((x) => x.id === msgId);
      const m = this.msgs[i] as AiMsg | undefined;
      if (!m || m.role !== "ai") return;
      this.ask(m.q, {
        fresh: true, clarify: !m.noClarify, history: historyOf(this.msgs.slice(0, i)),
        mode: m.mode === "analyze" ? "analyze" : "ask",
      });
    },

    /**
     * 回答 Agent 的确认。口径类（有口径词）：把回答作为补充说明拼回原问题再问一次，
     * 数据缺失类：选项本身就是可回答的新问法，直接问。
     * 追问一律关闭反问，避免来回拉扯。
     */
    answerClarification(msgId: string, choice: string) {
      const text = choice.trim();
      const m = this.msgs.find((x) => x.id === msgId) as AiMsg | undefined;
      if (!text || !m?.clarification || m.clarifyAnswered || m.clarifySkipped || this.running) return;
      m.clarifyAnswered = text;
      const term = m.clarification.term;
      const question = term ? `${m.q}（补充说明：「${term}」指${text}）` : text;
      this.ask(question, { display: text, clarify: false });
    },

    /** 输入框发送：若最近一条回答在等待确认，把输入当作对确认的回答 */
    skipClarification(msgId: string) {
      const m = this.msgs.find((x) => x.id === msgId) as AiMsg | undefined;
      if (m?.status === "clarify") m.clarifySkipped = true;
      this.persist();
    },

    submit(text: string) {
      const pending = this.pendingClarify;
      if (pending) this.answerClarification(pending.id, text);
      else this.ask(text);
    },

    /** 给一条回答打分；差评可附原因。提交成功才记下，失败时提示原因。 */
    async rate(msgId: string, rating: "up" | "down", reason = "") {
      const m = this.msgs.find((x) => x.id === msgId) as AiMsg | undefined;
      if (!m?.runId) return;
      try {
        await sendFeedback(m.runId, rating, reason);
        m.rating = rating;
        m.ratingReason = reason || undefined;
        this.persist();
      } catch (e) {
        window.alert(e instanceof Error ? e.message : "反馈没有提交成功");
      }
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
  if (!last) return "理解问题";
  if (last.label === "守卫执行") {
    if (last.state === "error") return "修正并重试";
    return m.chart ? "生成图表" : "归纳回答";
  }
  if (last.label === "生成图表") return "归纳回答";
  if (last.label === "浏览表目录") return "理解问题";
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
    clarify: { t: "需要确认", i: "?", c: "var(--accink)", bg: "var(--accbg)" },
  }[status];
}
