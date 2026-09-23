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
const remember = ref(false);
const otherInput = ref<HTMLInputElement | null>(null);

function choose(text: string) {
  if (!text.trim() || store.running) return;
  store.answerClarification(props.msg.id, text, remember.value);
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
  if (target.tagName === "INPUT") return (target as HTMLInputElement).type !== "checkbox";
  return target.tagName === "TEXTAREA" || target.isContentEditable;
}

function onKey(e: KeyboardEvent) {
  if (e.isComposing || e.altKey || e.ctrlKey || e.metaKey || isTypingElsewhere(e.target)) return;
  const n = options.value.length;
  if (e.key === "Escape") {
    e.preventDefault();
    store.skipClarification(props.msg.id);
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
      <span class="dicon">?</span>
      <div class="dq">
        <template v-if="c.term">确认「{{ c.term }}」的口径</template>
        <template v-else>换一个能用现有数据回答的问法</template>
      </div>
    </div>

    <div class="dopts">
      <div
        v-for="(o, i) in options"
        :key="o"
        class="opt"
        :class="{ on: active === i }"
        @mouseenter="active = i"
        @click="choose(o)"
      >
        <span class="onum">{{ i + 1 }}</span>
        <span class="otext">{{ o }}</span>
        <span class="oenter">↵</span>
      </div>
      <div class="opt other" :class="{ on: active === options.length }" @click="focusOther">
        <span class="onum">{{ options.length + 1 }}</span>
        <input
          ref="otherInput"
          v-model="other"
          :placeholder="c.term ? '其他说法，直接输入…' : '或者换个问法…'"
          @focus="active = options.length"
        />
        <button v-if="other.trim()" class="osend" @click.stop="choose(other)">发送</button>
      </div>
    </div>

    <div class="dfoot">
      <label v-if="c.term" class="remember">
        <input v-model="remember" type="checkbox" />
        记住选择，以后「{{ c.term }}」都按它理解
      </label>
      <span class="keys">↑↓ 选择 · Enter 确认</span>
      <button class="skip" @click="store.skipClarification(msg.id)">跳过 <kbd>Esc</kbd></button>
    </div>
  </div>
</template>

<style scoped>
.dock {
  background: var(--paper); border: 1px solid var(--acc); border-radius: var(--r-lg);
  box-shadow: var(--sh-md); padding: 14px 10px 8px; animation: fadeUp 0.2s ease;
}
.dhead { display: flex; align-items: center; gap: 10px; padding: 0 10px 8px; }
.dicon {
  width: 22px; height: 22px; flex: none; border-radius: 50%;
  background: var(--accbg); color: var(--accink); font-size: 12px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.dq { font-size: 13px; color: var(--accink); font-weight: 600; }
.dopts { display: flex; flex-direction: column; gap: 2px; }
.opt { display: flex; align-items: center; gap: 12px; padding: 8px 10px; border-radius: 12px; cursor: pointer; }
.opt.on { background: var(--accbg); }
.onum {
  width: 24px; height: 24px; flex: none; border-radius: 8px; background: var(--soft); color: var(--ink2);
  font-size: 12.5px; display: flex; align-items: center; justify-content: center;
}
.opt.on .onum { background: var(--acc); color: var(--paper); }
body[data-theme="dark"] .opt.on .onum { color: #201e1d; }
.otext { font-size: 14.5px; color: var(--ink); }
.oenter { margin-left: auto; color: var(--accink); font-size: 13px; opacity: 0; }
.opt.on .oenter { opacity: 1; }
.other input {
  flex: 1; min-width: 0; border: none; outline: none; background: none;
  color: var(--ink); font-size: 14.5px; font-family: inherit; padding: 2px 0;
}
.osend {
  border: none; border-radius: 999px; background: var(--acc); color: var(--paper); cursor: pointer;
  font-size: 12.5px; font-weight: 600; padding: 4px 14px;
}
body[data-theme="dark"] .osend { color: #201e1d; }
.dfoot {
  display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
  margin-top: 8px; padding: 8px 10px 0; border-top: 1px solid var(--line); font-size: 12px; color: var(--ink3);
}
.remember { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; color: var(--ink2); }
.remember input { accent-color: var(--acc); margin: 0; }
.keys { margin-left: auto; }
.skip {
  border: none; background: none; cursor: pointer; color: var(--ink3); font-size: 12px; font-family: inherit;
  display: inline-flex; align-items: center; gap: 6px; padding: 2px 0;
}
.skip:hover { color: var(--accink); }
kbd { font-family: inherit; font-size: 11px; border: 1px solid var(--line); border-radius: 5px; padding: 0 5px; }
</style>
