<script setup lang="ts">
import { computed, ref } from "vue";
import { prettySql, tokenizeSql } from "../lib/sql";
import { cleanAnswer } from "../lib/text";
import { nextStepOf, pillOf, useAppStore, type AiMsg } from "../stores/app";
import DqLogo from "./DqLogo.vue";
import ResultTable from "./ResultTable.vue";

const props = defineProps<{ msg: AiMsg }>();
const store = useAppStore();

// 运行过程默认收起：运行中只显示"正在…"的状态条，点击才在消息内展开步骤
const open = ref(false);

const pill = computed(() => pillOf(props.msg.status));

const nextStep = computed(() => nextStepOf(props.msg));
const runLabel = computed(() => `正在${nextStep.value}`);
const runStep = computed(() => `第 ${props.msg.steps.length + 1} 步`);

const meta = computed(() => {
  const m = props.msg;
  if (m.status === "cached") return "已跳过（缓存）";
  // 只数执行失败（之后会修正重试）；"已停止""连接中断"、图表失败都不算重试
  const errs = m.steps.filter((s) => s.state === "error" && s.label === "守卫执行").length;
  const dur = m.latencyMs != null ? ` · ${(m.latencyMs / 1000).toFixed(1)}s` : "";
  return `${m.steps.length} 步${dur}${errs ? ` · ${errs} 次失败重试` : ""}`;
});

// 确认的选项在底部面板里（ClarifyDock），消息里只留问题和结果
const clarifyState = computed(() => {
  const m = props.msg;
  if (m.clarifyAnswered) {
    return m.clarification?.term ? `已按「${m.clarifyAnswered}」继续查询` : `已改问：${m.clarifyAnswered}`;
  }
  if (m.clarifySkipped) return "已跳过";
  return store.pendingClarify?.id === m.id ? "" : "未回答"; // 等待中：选项就在下方面板
});

const answerText = computed(() => cleanAnswer(props.msg.answer || ""));

// 回答下方的"出处"：数据来自哪几张表、数字是否核对过，一键打开右栏看 SQL；
// 口径 / 表结构类问题没有查数据，出处写明依据，不冒充查询结果
const finished = computed(() => props.msg.status === "done" || props.msg.status === "cached");
const showSource = computed(() => finished.value && !props.msg.meta && !!props.msg.sql);
const showMetaSource = computed(() => finished.value && !!props.msg.meta);

// 反馈：👍 直接提交；👎 先选原因（可不选）再提交。只有服务端记了这次运行才显示
const canRate = computed(() => !!props.msg.runId && props.msg.status !== "running" && props.msg.status !== "clarify");
const asking = ref(false);
const reason = ref("");
const REASONS = ["数字不对", "理解错了问题", "口径不对", "结果不完整", "太慢了"];

function thumbsUp() {
  if (props.msg.rating) return;
  store.rate(props.msg.id, "up");
}
async function submitDown() {
  await store.rate(props.msg.id, "down", reason.value.trim());
  asking.value = false;
}

function copyAnswer() {
  if (answerText.value) navigator.clipboard?.writeText(answerText.value);
}
</script>

