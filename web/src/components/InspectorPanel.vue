<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { prettySql, tokenizeSql } from "../lib/sql";
import { nextStepOf, pillOf, useAppStore, type AiMsg } from "../stores/app";

const props = defineProps<{ msg: AiMsg }>();
const store = useAppStore();

// 打开面板时分区默认全部收起；运行中"过程"自动展开、结束后自动收起
const secProg = ref(props.msg.status === "running");
const secOut = ref(false);
const secCtx = ref(false);

watch(
  () => props.msg.id,
  () => {
    secProg.value = props.msg.status === "running";
    secOut.value = false;
    secCtx.value = false;
  },
);
watch(
  () => props.msg.status,
  (now, prev) => {
    if (now === "running") secProg.value = true;
    else if (prev === "running") secProg.value = false;
  },
);

const ctxUsed = computed(
  () => props.msg.contextUsed ?? { glossary: [], examples: [], memories: [] },
);

const pill = computed(() => pillOf(props.msg.status));
const sqlToks = computed(() => (props.msg.sql ? tokenizeSql(prettySql(props.msg.sql)) : []));
const usageLine = computed(() => {
  const u = props.msg.usage;
  if (!u) return "";
  const dur = props.msg.latencyMs != null ? ` · ${(props.msg.latencyMs / 1000).toFixed(1)}s` : "";
  return `LLM 调用 ${u.calls} 次 · ${u.tokens} tokens · cost ${u.cost.toFixed(6)}${dur}`;
});

function copySql() {
  if (props.msg.sql) navigator.clipboard?.writeText(props.msg.sql);
}
</script>

<template>
  <div class="wrap">
    <div class="panel">
      <div class="phead">
        <span class="pq">「{{ msg.q }}」</span>
        <span class="pclose" @click="store.panelId = null">✕</span>
      </div>
      <div class="pbody">
        <div class="sec" @click="secProg = !secProg">
          <span class="stitle">过程</span>
          <span class="spill" :style="{ color: pill.c, background: pill.bg }">{{ msg.status === "done" ? "已完成" : msg.status === "running" ? "运行中" : pill.t }}</span>
          <span class="schev" :class="{ open: secProg }">›</span>
        </div>
        <div v-if="secProg" class="steps">
          <div v-if="msg.status === 'cached'" class="cachebox">
            命中结果缓存，直接返回历史结果，本次无运行步骤、零消耗。
          </div>
          <div v-for="(s, i) in msg.steps" :key="i" class="step">
            <span v-if="s.state === 'run'" class="spinner"></span>
            <span
              v-else
              class="dot"
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
            <span class="spinner"></span>
            <div class="sbody"><div class="slabel" style="color: var(--ink3)">{{ nextStepOf(msg) }}…</div></div>
          </div>
        </div>

        <div class="hr"></div>
        <div class="sec" @click="secOut = !secOut">
          <span class="stitle">产出</span>
          <span v-if="msg.sql" class="ssub mono">SQL</span>
          <span class="schev" :class="{ open: secOut }">›</span>
        </div>
        <div v-if="secOut" class="out">
          <div v-if="msg.sql" class="sqlcard">
            <div class="sqlhead">
              <span class="mono sqllabel">生成的 SQL</span>
              <button class="sqlcopy" @click="copySql">复制</button>
            </div>
            <pre class="mono"><span v-for="(tk, i) in sqlToks" :key="i" :style="{ color: tk.c }">{{ tk.t }}</span></pre>
          </div>
          <div v-if="usageLine" class="usage">{{ usageLine }}</div>
        </div>

        <div class="hr"></div>
        <div class="sec" @click="secCtx = !secCtx">
          <span class="stitle">上下文</span>
          <span class="ssub">{{ (msg.selectedTables?.length || 0) + ctxUsed.glossary.length + ctxUsed.examples.length + ctxUsed.memories.length }} 项注入</span>
          <span class="schev" :class="{ open: secCtx }">›</span>
        </div>
        <div v-if="secCtx" class="ctx">
          <div>
            <div class="ctxlabel">连接<span class="ctxsub">由后端配置，自动接入</span></div>
            <div class="pillrow">
              <span class="dbpill"><span class="okdot"></span>{{ store.env?.db || "-" }}</span>
              <span class="dbpill">{{ store.env?.model || "-" }}</span>
              <span v-if="store.env" class="dbpill">cache · {{ store.env.cache }}</span>
            </div>
          </div>
          <div v-if="msg.selectedTables?.length">
            <div class="ctxlabel">Schema RAG 命中的表</div>
            <div class="pillrow">
              <span v-for="t in msg.selectedTables" :key="t" class="dbpill mono">{{ t }}</span>
            </div>
          </div>
          <div v-if="ctxUsed.glossary.length">
            <div class="ctxlabel">命中的业务字典</div>
            <div class="pillrow">
              <span v-for="g in ctxUsed.glossary" :key="g" class="dbpill">{{ g }}</span>
            </div>
          </div>
          <div v-if="ctxUsed.examples.length">
            <div class="ctxlabel">few-shot 参考例句</div>
            <div class="memcol">
              <div v-for="ex in ctxUsed.examples" :key="ex" class="memchip">{{ ex }}</div>
            </div>
          </div>
          <div>
            <div class="ctxlabel">注入的记忆 · {{ ctxUsed.memories.length }}</div>
            <div class="memcol">
              <div v-for="pm in ctxUsed.memories" :key="pm" class="memchip">{{ pm }}</div>
              <div v-if="!ctxUsed.memories.length" class="ctxsub">（本次问题未命中任何记忆）</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.wrap { width: 330px; flex: none; display: flex; min-height: 0; }
