<script setup lang="ts">
import { computed, nextTick, reactive } from "vue";
import { prettySql, tokenizeSql } from "../lib/sql";
import { cleanAnswer } from "../lib/text";
import type { AiMsg, PlanStep } from "../stores/app";
import ResultTable from "./ResultTable.vue";

const props = defineProps<{ msg: AiMsg }>();

const running = computed(() => props.msg.status === "running");
const plan = computed(() => props.msg.plan || []);
const doneCount = computed(() => plan.value.filter((s) => s.state !== "pending").length);
const open = reactive<Record<number, boolean>>({});

const headLabel = computed(() => {
  const m = props.msg;
  if (running.value) {
    if (!plan.value.length) return "正在制定分析计划";
    if (doneCount.value < plan.value.length) return `正在查询 · 已完成 ${doneCount.value}/${plan.value.length} 步`;
    return "正在写结论";
  }
  if (m.status === "stopped") return "分析已停止";
  if (m.status === "failed") return "分析没有完成";
  const dur = m.latencyMs != null ? ` · ${(m.latencyMs / 1000).toFixed(1)}s` : "";
  return `深度分析 · ${plan.value.length} 步查询${dur}`;
});

const STATE_TEXT: Record<PlanStep["state"], string> = {
  pending: "查询中", ok: "", empty: "没有数据", error: "失败", skipped: "未执行",
};

function stepMeta(s: PlanStep): string {
  if (s.state === "ok") return `${s.rowCount ?? 0} 行`;
  if (s.state === "pending" && !running.value) return "未执行";
  return STATE_TEXT[s.state];
}

// 结论里的 [n]：渲染成可点的小标记，点开对应那一步
const reportText = computed(() => cleanAnswer(props.msg.answer || props.msg.blockedText || ""));
const pieces = computed(() => {
  const out: { text?: string; cite?: number }[] = [];
  const re = /\[(\d{1,2})\]/g;
  let last = 0;
  let m: RegExpExecArray | null;
  const text = reportText.value;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push({ text: text.slice(last, m.index) });
    out.push({ cite: Number(m[1]) });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last) });
  return out;
});

