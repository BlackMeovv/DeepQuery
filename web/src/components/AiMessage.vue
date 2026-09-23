<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { nextStepOf, pillOf, useAppStore, type AiMsg } from "../stores/app";
import DqLogo from "./DqLogo.vue";
import ResultTable from "./ResultTable.vue";

const props = defineProps<{ msg: AiMsg }>();
const store = useAppStore();

// 运行过程在消息内原地下拉展开；运行中自动展开、结束后自动收起
const open = ref(props.msg.status === "running");
watch(
  () => props.msg.status,
  (now, prev) => {
    if (now === "running") open.value = true;
    else if (prev === "running") open.value = false;
  },
);

const pill = computed(() => pillOf(props.msg.status));

const nextStep = computed(() => nextStepOf(props.msg));
const runLabel = computed(() => `正在${nextStep.value}`);
const runStep = computed(() => `第 ${props.msg.steps.length + 1} 步`);

const meta = computed(() => {
  const m = props.msg;
  if (m.status === "cached") return "已跳过（缓存）";
  const errs = m.steps.filter((s) => s.state === "error").length;
  const dur = m.latencyMs != null ? ` · ${(m.latencyMs / 1000).toFixed(1)}s` : "";
  return `${m.steps.length} 步${dur}${errs ? ` · ${errs} 次失败重试` : ""}`;
});

function copyAnswer() {
  if (props.msg.answer) navigator.clipboard?.writeText(props.msg.answer);
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
        :class="{ err: msg.status === 'blocked' || msg.status === 'failed', warn: msg.status === 'stopped' }"
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

    <div v-if="msg.answer && msg.status !== 'blocked'" class="answer">{{ msg.answer }}</div>

    <ResultTable v-if="msg.columns && msg.columns.length" :msg="msg" />

    <img v-if="msg.chartUrl" class="chart" :src="msg.chartUrl" alt="chart" />
    <div v-else-if="msg.chartError" class="charterr">图表生成失败：{{ msg.chartError }}</div>

    <div v-if="msg.status !== 'running'" class="foot">
      <span v-if="msg.answer" @click="copyAnswer">复制回答</span>
      <span title="强制重新执行，不走缓存" @click="store.ask(msg.q, true)">重跑</span>
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
.serr { font-size: 12.5px; color: var(--err); margin-top: 2px; }
.thinking { display: flex; align-items: flex-start; gap: 14px; }
.skel { flex: 1; display: flex; flex-direction: column; gap: 10px; padding-top: 7px; }
.blocked { background: var(--errbg); border-radius: var(--r-md); padding: 12px 16px; }
.btitle { font-size: 13.5px; font-weight: 600; color: var(--err); margin-bottom: 2px; }
.btext { font-size: 13.5px; color: var(--ink2); white-space: pre-wrap; }
.answer { font-size: 15.5px; line-height: 1.85; color: var(--ink); white-space: pre-wrap; }
.chart { max-width: 100%; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--paper); }
.charterr { font-size: 12.5px; color: var(--warn); }
.foot { display: flex; gap: 16px; font-size: 12.5px; color: var(--ink3); }
.foot span { cursor: pointer; }
.foot span:hover { color: var(--accink); }
</style>
