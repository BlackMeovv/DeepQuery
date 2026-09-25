<script setup lang="ts">
import { computed } from "vue";
import { exportCsv } from "../lib/csv";

type Cell = string | number | null;
const props = defineProps<{ columns: string[]; rows: Cell[][]; rowCount: number; label?: string }>();

const numCols = computed(() =>
  props.columns.map((_, i) => props.rows.length > 0 && props.rows.every((r) => r[i] === null || typeof r[i] === "number")),
);

// 千分位让大数一眼可读；年份、编号类的列保持原样（2,018 年就不对了）。导出 CSV 仍是原始值
const plainCols = computed(() =>
  props.columns.map(
    (c, i) =>
      /年|year|id|编号|code|序号/i.test(c) ||
      props.rows.every((r) => r[i] === null || (Number.isInteger(r[i]) && (r[i] as number) >= 1900 && (r[i] as number) <= 2100)),
  ),
);

function show(v: Cell, i: number): string {
  if (typeof v !== "number" || plainCols.value[i] || Math.abs(v) < 10000) return String(v);
  return v.toLocaleString("en-US", { maximumFractionDigits: 6 });
}
</script>

<template>
  <div class="card">
    <div class="head">
      <span class="label">{{ label || "查询结果" }} · {{ rowCount }} 行</span>
      <button class="export" @click="exportCsv(columns, rows)">导出 CSV</button>
    </div>
    <div class="wrap">
      <table>
        <thead>
          <tr>
            <th class="idx">#</th>
            <th v-for="(c, i) in columns" :key="c" :class="{ num: numCols[i] }">{{ c }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, ri) in rows" :key="ri">
            <td class="idx mono">{{ ri + 1 }}</td>
            <td v-for="(v, ci) in r" :key="ci" :class="{ num: numCols[ci] }">
              <i v-if="v === null">NULL</i><template v-else>{{ show(v, ci) }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="rowCount > rows.length" class="more">
      共 {{ rowCount }} 行，展示前 {{ rows.length }} 行
    </div>
  </div>
</template>

<style scoped>
.card { border: 1px solid var(--line); border-radius: var(--r-md); overflow: hidden; background: var(--paper); }
.head { display: flex; align-items: center; padding: 8px 16px; background: var(--surface); }
.label { font-size: 12.5px; font-weight: 600; color: var(--ink2); }
.export { margin-left: auto; border: none; background: var(--accbg); color: var(--accink); font-size: 12px; font-weight: 600; cursor: pointer; border-radius: 999px; padding: 3px 12px; }
.export:hover { filter: brightness(0.96); }
.wrap { max-height: 280px; overflow: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th { position: sticky; top: 0; background: var(--paper); color: var(--ink3); font-weight: 600; font-size: 12px; text-align: left; padding: 8px 16px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th.idx { font-weight: 400; text-align: right; width: 44px; }
td { padding: 8px 16px; border-bottom: 1px solid var(--line); color: var(--ink); white-space: nowrap; font-variant-numeric: tabular-nums; }
tr:last-child td { border-bottom: none; }
td.idx { text-align: right; color: var(--ink3); font-size: 12px; }
th.num, td.num { text-align: right; }
td i { color: var(--ink3); }
.more { padding: 6px 16px; font-size: 12px; color: var(--ink3); border-top: 1px solid var(--line); }
</style>
