<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";
import { useAppStore } from "../stores/app";
import AiMessage from "./AiMessage.vue";
import ClarifyDock from "./ClarifyDock.vue";
import Composer from "./Composer.vue";

const store = useAppStore();
const scroller = ref<HTMLElement | null>(null);

// 示例问题由后端按数据集提供；带标签的几条故意问得模糊 / 超出数据范围，用来体验 Agent 先向你确认
const samples = computed(() => store.env?.samples ?? []);

const greetHead = computed(() => {
  const h = new Date().getHours();
  return h < 6 ? "凌晨好，" : h < 12 ? "早上好，" : h < 18 ? "下午好，" : "晚上好，";
});

const toBottom = () => nextTick(() => scroller.value?.scrollTo({ top: scroller.value.scrollHeight }));
watch(
  () => store.msgs.length + store.msgs.reduce((n, m) => n + (m.role === "ai" ? m.steps.length : 0), 0),
  toBottom,
);
// 确认面板比输入框高，出现时把最后一条消息顶上来
watch(() => store.pendingClarify?.id, toBottom);
</script>

<template>
  <div class="main">
    <div class="head" :class="{ noline: store.msgs.length === 0 }">
      <div class="title">{{ store.msgs.length === 0 ? "新的提问" : store.title }}</div>
      <div class="spacer"></div>
      <div v-if="store.env" class="dbpill">
        <span class="okdot"></span>{{ store.env.db }}
      </div>
      <span v-if="store.env?.mock" class="mock">MOCK</span>
      <button
        v-if="store.lastAiId"
        class="paneltoggle"
        :class="{ on: !!store.panelId }"
        :title="store.panelId ? '收起运行详情' : '打开运行详情'"
        @click="store.panelId = store.panelId ? null : store.lastAiId"
      >
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25">
          <rect x="3" y="3" width="18" height="18" rx="5" />
          <path d="M15 3v18" />
        </svg>
      </button>
    </div>

    <template v-if="store.msgs.length === 0">
      <div class="empty">
        <div class="blob b1"></div>
        <div class="blob b2"></div>
        <div class="blob b3"></div>
        <div class="ewrap">
          <div class="greet serif">{{ greetHead }}<br />想看哪块数据？</div>
          <div class="gsub">
            已连接 <span class="mono">{{ store.env?.db || "…" }}</span> · 每个回答都可追溯到 SQL
          </div>
          <div v-if="store.env?.dataset_note" class="dnote">
            {{ store.env.dataset_note }}
            <span v-if="store.env.dataset_source" class="dsrc">数据来源：{{ store.env.dataset_source }}</span>
          </div>
          <Composer placeholder="问一个关于数据的问题…" :elevated="true" />
          <div v-if="samples.length" class="samples">
            <div class="slabel">可以先试试</div>
            <div v-for="(s, i) in samples" :key="s.q" class="sample" @click="store.ask(s.q)">
              <span class="snum serif">{{ i + 1 }}</span>
              <span class="stext">{{ s.q }}</span>
              <span v-if="s.tag" class="stag">{{ s.tag }}</span>
              <span class="sarrow">→</span>
            </div>
          </div>
        </div>
      </div>
    </template>

    <template v-else>
      <div ref="scroller" class="msgs">
        <div class="col">
          <template v-for="m in store.msgs" :key="m.id">
            <div v-if="m.role === 'user'" class="urow">
              <div class="ububble">{{ m.text }}</div>
            </div>
            <AiMessage v-else :msg="m" />
          </template>
        </div>
      </div>
      <div class="cbottom">
        <div class="cwrap">
          <ClarifyDock v-if="store.pendingClarify" :key="store.pendingClarify.id" :msg="store.pendingClarify" />
          <Composer v-else placeholder="继续追问，或换一个问题…" />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.main {
  flex: 1; min-width: 0; display: flex; flex-direction: column; position: relative;
  background: var(--card); border-radius: var(--r-lg); box-shadow: var(--sh-sm); overflow: hidden;
}
.head { height: 58px; flex: none; display: flex; align-items: center; gap: 12px; padding: 0 26px; border-bottom: 1px solid var(--line); position: relative; z-index: 1; }
.head.noline { border-bottom-color: transparent; }
.title { font-size: 14px; color: var(--ink2); }
.spacer { flex: 1; }
.dbpill { display: inline-flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--acc2deep); background: var(--acc2bg); border-radius: 999px; padding: 4px 15px; }
.okdot { width: 7px; height: 7px; border-radius: 50%; background: var(--acc2); }
.mock { font-size: 11px; font-weight: 600; color: var(--warn); background: var(--warnbg); border-radius: 999px; padding: 2px 10px; }
.paneltoggle { display: flex; align-items: center; justify-content: center; width: 34px; height: 34px; border: none; border-radius: 50%; background: none; color: var(--ink3); cursor: pointer; padding: 0; }
.paneltoggle:hover { background: var(--soft); color: var(--ink); }
.paneltoggle.on { color: var(--accink); background: var(--accbg); }

