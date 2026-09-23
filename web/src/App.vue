<script setup lang="ts">
import { onMounted } from "vue";
import ChatMain from "./components/ChatMain.vue";
import InspectorPanel from "./components/InspectorPanel.vue";
import SideBar from "./components/SideBar.vue";
import { useAppStore } from "./stores/app";

const store = useAppStore();
onMounted(async () => {
  // 先完成初始化（取环境、验访问口令、分配访客 ID），再处理链接里的 ?q= 提问，
  // 否则公网部署下这条提问会在口令就绪前发出而失败，且落在共享的 default 访客上
  await store.init();
  // ?theme=dark|light 与 ?q=…&chart=1：分享链接 / 录 demo 用
  const params = new URLSearchParams(location.search);
  const theme = params.get("theme");
  if (theme === "dark" || theme === "light") {
    store.theme = theme;
    document.body.dataset.theme = theme;
  }
  const q = params.get("q");
  if (q) {
    if (params.get("chart") === "1") store.chartOn = true;
    store.ask(q);
  }
});
</script>

<template>
  <div class="layout">
    <SideBar />
    <ChatMain />
    <InspectorPanel v-if="store.panelMsg" :msg="store.panelMsg" />
  </div>
</template>

<style scoped>
/* 浮动卡片布局：奶油底上并排三块，侧栏透明、主区与详情面板是圆角卡片 */
.layout {
  height: 100vh;
  display: flex;
  gap: 16px;
  padding: 14px;
  background: var(--paper);
  color: var(--ink);
  overflow: hidden;
}
</style>