.panel { flex: 1; background: var(--card); border-radius: var(--r-lg); box-shadow: var(--sh-sm); display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
.phead { flex: none; display: flex; align-items: center; gap: 8px; padding: 17px 22px 8px; }
.pq { font-size: 12.5px; color: var(--ink3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
.pclose { width: 26px; height: 26px; flex: none; display: flex; align-items: center; justify-content: center; border-radius: 50%; cursor: pointer; color: var(--ink3); font-size: 13px; line-height: 1; }
.pclose:hover { background: var(--soft); color: var(--ink); }
.pbody { flex: 1; overflow-y: auto; padding: 2px 22px 16px; }
.sec { display: flex; align-items: center; gap: 8px; padding: 13px 2px; cursor: pointer; user-select: none; }
.sec:hover .schev { color: var(--ink); }
.stitle { font-size: 13.5px; font-weight: 600; }
.ssub { font-size: 11.5px; color: var(--ink3); }
.schev { margin-left: auto; font-size: 13px; color: var(--ink3); transition: transform 0.15s; display: inline-block; }
.schev.open { transform: rotate(90deg); }
.spill { font-size: 11.5px; font-weight: 600; border-radius: 999px; padding: 2px 11px; }
.steps { display: flex; flex-direction: column; gap: 12px; padding-bottom: 16px; }
.cachebox { background: var(--accbg); border-radius: var(--r-md); padding: 10px 14px; font-size: 13px; color: var(--accink); }
.step { display: flex; gap: 9px; animation: fadeUp 0.25s ease; }
.step .spinner { margin-top: 4px; }
.dot { width: 15px; height: 15px; flex: none; margin-top: 3px; border-radius: 50%; font-size: 8.5px; font-weight: 700; display: flex; align-items: center; justify-content: center; }
.sbody { min-width: 0; }
.slabel { font-size: 13.5px; color: var(--ink); }
.sthought { font-size: 12.5px; color: var(--ink3); margin-top: 1px; }
.serr { font-size: 12.5px; color: var(--err); margin-top: 2px; }
.hr { height: 1px; background: var(--line); }
.out { display: flex; flex-direction: column; gap: 10px; padding-bottom: 16px; }
.sqlcard { border-radius: var(--r-md); overflow: hidden; background: var(--code); }
.sqlhead { display: flex; align-items: center; padding: 7px 14px; border-bottom: 1px solid var(--line); }
.sqllabel { font-size: 11px; color: var(--ink3); }
.sqlcopy { margin-left: auto; border: none; background: none; color: var(--accink); font-size: 12px; font-weight: 600; cursor: pointer; padding: 0; }
pre { margin: 0; padding: 12px 14px; font-size: 12px; line-height: 1.7; overflow-x: auto; white-space: pre; }
.usage { font-size: 12px; color: var(--ink3); }
.ctx { display: flex; flex-direction: column; gap: 12px; padding-bottom: 16px; }
.ctxlabel { font-size: 12px; color: var(--ink3); margin-bottom: 6px; }
.ctxsub { margin-left: 6px; font-size: 11px; color: var(--ink3); }
.pillrow { display: flex; flex-wrap: wrap; gap: 6px; }
.dbpill { display: inline-flex; align-items: center; gap: 7px; border-radius: 999px; padding: 4px 13px; font-size: 12.5px; background: var(--soft); color: var(--ink2); }
.okdot { width: 6px; height: 6px; border-radius: 50%; background: var(--acc2); }
.memcol { display: flex; flex-direction: column; gap: 5px; }
.memchip { font-size: 12.5px; color: var(--ink2); border-radius: var(--r-sm); padding: 5px 11px; background: var(--soft); }
</style>