/* 空态：装饰圆 + 左对齐的大标题 */
.empty { flex: 1; display: flex; flex-direction: column; justify-content: center; padding: 0 24px 52px; position: relative; overflow: hidden; }
.blob { position: absolute; border-radius: 50%; pointer-events: none; }
.b1 { right: -120px; top: -140px; width: 420px; height: 420px; background: var(--accbg); opacity: 0.65; }
.b2 { right: 150px; top: 180px; width: 130px; height: 130px; background: var(--acc2bg); opacity: 0.8; }
.b3 { right: 80px; bottom: -90px; width: 260px; height: 260px; background: var(--surface); opacity: 0.9; }
.ewrap { width: 100%; max-width: 640px; margin: 0 auto; position: relative; }
.greet { font-size: 44px; line-height: 1.18; margin-bottom: 12px; }
.gsub { font-size: 13.5px; color: var(--ink3); margin-bottom: 32px; }
.dnote {
  font-size: 13px; line-height: 1.7; color: var(--ink2); margin: -18px 0 26px;
  padding-left: 12px; border-left: 3px solid var(--acc2bg);
}
.gsub .mono { font-size: 12.5px; }
.samples { display: flex; flex-direction: column; gap: 8px; margin-top: 34px; }
.slabel { font-size: 11px; letter-spacing: 0.1em; color: var(--ink3); padding: 0 2px 2px; }
.sample {
  display: flex; align-items: center; gap: 14px; cursor: pointer;
  padding: 8px 16px 8px 8px; border-radius: 999px;
  background: color-mix(in srgb, var(--paper) 55%, transparent);
}
.sample:hover { background: var(--accbg); }
.snum { width: 30px; height: 30px; flex: none; border-radius: 50%; background: var(--acc2bg); color: var(--acc2deep); display: flex; align-items: center; justify-content: center; font-size: 13px; }
.stext { font-size: 14.5px; color: var(--ink); }
.dsrc { display: block; font-size: 11.5px; color: var(--ink3); margin-top: 2px; }
.stag { font-size: 11.5px; color: var(--accink); background: var(--accbg); border-radius: 999px; padding: 2px 10px; flex: none; }
.sarrow { margin-left: auto; color: var(--acc); font-size: 15px; }

.msgs { flex: 1; overflow-y: auto; padding: 28px 28px 8px; }
.col { max-width: 680px; margin: 0 auto; display: flex; flex-direction: column; gap: 24px; }
.urow { display: flex; justify-content: flex-end; }
.ububble { max-width: 76%; background: var(--surface); border-radius: var(--r-md); border-bottom-right-radius: 4px; padding: 10px 18px; font-size: 15px; }
.cbottom { flex: none; padding: 10px 28px 20px; display: flex; justify-content: center; }
.cwrap { width: 100%; max-width: 680px; }
</style>
