<script setup lang="ts">
import { useAppStore } from "../stores/app";

withDefaults(defineProps<{ placeholder: string; elevated?: boolean }>(), { elevated: false });
const store = useAppStore();

function send() {
  if (store.running) store.stop();
  else store.submit(store.draft);
}
</script>

<template>
  <div class="composer" :class="{ elevated }">
    <input
      v-model="store.draft"
      :placeholder="placeholder"
      @keydown.enter.prevent="send"
    />
    <div class="bar">
      <div
        class="chart"
        :class="{ on: store.chartOn && !store.analyzeOn, off: store.running || store.analyzeOn }"
        :title="store.analyzeOn ? '深度分析不生成图表' : '结果画成图（沙箱里执行模型写的画图代码）'"
        @click="!store.running && !store.analyzeOn && (store.chartOn = !store.chartOn)"
      >
        <svg class="cic" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.75" stroke-linecap="round">
          <path d="M6 20v-6M12 20V4M18 20v-9" />
        </svg>
        生成图表
      </div>
      <div
        class="chart"
        :class="{ on: store.analyzeOn, off: store.running }"
        title="适合&quot;为什么下降&quot;这类问题：拆成几步查询，再写每句都标明出处的结论"
        @click="!store.running && (store.analyzeOn = !store.analyzeOn)"
      >
        <svg class="cic" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M4 19V5M4 19h16M8 15l3-4 3 2 5-6" />
        </svg>
        深度分析
      </div>
      <span class="hint">{{
        store.running ? "正在运行 · 流式接收中…"
        : store.analyzeOn ? "会拆成几步查询，比普通提问慢一些 · Enter 发送"
        : "重复的问题会命中缓存 · Enter 发送"
      }}</span>
      <button class="send" :class="{ stop: store.running }" @click="send">
        <span v-if="store.running" class="stopsq"></span>
        {{ store.running ? "停止" : "发送" }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.composer {
  background: var(--paper); border: 1px solid var(--line); border-radius: var(--r-lg);
  padding: 15px 20px; box-shadow: var(--sh-sm); transition: border-color 0.15s, box-shadow 0.15s;
}
.composer.elevated { box-shadow: var(--sh-md); }
.composer:focus-within { border-color: var(--acc); }
input { width: 100%; border: none; outline: none; background: none; color: var(--ink); font-size: 15px; padding: 2px 4px; }
.bar { display: flex; align-items: center; gap: 12px; margin-top: 12px; }
.chart {
  display: inline-flex; align-items: center; gap: 7px; cursor: pointer; user-select: none;
  border: 1px solid var(--line); border-radius: 999px; padding: 4px 14px; font-size: 12.5px; color: var(--ink2);
}
.chart:hover { background: var(--accbg); border-color: var(--acc); }
.chart.on { border-color: var(--acc); color: var(--accink); background: var(--accbg); }
.chart.off { opacity: 0.55; cursor: default; }
.chart.off:hover { background: none; border-color: var(--line); }
.cic { flex: none; }
.hint { font-size: 12px; color: var(--ink3); }
.send {
  margin-left: auto; border: none; border-radius: 999px; cursor: pointer;
  background: var(--acc); color: var(--paper); font-size: 14px; font-weight: 600;
  padding: 8px 24px; box-shadow: var(--sh-sm);
  display: inline-flex; align-items: center; gap: 8px;
}
body[data-theme="dark"] .send { color: #201e1d; }
.send:hover { filter: brightness(0.94); }
.send:active { filter: brightness(0.88); }
.send.stop { animation: dq-pulse 1.6s infinite; }
.stopsq { width: 9px; height: 9px; border-radius: 2px; background: currentColor; }
</style>
