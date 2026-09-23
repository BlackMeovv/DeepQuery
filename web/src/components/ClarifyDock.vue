<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { useAppStore, type AiMsg } from "../stores/app";

// Agent 等待确认时替换输入框：选项可点击，也支持数字键 / ↑↓ + Enter / Esc 跳过
const props = defineProps<{ msg: AiMsg }>();
const store = useAppStore();

const c = computed(() => props.msg.clarification!);
const options = computed(() => c.value.options);
const active = ref(0); // 0..n-1 为选项，n 为"其他说法"输入行
const other = ref("");
const otherInput = ref<HTMLInputElement | null>(null);

function choose(text: string) {
  if (!text.trim() || store.running) return;
  store.answerClarification(props.msg.id, text);
}

function skip() {
  store.skipClarification(props.msg.id);
}

function focusOther() {
  active.value = options.value.length;
  nextTick(() => otherInput.value?.focus());
}

function move(delta: number) {
  const n = options.value.length + 1;
  active.value = (active.value + delta + n) % n;
  if (active.value === options.value.length) focusOther();
  else otherInput.value?.blur();
}

function isTypingElsewhere(target: EventTarget | null) {
  if (!(target instanceof HTMLElement) || target === otherInput.value) return false;
  return target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable;
}

function onKey(e: KeyboardEvent) {
  if (e.isComposing || e.altKey || e.ctrlKey || e.metaKey || isTypingElsewhere(e.target)) return;
  const n = options.value.length;
  if (e.key === "Escape") {
    e.preventDefault();
    skip();
  } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    move(e.key === "ArrowDown" ? 1 : -1);
  } else if (e.target === otherInput.value) {
    if (e.key === "Enter") {
      e.preventDefault();
      choose(other.value);
    }
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (active.value < n) choose(options.value[active.value]);
    else focusOther();
  } else if (/^[1-9]$/.test(e.key)) {
    const k = Number(e.key);
    if (k <= n) {
      e.preventDefault();
      choose(options.value[k - 1]);
    } else if (k === n + 1) {
      e.preventDefault();
      focusOther();
    }
  }
}

onMounted(() => {
  window.addEventListener("keydown", onKey);
  if (!options.value.length) focusOther();
});
onBeforeUnmount(() => window.removeEventListener("keydown", onKey));
</script>

<template>
  <div class="dock">
    <!-- 问题本身已显示在上方消息里，这里只说明要选什么 -->
    <div class="dhead">
      <span class="dtitle">{{ c.term ? `「${c.term}」指的是？` : "换一个能用现有数据回答的问法" }}</span>
      <button class="skip" title="跳过（Esc）" @click="skip">
        跳过
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>
    </div>

    <div class="dopts">
      <div
        v-for="(o, i) in options"
        :key="o"
        class="opt"
        :class="{ on: active === i }"
        :style="{ animationDelay: `${i * 35}ms` }"
        @mouseenter="active = i"
        @click="choose(o)"
      >
        <span class="onum">{{ i + 1 }}</span>
        <span class="otext">{{ o }}</span>
        <svg class="oarrow" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
          <path d="M5 12h14M13 6l6 6-6 6" />
        </svg>
      </div>

      <div
        class="opt other"
        :class="{ on: active === options.length }"
        :style="{ animationDelay: `${options.length * 35}ms` }"
        @click="focusOther"
      >
        <span class="onum">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 20h4L19 9l-4-4L4 16v4z" />
          </svg>
        </span>
        <input
          ref="otherInput"
          v-model="other"
          :placeholder="c.term ? '都不是？直接说你的意思' : '或者输入你想问的'"
          @focus="active = options.length"
        />
        <button class="osend" :class="{ ready: other.trim() }" :disabled="!other.trim()" @click.stop="choose(other)">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 19V5M6 11l6-6 6 6" />
          </svg>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dock {
  background: var(--paper); border: 1px solid var(--line); border-radius: var(--r-lg);
  box-shadow: var(--sh-md); padding: 12px 8px 8px; animation: fadeUp 0.22s ease;
}
.dhead { display: flex; align-items: center; gap: 12px; padding: 0 10px 8px 14px; }
.dtitle { font-size: 13.5px; font-weight: 600; color: var(--ink2); }
.skip {
  margin-left: auto; border: none; background: none; cursor: pointer; font-family: inherit;
  display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px;
  font-size: 12.5px; color: var(--ink3);
}
.skip:hover { background: var(--soft); color: var(--ink); }

.dopts { display: flex; flex-direction: column; gap: 2px; }
.opt {
  display: flex; align-items: center; gap: 13px; padding: 10px 12px 10px 14px; border-radius: 14px;
  cursor: pointer; animation: fadeUp 0.25s ease both; transition: background 0.12s;
}
.opt.on { background: var(--accbg); }
.onum {
  width: 24px; height: 24px; flex: none; border-radius: 50%;
  border: 1px solid var(--line); color: var(--ink3); font-size: 12px;
  display: flex; align-items: center; justify-content: center; transition: all 0.12s;
}
.opt.on .onum { border-color: var(--acc); background: var(--acc); color: var(--paper); }
body[data-theme="dark"] .opt.on .onum { color: #201e1d; }
.otext { font-size: 15px; color: var(--ink); line-height: 1.5; }
.opt.on .otext { color: var(--accdeep); }
.oarrow { margin-left: auto; flex: none; color: var(--accink); opacity: 0; transform: translateX(-4px); transition: all 0.15s; }
.opt.on .oarrow { opacity: 1; transform: none; }

.other { margin-top: 4px; }
.other:not(.on) { box-shadow: inset 0 1px 0 var(--line); border-radius: 0 0 14px 14px; }
.other input {
  flex: 1; min-width: 0; border: none; outline: none; background: none;
  color: var(--ink); font-size: 15px; font-family: inherit; padding: 2px 0;
}
.other input::placeholder { color: var(--ink3); }
.osend {
  width: 30px; height: 30px; flex: none; border: none; border-radius: 50%; cursor: default;
  background: var(--soft); color: var(--ink3); display: flex; align-items: center; justify-content: center;
  transition: all 0.15s;
}
.osend.ready { background: var(--acc); color: var(--paper); cursor: pointer; }
body[data-theme="dark"] .osend.ready { color: #201e1d; }
</style>