<template>
  <div class="ai">
    <div class="striprow">
      <div v-if="msg.status === 'running'" class="strip running" @click="open = !open">
        <svg class="ring" width="17" height="17" viewBox="0 0 20 20" fill="none">
          <circle cx="10" cy="10" r="7.5" stroke="var(--accbg)" stroke-width="3" />
          <path d="M10 2.5 A7.5 7.5 0 0 1 17.5 10" stroke="var(--accink)" stroke-width="3" stroke-linecap="round" />
        </svg>
        <span class="rtitle">{{ runLabel }}</span>
        <span v-if="runStep" class="rmeta">{{ runStep }}</span>
        <span class="dots">
          <span class="d"></span><span class="d d2"></span><span class="d d3"></span>
        </span>
      </div>
      <div
        v-else
        class="strip"
        :class="{ err: msg.status === 'blocked' || msg.status === 'failed', warn: msg.status === 'stopped', ask: msg.status === 'clarify' }"
        @click="open = !open"
      >
        <span class="dot" :style="{ background: pill.bg, color: pill.c }">{{ pill.i }}</span>
        <span :style="{ color: pill.c }" class="stitle">{{ pill.t }}</span>
        <span class="smeta">{{ meta }}</span>
        <span class="schev" :class="{ open }">›</span>
      </div>
      <span v-if="msg.status === 'cached'" class="cachetag">缓存命中 · 零消耗</span>
    </div>

    <div v-if="open" class="stepsbox">
      <div v-if="msg.status === 'cached'" class="scache">
        命中结果缓存，直接返回历史结果，本次无运行步骤、零消耗。
      </div>
      <div v-for="(s, i) in msg.steps" :key="i" class="step">
        <svg v-if="s.state === 'run'" class="stepring" width="16" height="16" viewBox="0 0 20 20" fill="none">
          <circle cx="10" cy="10" r="7.5" stroke="var(--accbg)" stroke-width="3" />
          <path d="M10 2.5 A7.5 7.5 0 0 1 17.5 10" stroke="var(--accink)" stroke-width="3" stroke-linecap="round" />
        </svg>
        <span
          v-else
          class="sdot"
          :style="{
            background: s.state === 'error' ? 'var(--errbg)' : 'var(--acc2bg)',
            color: s.state === 'error' ? 'var(--err)' : 'var(--acc2ink)',
          }"
        >{{ s.state === "error" ? "✕" : "✓" }}</span>
        <div class="sbody">
          <div class="slabel">{{ s.label }}</div>
          <div v-if="s.thought" class="sthought">{{ s.thought }}</div>
          <div v-if="s.detail" class="sdetail">{{ s.detail }}</div>
          <pre v-if="s.sql" class="ssql mono"><span v-for="(t, j) in tokenizeSql(prettySql(s.sql))" :key="j" :style="{ color: t.c }">{{ t.t }}</span></pre>
          <div v-if="s.err" class="serr mono">{{ s.err }}</div>
        </div>
      </div>
      <div v-if="msg.status === 'running'" class="step">
        <svg class="stepring" width="16" height="16" viewBox="0 0 20 20" fill="none">
          <circle cx="10" cy="10" r="7.5" stroke="var(--accbg)" stroke-width="3" />
          <path d="M10 2.5 A7.5 7.5 0 0 1 17.5 10" stroke="var(--accink)" stroke-width="3" stroke-linecap="round" />
        </svg>
        <div class="sbody"><div class="slabel" style="color: var(--ink3)">{{ nextStep }}…</div></div>
      </div>
    </div>

    <!-- 生成中的占位：logo 均衡器跳动 + 微光骨架 -->
    <div v-if="msg.status === 'running' && !msg.answer" class="thinking">
      <DqLogo :size="38" :animated="true" />
      <div class="skel">
        <div class="shimmer" style="width: 82%"></div>
        <div class="shimmer" style="width: 58%; animation-delay: 0.15s"></div>
      </div>
    </div>

    <div v-if="msg.status === 'blocked'" class="blocked">
      <div class="btitle">回答已被幻觉校验拦截</div>
      <div class="btext">{{ msg.blockedText }}</div>
    </div>

    <div v-if="msg.status === 'clarify' && msg.clarification" class="clarify">
      <div class="answer">{{ msg.clarification.question }}</div>
      <div v-if="clarifyState" class="cstate">{{ clarifyState }}</div>
    </div>

    <div v-if="answerText && msg.status !== 'blocked'" class="answer">{{ answerText }}</div>

    <div v-if="showSource" class="source">
      <span class="srctag">出处</span>
      <span>查询结果 {{ msg.rowCount }} 行</span>
      <template v-if="msg.sourceTables?.length">
        <span class="dotsep">·</span>
        <span>来自 <span class="mono">{{ msg.sourceTables.join("、") }}</span></span>
      </template>
      <template v-if="msg.numbersVerified">
        <span class="dotsep">·</span>
        <span class="verified" title="回答里的每个数字都能在查询结果、问题或 SQL 中找到">✓ {{ msg.numbersVerified }} 个数字已核对</span>
      </template>
      <span class="srclink" @click="store.panelId = msg.id">查看 SQL ›</span>
    </div>
    <div v-else-if="showMetaSource" class="source">
      <span class="srctag">依据</span>
      <span>表结构与业务口径</span>
      <span class="dotsep">·</span>
      <span>未查询数据</span>
    </div>
    <div v-if="showSource && msg.sqlSummary?.length" class="source scope" title="从 SQL 自动生成，不经过模型">
      <span class="srctag scopetag">口径</span>
      <template v-for="(part, i) in msg.sqlSummary" :key="i">
        <span v-if="i" class="dotsep">·</span>
        <span>{{ part }}</span>
      </template>
    </div>

    <ResultTable v-if="msg.columns && msg.columns.length" :columns="msg.columns" :rows="msg.rows || []" :row-count="msg.rowCount || 0" />

    <img v-if="msg.chartUrl" class="chart" :src="msg.chartUrl" alt="chart" />
    <div v-else-if="msg.chartError" class="charterr">图表生成失败：{{ msg.chartError }}</div>

    <div v-if="msg.status !== 'running' && msg.status !== 'clarify'" class="foot">
      <span v-if="msg.answer" @click="copyAnswer">复制回答</span>
      <span title="强制重新执行，不走缓存" @click="store.rerun(msg.id)">重跑</span>
      <template v-if="canRate">
        <span v-if="msg.rating" class="rated">{{ msg.rating === "up" ? "已标记有帮助" : "已记录问题，谢谢反馈" }}</span>
        <template v-else>
          <span class="thumb" title="有帮助" @click="thumbsUp">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v11H4a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1h3Zm0 0 4-7a2.5 2.5 0 0 1 3 2.6L13.4 9H19a2 2 0 0 1 2 2.3l-1.3 7.6A2.5 2.5 0 0 1 17.2 21H7"/></svg>
          </span>
          <span class="thumb" :class="{ on: asking }" title="有问题" @click="asking = !asking">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17 14V3h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-3Zm0 0-4 7a2.5 2.5 0 0 1-3-2.6l.6-3.4H5a2 2 0 0 1-2-2.3l1.3-7.6A2.5 2.5 0 0 1 6.8 3H17"/></svg>
          </span>
        </template>
      </template>
    </div>
    <div v-if="asking && !msg.rating" class="fbbox">
      <div class="fbtitle">哪里不对？（可以不选，直接提交）</div>
      <div class="fbchips">
        <span v-for="r in REASONS" :key="r" class="fbchip" :class="{ on: reason === r }" @click="reason = reason === r ? '' : r">{{ r }}</span>
      </div>
      <div class="fbrow">
        <input v-model="reason" class="fbinput" maxlength="200" placeholder="或者写一句具体的问题" @keydown.enter="submitDown" />
        <button class="fbsend" @click="submitDown">提交</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ai { display: flex; flex-direction: column; gap: 13px; }