async function openStep(no: number) {
  open[no] = true;
  await nextTick();
  document.getElementById(`step-${props.msg.id}-${no}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
}

const finished = computed(() => props.msg.status === "done" || props.msg.status === "cached" || props.msg.status === "blocked");
</script>

<template>
  <div class="an">
    <div class="head" :class="{ run: running, bad: msg.status === 'failed' || msg.status === 'stopped' }">
      <svg v-if="running" class="ring" width="16" height="16" viewBox="0 0 20 20" fill="none">
        <circle cx="10" cy="10" r="7.5" stroke="var(--accbg)" stroke-width="3" />
        <path d="M10 2.5 A7.5 7.5 0 0 1 17.5 10" stroke="var(--accink)" stroke-width="3" stroke-linecap="round" />
      </svg>
      <svg v-else class="hicon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 19V5M4 19h16M8 15l3-4 3 2 5-6" />
      </svg>
      <span class="htext">{{ headLabel }}</span>
      <span v-if="msg.status === 'cached'" class="hcache">缓存命中 · 零消耗</span>
    </div>

    <div v-if="msg.planThought" class="thought">{{ msg.planThought }}</div>

    <div v-if="!plan.length && running" class="skel">
      <div class="shimmer" style="width: 70%"></div>
      <div class="shimmer" style="width: 52%; animation-delay: 0.15s"></div>
    </div>

    <ol v-if="plan.length" class="plan">
      <li v-for="s in plan" :id="`step-${msg.id}-${s.no}`" :key="s.no" class="pstep" :class="s.state">
        <div class="prow" @click="s.state !== 'pending' && (open[s.no] = !open[s.no])">
          <span class="pno">
            <svg v-if="s.state === 'pending' && running" class="pspin" width="22" height="22" viewBox="0 0 20 20" fill="none">
              <circle cx="10" cy="10" r="8" stroke="var(--accbg)" stroke-width="2.5" />
              <path d="M10 2 A8 8 0 0 1 18 10" stroke="var(--accink)" stroke-width="2.5" stroke-linecap="round" />
            </svg>
            <template v-else>{{ s.no }}</template>
          </span>
          <div class="ptext">
            <div class="pq">{{ s.question }}</div>
            <div v-if="s.purpose" class="ppurpose">{{ s.purpose }}</div>
          </div>
          <span class="pmeta">{{ stepMeta(s) }}</span>
          <span v-if="s.state !== 'pending'" class="chev" :class="{ open: open[s.no] }">›</span>
        </div>
        <div v-if="open[s.no]" class="pdetail">
          <div v-if="s.summary?.length" class="pscope"><span class="tag">口径</span>{{ s.summary.join(" · ") }}</div>
          <pre v-if="s.sql" class="psql mono"><span v-for="(t, j) in tokenizeSql(prettySql(s.sql))" :key="j" :style="{ color: t.c }">{{ t.t }}</span></pre>
          <ResultTable v-if="s.columns?.length" :columns="s.columns" :rows="s.rows || []" :row-count="s.rowCount || 0" :label="`第 ${s.no} 步结果`" />
          <div v-if="s.error" class="perr">{{ s.error }}</div>
        </div>
      </li>
    </ol>

    <div v-if="msg.reviewNote" class="review"><span class="tag">下钻判断</span>{{ msg.reviewNote }}</div>

    <div v-if="running && plan.length && doneCount === plan.length && !msg.answer" class="skel">
      <div class="shimmer" style="width: 80%"></div>
      <div class="shimmer" style="width: 60%; animation-delay: 0.15s"></div>
    </div>

    <div v-if="msg.status === 'blocked'" class="warn">结论里有数字对不上所标注步骤的结果，已改为直接列出各步查询结果。</div>
    <div v-if="reportText" class="report">
      <template v-for="(p, i) in pieces" :key="i">
        <span v-if="p.text">{{ p.text }}</span>
        <span v-else class="cite" :title="`出自第 ${p.cite} 步，点开查看`" @click="openStep(p.cite!)">{{ p.cite }}</span>
      </template>
    </div>

    <div v-if="finished && msg.status !== 'blocked' && plan.length" class="source">
      <span class="srctag">依据</span>
      <span>{{ plan.filter((s) => s.state === "ok").length }} 步查询结果</span>
      <template v-if="msg.sourceTables?.length">
        <span class="dotsep">·</span>
        <span>来自 <span class="mono">{{ msg.sourceTables.join("、") }}</span></span>
      </template>
      <template v-if="msg.numbersVerified">
        <span class="dotsep">·</span>
        <span class="verified" title="每个数字都核对过：确实出自句末所标注那一步的查询结果">✓ {{ msg.numbersVerified }} 个数字已逐句核对出处</span>
      </template>
    </div>
  </div>
</template>

<style scoped>
.an { display: flex; flex-direction: column; gap: 12px; }
.head {
  align-self: flex-start; display: inline-flex; align-items: center; gap: 8px;
  background: var(--acc2bg); color: var(--acc2deep); border-radius: 999px; padding: 5px 15px 5px 10px;
  font-size: 12.5px; font-weight: 600;
}
.head.run { background: var(--accbg); color: var(--accdeep); }
.head.bad { background: var(--errbg); color: var(--err); }
.ring { animation: spin 0.9s linear infinite; flex: none; }
.hicon { flex: none; }
.hcache { font-weight: 400; color: var(--accink); margin-left: 4px; }
.thought { font-size: 13px; color: var(--ink3); margin-top: -2px; }
.skel { display: flex; flex-direction: column; gap: 10px; padding: 4px 0; }
.plan { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.pstep { border: 1px solid var(--line); border-radius: var(--r-md); background: var(--paper); overflow: hidden; animation: fadeUp 0.25s ease; }
.prow { display: flex; align-items: center; gap: 12px; padding: 10px 14px; cursor: pointer; }
.pstep.pending .prow { cursor: default; }
.pno {
  width: 22px; height: 22px; flex: none; border-radius: 50%; display: flex; align-items: center; justify-content: center;
  font-size: 11.5px; font-weight: 700; background: var(--acc2bg); color: var(--acc2ink);
}
.pstep.pending .pno { background: none; }
.pstep.error .pno, .pstep.skipped .pno { background: var(--errbg); color: var(--err); }
.pstep.empty .pno { background: var(--soft); color: var(--ink3); }
.pspin { animation: spin 0.9s linear infinite; }
.ptext { flex: 1; min-width: 0; }
.pq { font-size: 13.5px; color: var(--ink); }
.ppurpose { font-size: 12px; color: var(--ink3); margin-top: 1px; }
.pmeta { font-size: 12px; color: var(--ink3); white-space: nowrap; }
.pstep.error .pmeta { color: var(--err); }
.chev { color: var(--ink3); font-size: 12px; transition: transform 0.15s; }
.chev.open { transform: rotate(90deg); }
.pdetail { padding: 0 14px 12px 48px; display: flex; flex-direction: column; gap: 8px; }
.pscope, .review { font-size: 12.5px; color: var(--ink2); }
.tag {
  font-size: 11px; font-weight: 600; color: var(--accink); background: var(--accbg);
  border-radius: 999px; padding: 1px 9px; margin-right: 8px;
}
.psql {
  margin: 0; padding: 8px 12px; border-radius: 10px; background: var(--soft);
  font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; max-height: 160px; overflow: auto;
}
.perr { font-size: 12.5px; color: var(--err); }
.warn { font-size: 13px; color: var(--warn); background: var(--warnbg); border-radius: var(--r-md); padding: 8px 14px; }
.report { font-size: 15.5px; line-height: 1.85; color: var(--ink); white-space: pre-wrap; }
.cite {
  display: inline-flex; align-items: center; justify-content: center; min-width: 17px; height: 17px;
  margin: 0 2px; padding: 0 4px; border-radius: 6px; vertical-align: 2px;
  font-size: 10.5px; font-weight: 700; color: var(--accink); background: var(--accbg); cursor: pointer;
}
.cite:hover { filter: brightness(0.95); }
.source { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: -4px; font-size: 12.5px; color: var(--ink3); }
.source .mono { font-size: 12px; color: var(--ink2); }
.srctag {
  font-size: 11px; font-weight: 600; color: var(--acc2ink); background: var(--acc2bg);
  border-radius: 999px; padding: 1px 9px; margin-right: 2px;
}
.dotsep { opacity: 0.6; }
.verified { color: var(--acc2ink); }
</style>
