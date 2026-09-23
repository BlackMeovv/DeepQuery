# 前端开发规范：自建 Vue3 控制台

> 面向：自己用 Vue3 重写控制台 UI。后端 API 已全部就绪且稳定，本文是完整契约。
> 内置单文件页（`GET /`）保留作为零依赖后备和 API 参考实现，两者互不影响。

## 一、推荐技术栈

- **Vue 3 + Vite + TypeScript + Pinia**（组合式 API）
- 组件库二选一：
  - **Arco Design Vue**（字节开源）——本身就是内部数据平台风格，与后端现用的 #165dff 主色一致，最贴合"生产工具"观感
  - **Element Plus**——国内使用最广、生态最成熟
- SQL 高亮：`shiki` 或 `highlight.js`；图表（可选进阶）：`echarts` 直接在前端渲染查询结果
- **不需要任何 SSE 库**：`fetch` + `ReadableStream` 自己按空行切事件即可（见 `web/src/lib/api.ts` 的 `askStream`）；
  只用不带上下文的 GET 版接口时，原生 `EventSource` 也行（参考内置页的实现）

## 二、开发联调（二选一）

```js
// 方式 A（推荐）：vite.config.ts 代理，零后端配置
server: { proxy: { "/api": "http://localhost:8000", "/charts": "http://localhost:8000", "/healthz": "http://localhost:8000" } }
```

```bash
# 方式 B：后端开 CORS
echo "CORS_ALLOW_ORIGINS=http://localhost:5173" >> .env
```

联调时后端起 mock 模式即可（不花钱、毫秒级响应）：`LLM_MOCK=1 uv run deepquery serve`

## 三、页面结构与控件清单（按区域）

| 区域 | 内容 | Arco 组件参考 | Element 组件参考 |
|---|---|---|---|
| 顶栏 | 品牌、环境信息（db/model/cache/MOCK 标）、主题切换 | Layout.Header + Tag + Dropdown | el-header + el-tag + el-dropdown |
| 左栏·数据表 | 表→列两级树，点击插入到输入框 | Tree | el-tree |
| 左栏·口径记忆 | 列表 + 删除确认 + 添加输入框 | List + Popconfirm + Input | el-list 自组 + el-popconfirm |
| 左栏·查询历史 | 可点击列表（localStorage）、清空 | List | 自组 |
| 中栏·查询区 | 输入框、"生成图表"开关、查询/停止按钮（loading 态）、示例问题 | Input + Switch + Button + Tag | el-input + el-switch + el-button |
| 中栏·SQL | 代码块（高亮+复制）、缓存命中标记 | Typography + shiki | 同左 |
| 中栏·结果 | 数据表格（固定表头、数字列右对齐、行号）、导出 CSV、行数提示 | Table | el-table |
| 中栏·回答 | 文本卡片；防幻觉拦截时给 Warning 样式 | Alert/Card | el-alert |
| 中栏·图表 | 图片预览（后端沙箱 PNG）；进阶：用 ECharts 前端直出 | Image | el-image |
| 右栏·运行过程 | 时间线/步骤条：每步状态 + 模型思路引用块 + 错误红字 | Timeline / Steps | el-timeline |
| 右栏·尝试详情 | 折叠面板：每次失败的 SQL + 错误分类 | Collapse | el-collapse |
| 右栏·本次消耗 | 键值描述列表（status/calls/tokens/cost/latency/cache） | Descriptions | el-descriptions |
| 全局 | 骨架屏、空状态、错误提示 | Skeleton/Empty/Message | 同名 |

状态管理建议（Pinia 一个 store 就够）：`envInfo` / `schema` / `memory[]` / `history[]` /
`running` / `tasks[]`（运行过程事件流）/ `result`（final 载荷）。

## 四、后端 API 契约（稳定，可放心依赖）

### 1. `GET /healthz`
```json
{ "ok": true, "cache": "memory|redis", "mock": false, "db": "ecommerce.sqlite", "model": "deepseek-chat",
  "protected": false,                       // true=开启了访问口令
  "dataset_note": "一家虚构电商……",         // 空状态展示的数据说明，可能为空串
  "dataset_source": "",                      // 数据来源与许可（真实数据集才有）
  "samples": [ { "q": "哪个客户最好？", "tag": "会先问你口径" } ] }  // 首页示例问题，随数据集切换；自有库为空
```

### 2. `GET /api/schema` — 库表结构（左栏树）
```json
{ "tables": [ { "name": "customers", "columns": [ { "name": "city", "type": "TEXT" } ] } ] }
```

### 3. 记忆管理
- `GET /api/memory?user=default` → `{ "notes": [ { "id": 1, "note": "…", "created_at": "…" } ] }`
- `POST /api/memory`，body `{ "note": "…", "user": "default" }` → `{ "id": 2 }`（note 1-500 字）
- `DELETE /api/memory/{id}?user=default` → `{ "ok": true }`（404=不存在）

### 4. `POST /api/ask` — 核心接口，SSE 流