.striprow { display: flex; align-items: center; gap: 8px; }
.strip {
  display: inline-flex; align-items: center; gap: 9px; cursor: pointer;
  background: var(--acc2bg); border-radius: 999px; padding: 5px 15px 5px 9px; font-size: 12.5px;
}
.strip:hover { filter: brightness(0.97); }
.strip.running { background: var(--accbg); }
.strip.err { background: var(--errbg); }
.strip.warn { background: var(--warnbg); }
.strip.ask { background: var(--accbg); }
.ring { animation: spin 0.9s linear infinite; flex: none; }
.rtitle { color: var(--accdeep); font-weight: 600; }
.rmeta { color: var(--accink); opacity: 0.75; }
.dots { display: inline-flex; gap: 3px; }
.d { width: 4px; height: 4px; border-radius: 50%; background: var(--accink); animation: dq-dot 1.2s infinite; }
.d2 { animation-delay: 0.2s; }
.d3 { animation-delay: 0.4s; }
.dot { width: 17px; height: 17px; border-radius: 50%; font-size: 9px; font-weight: 700; display: flex; align-items: center; justify-content: center; flex: none; }
.stitle { font-weight: 600; }
.smeta { color: var(--ink3); }
.schev { color: var(--ink3); font-size: 12px; line-height: 1; transition: transform 0.15s; display: inline-block; }
.schev.open { transform: rotate(90deg); }
.cachetag { font-size: 12px; color: var(--accink); background: var(--accbg); border-radius: 999px; padding: 3px 12px; }
.stepsbox { margin: -2px 0 0 12px; padding: 4px 0 4px 16px; border-left: 2px solid var(--line); display: flex; flex-direction: column; gap: 11px; }
.scache { background: var(--accbg); border-radius: var(--r-md); padding: 9px 14px; font-size: 13px; color: var(--accink); }
.step { display: flex; gap: 10px; animation: fadeUp 0.25s ease; }
.stepring { flex: none; margin-top: 3px; animation: spin 0.9s linear infinite; }
.sdot { width: 16px; height: 16px; flex: none; margin-top: 2.5px; border-radius: 50%; font-size: 9px; font-weight: 700; display: flex; align-items: center; justify-content: center; }
.sbody { min-width: 0; }
.slabel { font-size: 13px; color: var(--ink); }
.sthought { font-size: 12.5px; color: var(--ink3); margin-top: 1px; }
.sdetail { font-size: 12px; color: var(--accink); margin-top: 2px; }
.serr { font-size: 12.5px; color: var(--err); margin-top: 2px; }
.thinking { display: flex; align-items: flex-start; gap: 14px; }
.skel { flex: 1; display: flex; flex-direction: column; gap: 10px; padding-top: 7px; }
.blocked { background: var(--errbg); border-radius: var(--r-md); padding: 12px 16px; }
.btitle { font-size: 13.5px; font-weight: 600; color: var(--err); margin-bottom: 2px; }
.btext { font-size: 13.5px; color: var(--ink2); white-space: pre-wrap; }
.clarify { display: flex; flex-direction: column; gap: 6px; }
.cstate { font-size: 12.5px; color: var(--accink); }
.ssql {
  margin: 6px 0 0; padding: 8px 12px; border-radius: 10px; background: var(--soft);
  font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
  max-height: 150px; overflow: auto;
}
.source {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: -4px;
  font-size: 12.5px; color: var(--ink3);
}
.source .mono { font-size: 12px; color: var(--ink2); }
.srctag {
  font-size: 11px; font-weight: 600; color: var(--acc2ink); background: var(--acc2bg);
  border-radius: 999px; padding: 1px 9px; margin-right: 2px;
}
.dotsep { opacity: 0.6; }
.scope { margin-top: -8px; color: var(--ink2); }
.scopetag { color: var(--accink); background: var(--accbg); }
.verified { color: var(--acc2ink); }
.srclink { margin-left: auto; cursor: pointer; color: var(--accink); }
.srclink:hover { text-decoration: underline; }
.answer { font-size: 15.5px; line-height: 1.85; color: var(--ink); white-space: pre-wrap; }
.chart { max-width: 100%; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--paper); }
.charterr { font-size: 12.5px; color: var(--warn); }
.foot { display: flex; gap: 16px; font-size: 12.5px; color: var(--ink3); }
.foot span { cursor: pointer; }
.foot span:hover { color: var(--accink); }
.foot .thumb { display: inline-flex; align-items: center; }
.foot .thumb.on { color: var(--err); }
.foot .rated { cursor: default; color: var(--acc2ink); }
.foot .rated:hover { color: var(--acc2ink); }
.fbbox {
  margin-top: -6px; padding: 12px 14px; border-radius: var(--r-md); background: var(--soft);
  display: flex; flex-direction: column; gap: 9px; animation: fadeUp 0.2s ease;
}
.fbtitle { font-size: 12.5px; color: var(--ink2); }
.fbchips { display: flex; flex-wrap: wrap; gap: 7px; }
.fbchip {
  font-size: 12px; padding: 3px 11px; border-radius: 999px; cursor: pointer;
  background: var(--paper); color: var(--ink2); border: 1px solid var(--line);
}
.fbchip.on { background: var(--errbg); color: var(--err); border-color: transparent; }
.fbrow { display: flex; gap: 8px; }
.fbinput {
  flex: 1; font: inherit; font-size: 13px; padding: 6px 11px; border-radius: 10px;
  border: 1px solid var(--line); background: var(--paper); color: var(--ink); outline: none;
}
.fbinput:focus { border-color: var(--accink); }
.fbsend {
  font: inherit; font-size: 12.5px; padding: 0 14px; border-radius: 10px; border: none; cursor: pointer;
  background: var(--acc); color: var(--paper); font-weight: 600;
}
.fbsend:hover { filter: brightness(0.94); }
</style>
