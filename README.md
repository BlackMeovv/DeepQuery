<div align="center">

<img src="docs/assets/logo.svg" width="88" alt="DeepQuery logo">

# DeepQuery

**用中文问数据，拿到可追溯、可验证的答案**

企业数据问答 Agent · 自然语言转 SQL · 自带评测闭环

[![CI](https://github.com/BlackMeovv/DeepQuery/actions/workflows/ci.yml/badge.svg)](https://github.com/BlackMeovv/DeepQuery/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-Agent-c67139)
![FastAPI](https://img.shields.io/badge/FastAPI-SSE-009688?logo=fastapi&logoColor=white)
![Vue](https://img.shields.io/badge/Vue-3-42b883?logo=vuedotjs&logoColor=white)

[快速开始](#快速开始) · [评测结果](#评测结果) · [架构](#架构) · [部署](docs/DEPLOY.md) · [English](README.en.md)

</div>

<br>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/metrics-dark.png">
  <img src="docs/assets/metrics-light.png" alt="业务集执行准确率 97.8%；失败驱动修复提升 +4.4pt；BIRD 150 题子集 64.7%；280+ 离线测试" width="100%">
</picture>

<img src="docs/assets/demo.gif" alt="从提问到回答：生成 SQL、守卫执行、逐字输出回答，并展开运行详情查看 SQL" width="100%">

## 亮点

企业里大量临时取数需求不在任何仪表盘上，业务同学只能提工单排队等分析师写 SQL。让模型生成 SQL 并不难，难的是**让人敢用它的结果**——DeepQuery 把可靠性放在确定性的代码里，而不是寄希望于模型自觉。

<table>
<tr>
<td width="33%" valign="top">

**🛡️ 语法树守卫**

只放行单条 SELECT 与白名单内的表，强制行数上限；数据库层再以只读方式执行，应用和数据库两道防线拦住写操作

</td>
<td width="33%" valign="top">

**🔢 数字溯源校验**

回答里的每个数字都要在查询结果、问题或 SQL 中找到出处；找不到就重写，仍不合格则只返回原始表格

</td>
<td width="33%" valign="top">

**🔁 按错误类型修复**

表或列不存在、语法错误、超时、空结果分别给出针对性提示；修复轮次与 token / 金额预算都有硬上限

</td>
</tr>
<tr>
<td width="33%" valign="top">

**📊 可复现的评测**

236 题自建评测集 + BIRD 公开基准；区间按题目聚类校正，每次改动都在同一批题上按题配对检验

</td>
<td width="33%" valign="top">

**🧭 上下文工程**

schema 装得下就完整交给模型，超过体积阈值才检索选表；业务口径、相似例句和用户记忆按需注入

</td>
<td width="33%" valign="top">

**⚡ 全程可追溯**

步骤与回答逐字流式推送；每个答案都能展开到执行的 SQL、原始结果和本次注入的上下文

</td>
</tr>
<tr>
<td width="33%" valign="top">

**🙋 拿不准先确认**

"最好的客户"这类没有定义的说法，先给出几种口径让用户选，而不是自己猜；问到库里没有的数据，说明缺什么并给出能回答的相近问法

</td>
<td width="33%" valign="top">

**🧠 跨会话记忆**

用户保存的口径和偏好会注入之后的每次提问，定义过的说法不再反问；公网部署时每个访客的记忆相互隔离

</td>
<td width="33%" valign="top">

**🔌 多种接入**

网页、命令行和 MCP server 共用同一套 Agent；SQLite / MySQL / PostgreSQL 只读接入，换库只改连接串

</td>
</tr>
</table>

<details>
<summary>更多截图：首页 / 暗色主题</summary>
<br>

![首页](docs/assets/ui-home.png)

![暗色主题](docs/assets/ui-dark.png)

</details>

## 评测结果

模型：gpt-5.5（经 OpenAI 兼容接口）。原始结果与逐题记录在 [eval/results/](eval/results/)，下表可用 `make report` 复算。

| 评测集 | 配置 | EX（95% CI） |
|---|---|---|
| 业务集 dev · 165 题 × 3 次 | 基线 | 93.3% [90.1, 95.6] |
| 业务集 dev · 165 题 × 3 次 | 业务口径注入 + 输出纪律 | **97.8%** [95.5, 98.9] |
| 业务集 holdout · 71 题 × 3 次 | 同上（调优期间未运行） | **97.7%** [92.3, 99.3] |
| BIRD dev · 150 题固定子集 | 全量 schema 直供 | **64.7%** [56.7, 71.9] |
| BIRD dev · 150 题固定子集 | 检索选表 | 61.3% [53.3, 68.8] |

- **失败驱动的修复带来了真实提升。** 同一批 165 题按题配对：平均 +4.4 个点，95% CI [+1.7, +7.2]，23 题变好、7 题变差，符号检验 p = 0.005。每类失败的根因与修复过程见 [docs/badcases.md](docs/badcases.md)。
- **消融结果直接改变了设计。** BIRD 上检索选表与全量直供的配对差异为 +3.3 个点，95% CI [−2.1, +8.7]，两者无法区分；而检索只省约 3% 的 token，却多了一个召回失败点（选表召回率 95.5%）。因此默认完整交给模型，只有 schema 超过体积阈值时才检索。

<sub>EX 为执行准确率，比较结果集而非 SQL 文本，与 BIRD / Spider 口径一致。同一题重复 3 次的结果高度相关，不能当作独立样本：区间按题目聚类估计设计效应（dev 1.52、holdout 2.21）后计算。</sub>

## 架构

```mermaid
flowchart LR
    Q([提问]) --> CTX["上下文组装<br/>schema 直供或检索选表<br/>+ 业务口径 / 例句 / 记忆"]
    CTX --> GEN[生成 SQL]
    GEN -- 口径不明或缺数据 --> ASK(["向用户确认<br/>给出可选口径"])
    ASK -. 用户选择后重新提问 .-> Q
    GEN --> EXE["守卫校验<br/>只读执行"]
    EXE -- 成功 --> SUM["归纳回答<br/>数字溯源校验"]
    EXE -- 成功，需要图表 --> CH["生成图表<br/>沙箱执行"]
    CH --> SUM
    EXE -- 失败，有余量 --> REP["按错误类型<br/>定向修复"]
    REP --> EXE
    EXE -- 轮次或预算用尽 --> FB["降级收尾<br/>不调用模型"]
    SUM --> OUT([回答 + SQL + 结果])
    FB --> OUT
```

外层流转由 LangGraph 状态机负责；修复节点内部是手写的循环，除了按错误类型给出提示，还会检测模型是否在重复提交同一条 SQL。

| 层 | 选型 |
|---|---|
| Agent 编排 | LangGraph 状态机 + 手写修复循环 |
| SQL 安全 | sqlglot 语法树守卫；SQLite / MySQL / PostgreSQL 只读接入 |
| 检索 | BM25（中文单字 + 双字分词）+ 可选向量检索，RRF 融合 |
| 服务 | FastAPI + SSE 流式；Redis 结果缓存；Prometheus / Grafana；Langfuse 追踪；MCP server |
| 前端 | Vue 3 + Vite + Pinia，自研样式，无 UI 组件库依赖 |
| 评测 | 自研 evalkit：执行准确率判分、聚类校正区间、按题配对比较、BIRD / Spider 适配 |

## 快速开始

需要 [uv](https://docs.astral.sh/uv/) 和任意 OpenAI 兼容的模型接口（DeepSeek、Qwen、Kimi、OpenAI、本地 Ollama 等）。

```bash
make install
cp .env.example .env          # 填入 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
make demo-db                  # 生成虚构电商演示库：240 位客户、36 个商品、1500 笔订单，6 张表
make olist-db                 # 可选：导入 Olist 巴西电商真实数据（约 10 万订单），DB_PATH 指向它即可切换
make ask Q="下单次数最多的前5名客户是谁？"
make serve                    # 网页 http://localhost:8000
```

也可以 `docker compose up --build` 一键启动服务、Redis、Prometheus 和 Grafana；部署到服务器见 [docs/DEPLOY.md](docs/DEPLOY.md)。

不配置模型 API 也能验证全部工程链路：

```bash
make test         # 280+ 个离线测试（MockLLM）
make smoke-gold   # 评测基建自检：gold SQL 回放必须 100%
```

## 接入自己的数据库

表结构在连接时自省，守卫按引擎方言解析，没有针对演示库的硬编码。换库只需改连接目标：

```bash
deepquery ask "问题" --db /path/to/your.sqlite
deepquery ask "问题" --db mysql://readonly:pwd@host:3306/yourdb       # uv sync --extra mysql
deepquery ask "问题" --db postgres://readonly:pwd@host:5432/yourdb    # uv sync --extra postgres
```

生产接入请使用**只读数据库账号**，它是权限的硬边界，应用层守卫是它之上的第二道防线；`.env` 中的 `ALLOWED_TABLES` 可以进一步限制 Agent 可见的表。

## 安全设计

| 风险 | 措施 |
|---|---|
| 模型写出修改数据的 SQL | 语法树只放行单条 SELECT；SQLite 以只读 URI + `query_only` + authorizer 打开；服务器引擎用只读会话与只读账号 |
| 访问未授权的表 | 表白名单同时作用于提示词中的 schema、守卫和 API，模型看不到也查不了 |
| 模型生成的画图代码 | 不在主进程执行：有 Docker 时用断网容器（内存 / CPU / 进程数限额），否则用带资源限额的独立子进程；产物只接受普通 PNG 文件，不跟随符号链接 |
| 回答编造数字 | 数字溯源校验，重写一次仍不合格则降级为原始结果 |
| 成本失控 | 单次提问的 token 与金额预算熔断，修复轮次上限 |
| 公网演示被刷额度 | 访问口令、每访客限流、全站每日花费上限；访客的记忆按浏览器隔离（见 [部署指南](docs/DEPLOY.md)） |

## 更多

- [docs/DEPLOY.md](docs/DEPLOY.md)：服务器部署、nginx 反代、访问口令、切换真实数据
- [docs/benchmarks.md](docs/benchmarks.md)：BIRD / Spider 接入与统计口径
- [docs/badcases.md](docs/badcases.md)：失败案例逐条复盘
- [docs/frontend-spec.md](docs/frontend-spec.md)：SSE 事件与 API 约定

`uv sync --extra mcp && uv run deepquery-mcp` 启动 MCP server，可接入 Claude Desktop 等客户端；`deepquery remember "销售额指已完成订单的成交金额"` 为当前用户保存业务口径，之后的提问会自动带上。