请求体（JSON）：
```json
{
  "question": "那按州呢？",
  "chart": false,
  "user": "default",
  "fresh": false,       // true=跳过缓存强制重跑
  "clarify": true,      // 允许 Agent 在口径不明或缺数据时先反问；用户回答确认后的追问传 false，避免来回拉扯
  "code": null,         // 开启访问口令时必填
  // 同一会话里之前的几轮（旧的在前，最多 3 轮），让 Agent 听懂"那按州呢""这些用了哪些字段"这类追问。
  // 只拼进提示词供模型参考、从不执行；缓存按它区分。question ≤500 字、sql ≤4000 字、answer ≤600 字。
  // sql 建议传上一轮 final 里的 predicted_sql（模型原始 SQL）：守卫改写版末尾的 LIMIT 会被模型照抄
  "history": [ { "question": "延迟送达的订单评分低多少？", "sql": "SELECT …", "answer": "低 2.02 分。" } ]
}
```
用 POST 而不是 GET：带上之前几轮的 SQL 后，URL 会超过 nginx 默认 8KB 的请求行上限；口令也不会进访问日志。
`GET /api/ask?question=…&chart=0|1&user=default&clarify=1&fresh=1&code=…` 仍然保留（不带对话上下文，内置单文件页在用）。

`Content-Type: text/event-stream`。响应里有三类事件（另有 `: ping` 注释行做心跳，忽略即可）：

**`event: node`**（每完成一个节点推一条，驱动右栏运行过程）：
```json
{
  "node": "generate_sql | execute | repair | chart | summarize | fallback | clarify | explain",
  "label": "生成 SQL",
  "thought": "模型的一句话思路（generate_sql/repair 才有，可无）",
  "sql": "生成的 SQL（generate_sql/repair 才有）",
  "ok": false,               // 仅 execute/chart 携带
  "error_kind": "no_such_column",   // 失败时携带：结构化错误分类
  "error_message": "…"
}
```

**`event: delta`**：回答逐字输出，`{"text": "当前这次生成的累积全文"}`（重写时整体替换）。

**`event: final`**（一次且仅一次，之后连接结束）：
```json
{
  // ok_meta：问的是口径 / 表结构 / 之前的查询怎么算的，Agent 依据 schema 与业务字典直接回答，
  // 没有查询数据（sql 为 null、没有结果表），前端出处应写"依据表结构与业务口径 · 未查询数据"
  "status": "ok | ok_empty | ok_meta | failed | budget_exceeded | needs_clarification",
  "cached": false,                  // true=缓存命中（此时没有 node 事件，直接 final）
  "answer": "自然语言回答",
  "sql": "实际执行的 SQL（含守卫注入的 LIMIT）",
  "predicted_sql": "模型原始 SQL",
  "columns": ["status", "cnt"],
  "rows": [["completed", 847]],     // 最多 50 行；单元格可能为 null
  "row_count": 4,
  "attempts": [ { "sql": "…", "ok": false, "error_kind": "…", "error_message": "…" } ],
  "selected_tables": ["orders"] ,   // Schema RAG 选表（未启用为 null）
  // 本次运行实际注入 prompt 的上下文（透明化面板用）；缓存命中或旧版本可能为 null
  "context_used": { "glossary": ["GMV"], "examples": ["各品类的成交金额"], "memories": ["口径：只统计已完成订单"] },
  "hallucination_blocked": false,   // true=回答被防幻觉拦截降级（UI 应给警示态）
  "source_tables": ["orders", "reviews"],  // 出处：结果来自哪几张表
  "numbers_verified": 1,            // 回答里核对过出处的数字个数
  "chart_url": "/charts/chart-ab12….png",  // 或 null；chart_error 为失败原因
  "chart_error": null,
  // status=needs_clarification 时：Agent 没有写 SQL，而是要向用户确认（其余时候为 null）
  // term 为空表示"缺数据"类确认，options 是能回答的相近问法，可直接作为新问题提交
  "clarification": { "question": "你说的最好的客户按什么来排？", "term": "最好的客户", "options": ["按累计消费金额", "按下单次数"] },
  "usage": { "llm_calls": 2, "total_tokens": 930, "cost": 0.000271, "prompt_tokens": 0, "completion_tokens": 0, "unmetered_calls": 0 },
  "latency_ms": 2310
}
```

错误处理：读流出错或没收到 final 就断开 = 连接中断；停止查询 = `AbortController.abort()`，服务端随即停止调用模型。
口令错误返回 401；参数校验失败返回 422（question 为空/超 2000 字，history 超 3 轮或超长，note 超 500 字）。

### 5. `GET /charts/{name}` — 沙箱图表 PNG（final 里的 chart_url 直接当 `<img src>`）

### 6. `GET /metrics` — Prometheus 文本（前端一般不用）

## 五、交互要点（照抄内置页的行为即可）

1. 运行中：查询按钮 → 停止按钮；右栏先放一个"思考中"pending 项，node 事件逐条插到它前面
2. `cached: true` 时不会有 node 事件——直接渲染 final，标注"缓存命中"，usage 是本次真实消耗（全 0）
3. 数字列判定：整列 `null | number` 即右对齐 + 等宽字体
4. `hallucination_blocked: true` → 回答区用警示样式并说明"已降级为原始查询结果"
5. 历史/主题存 localStorage；支持 `?q=…&chart=1` 打开即自动执行（录 demo 用）
6. `needs_clarification`：消息里显示 question，输入框替换为底部确认面板（options 编号可点选，数字键 / ↑↓ + Enter 选择，Esc 跳过，末行可自由输入）。口径类（有 term）把选择拼回原问题
   `原问题（补充说明：「term」指选项）` 再以 `clarify=0` 提问；
   缺数据类（term 为空）直接以选项作为新问题提问。确认结果不进缓存

## 六、构建与部署

`vite build` 产物是纯静态文件，两种上线方式：
- 独立托管（Nginx/静态站点），API 走同域代理
- 或放进仓库 `web/dist`，后端挂 StaticFiles（要走这条路时告诉我，我把挂载写好）
