<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { useAppStore } from "../stores/app";
import DqLogo from "./DqLogo.vue";

const store = useAppStore();
const schemaOpen = ref(false);
const memOpen = ref(false);
const openTables = ref<Record<string, boolean>>({});

function addMem() {
  const note = window.prompt("新增记忆（会注入到后续每次提问）");
  if (note && note.trim()) store.addMem(note.trim());
}

// ---- 历史会话 ⋯ 菜单与重命名 ----
const menuFor = ref<string | null>(null);
const editingId = ref<string | null>(null);
const editText = ref("");
const editInput = ref<HTMLInputElement[] | null>(null);

function openMenu(id: string) {
  menuFor.value = menuFor.value === id ? null : id;
}
async function startRename(id: string, title: string) {
  menuFor.value = null;
  editingId.value = id;
  editText.value = title;
  await nextTick();
  editInput.value?.[0]?.focus();
  editInput.value?.[0]?.select();
}
function commitRename() {
  if (editingId.value) store.renameConvo(editingId.value, editText.value);
  editingId.value = null;
}
function closeMenu() {
  menuFor.value = null;
}
onMounted(() => document.addEventListener("click", closeMenu));
onBeforeUnmount(() => document.removeEventListener("click", closeMenu));
</script>

<template>
  <aside class="side">
    <div class="brand">
      <DqLogo :size="34" />
      <div class="name serif">DeepQuery</div>
    </div>

    <div class="pad">
      <button class="newbtn" @click="store.newChat()">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.75" stroke-linecap="round">
          <path d="M5 12h14M12 5v14" />
        </svg>
        新建提问
      </button>
    </div>

    <div class="scroll">
      <div class="row" @click="schemaOpen = !schemaOpen">
        <span class="ic">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round">
            <ellipse cx="12" cy="5" rx="9" ry="3" />
            <path d="M3 5v14a9 3 0 0 0 18 0V5" />
            <path d="M3 12a9 3 0 0 0 18 0" />
          </svg>
        </span>
        库表结构
        <span class="meta">{{ store.schema.length }} 表</span>
        <span class="mchev" :class="{ open: schemaOpen }">›</span>
      </div>
      <div v-if="schemaOpen" class="tree">
        <div class="treehint">这是 Agent 能看到的全部表；点列名可插入到输入框</div>
        <div v-for="tb in store.schema" :key="tb.name">
          <div class="trow" @click="openTables[tb.name] = !openTables[tb.name]">
            <span class="chev" :class="{ open: openTables[tb.name] }">›</span>
            <span class="mono tname">{{ tb.name }}</span>
            <span class="meta">{{ tb.columns.length }}</span>
          </div>
          <div v-if="openTables[tb.name]" class="cols">
            <div
              v-for="cl in tb.columns"
              :key="cl.name"
              class="crow mono"
              title="点击插入到输入框"
              @click="store.insertToken(cl.name)"
            >
              {{ cl.name }}<span class="cty">{{ cl.type }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="row" @click="memOpen = !memOpen">
        <span class="ic">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linejoin="round">
            <path d="M12 3l2.2 4.8L19 10l-4.8 2.2L12 17l-2.2-4.8L5 10l4.8-2.2L12 3z" />
          </svg>
        </span>
        记忆
        <span class="meta">{{ store.mems.length }} 条</span>
        <span class="mchev" :class="{ open: memOpen }">›</span>
      </div>
      <div v-if="memOpen" class="mems">
        <div class="treehint">Agent 的跨会话记忆：教它你的业务口径，之后每次提问自动带上</div>
        <div v-for="mm in store.mems" :key="mm.id" class="mem">
          <span class="mtext">{{ mm.note }}</span>
          <span class="mdel" @click="store.delMem(mm.id)">✕</span>
        </div>
        <button class="madd" @click="addMem">＋ 添加记忆</button>
      </div>

      <div class="histlabel">历史</div>
      <div
        v-for="cv in store.convos"
        :key="cv.id"
        class="hist"
        :class="{ cur: cv.id === store.curConvo, menuon: menuFor === cv.id }"
        @click="store.pickConvo(cv.id)"
      >
        <input
          v-if="editingId === cv.id"
          ref="editInput"
          v-model="editText"
          class="hedit"
          @click.stop
          @keydown.enter="commitRename"
          @keydown.esc="editingId = null"
          @blur="commitRename"
        />
        <span v-else class="htext">{{ cv.title }}</span>
        <span class="hmore" title="更多操作" @click.stop="openMenu(cv.id)">⋯</span>
        <div v-if="menuFor === cv.id" class="menu" @click.stop>
          <div class="mitem" @click="startRename(cv.id, cv.title)">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
              <path d="M11.3 2.7a1.7 1.7 0 0 1 2.4 2.4L5.5 13.3 2 14l0.7-3.5 8.6-7.8z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
            </svg>
            重命名
          </div>
          <div class="mitem danger" @click="menuFor = null; store.delConvo(cv.id)">
            <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
              <path d="M2.5 4h11M6.5 4V2.8h3V4M4 4l0.7 9.2h6.6L12 4M6.6 6.5v4.5M9.4 6.5v4.5" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            删除
          </div>
        </div>
      </div>
    </div>

    <div class="foot">
      <div class="avatar">{{ store.env?.protected ? "访" : "BM" }}</div>
      <span class="uname">{{ store.env?.protected ? "访客" : "BlackMeovv" }}</span>
      <button class="theme" title="切换主题" @click="store.toggleTheme()">
        <svg v-if="store.theme === 'light'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round">
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
        </svg>
        <svg v-else width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linejoin="round">
          <path d="M20 14.1A8.9 8.9 0 1 1 9.9 4a7 7 0 0 0 10.1 10.1z" />
        </svg>
      </button>
    </div>
  </aside>
</template>

<style scoped>
.side { width: 236px; flex: none; display: flex; flex-direction: column; overflow: hidden; padding: 2px 0; }
.brand { padding: 6px 10px 18px; display: flex; align-items: center; gap: 10px; }
.name { font-size: 21px; }
.pad { padding: 0 8px 18px; }
.newbtn {
  display: inline-flex; align-items: center; gap: 8px; border: none; cursor: pointer;
  border-radius: 999px; padding: 9px 20px; font-size: 14px; font-weight: 600;
  background: var(--acc); color: var(--paper); box-shadow: var(--sh-sm);
}
body[data-theme="dark"] .newbtn { color: #201e1d; }
.newbtn:hover { filter: brightness(0.94); }
.newbtn:active { filter: brightness(0.88); }
.scroll { flex: 1; overflow-y: auto; padding: 0 8px 12px; }
.row { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-radius: 999px; cursor: pointer; font-size: 14px; color: var(--ink); }
.row:hover { background: var(--card); }
.ic { width: 16px; flex: none; display: flex; align-items: center; justify-content: center; color: var(--accink); }
.meta { margin-left: auto; font-size: 12px; color: var(--ink3); }
.mchev { font-size: 12px; color: var(--ink3); transition: transform 0.15s; display: inline-block; }
.mchev.open { transform: rotate(90deg); }
.tree { padding: 2px 0 6px; }
.treehint { font-size: 11px; color: var(--ink3); padding: 2px 12px 6px; line-height: 1.5; }
.trow { display: flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 999px; cursor: pointer; font-size: 13px; }
.trow:hover { background: var(--card); }
.chev { width: 10px; font-size: 11px; color: var(--ink3); transition: transform 0.15s; display: inline-block; }
.chev.open { transform: rotate(90deg); }
.tname { font-size: 12.5px; }
.cols { margin: 0 0 4px 26px; padding-left: 10px; border-left: 1px solid var(--line); }
.crow { display: flex; gap: 8px; padding: 2px 8px; border-radius: 999px; cursor: pointer; font-size: 12px; color: var(--ink2); }
.crow:hover { background: var(--accbg); color: var(--accink); }
.cty { color: var(--ink3); font-size: 10.5px; }
.mems { display: flex; flex-direction: column; gap: 5px; padding: 4px 4px 6px; }
.mem { display: flex; align-items: flex-start; gap: 6px; background: var(--card); border-radius: var(--r-md); padding: 7px 12px; font-size: 12px; color: var(--ink2); }
.mtext { flex: 1; }
.mdel { cursor: pointer; color: var(--ink3); line-height: 1.2; }
.mdel:hover { color: var(--err); }
.madd { align-self: flex-start; border: none; background: none; color: var(--accink); font-size: 12px; cursor: pointer; padding: 2px 6px; font-weight: 600; }
.histlabel { padding: 22px 12px 6px; font-size: 11px; letter-spacing: 0.1em; color: var(--ink3); }
.hist { position: relative; display: flex; align-items: center; gap: 6px; padding: 7px 14px; border-radius: 999px; cursor: pointer; font-size: 13.5px; color: var(--ink2); }
.hist:hover { background: var(--card); }
.hist.cur { background: var(--accbg); color: var(--accdeep); font-weight: 600; }
.htext { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.hedit { flex: 1; min-width: 0; border: 1px solid var(--acc); border-radius: 999px; background: var(--card); color: var(--ink); font-size: 13px; padding: 1px 10px; outline: none; }
.hmore { visibility: hidden; flex: none; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; color: var(--ink3); font-size: 14px; line-height: 1; border-radius: 50%; }
.hist:hover .hmore, .hist.menuon .hmore { visibility: visible; }
.hmore:hover { background: var(--soft); color: var(--ink); }
.menu { position: absolute; right: 6px; top: 30px; z-index: 20; background: var(--card); border-radius: var(--r-md); box-shadow: var(--sh-lg); padding: 5px; min-width: 118px; }
.mitem { display: flex; align-items: center; gap: 8px; padding: 6px 12px; border-radius: 999px; font-size: 13px; color: var(--ink); cursor: pointer; }
.mitem:hover { background: var(--soft); }
.mitem.danger { color: var(--err); }
.mitem.danger:hover { background: var(--errbg); }
.foot { padding: 12px 12px 6px; display: flex; align-items: center; gap: 10px; }
.avatar { width: 28px; height: 28px; border-radius: 50%; background: var(--acc2bg); color: var(--acc2deep); display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; }
.uname { font-size: 13px; color: var(--ink2); }
.theme { margin-left: auto; width: 30px; height: 30px; display: flex; align-items: center; justify-content: center; border: none; border-radius: 50%; background: none; color: var(--ink3); cursor: pointer; padding: 0; }
.theme:hover { background: var(--card); color: var(--ink); }
</style>
