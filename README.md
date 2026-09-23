# DeepQuery

[![CI](https://github.com/BlackMeovv/DeepQuery/actions/workflows/ci.yml/badge.svg)](https://github.com/BlackMeovv/DeepQuery/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11-blue)
[English](README.en.md)

**用中文问数据，拿到可追溯、可验证的答案。**

企业里大量临时取数需求不在任何仪表盘上，业务同学只能提工单排队等分析师写 SQL。
DeepQuery 把"选表 → 写 SQL → 执行 → 出错修正 → 给结论"做成一个 Agent，
每个回答都能展开到它执行的 SQL 和原始结果。

![对话与运行过程](docs/assets/ui-run.png)

## 设计思路

让模型生成 SQL 并不难，难的是**让人敢用它的结果**。DeepQuery 的做法是不依赖模型自觉，
把可靠性放在确定性的代码里：

- **模型只提议，代码来把关。** SQL 先经 sqlglot 语法树校验（只放行单条 SELECT、表白名单、
  强制行数上限），再以只读方式执行；修复轮次与 token/金额预算都有硬上限。
- **回答里的每个数字都要有出处。** 数字必须能在查询结果、问题或 SQL 里找到（支持千分位、
  百分号、万/亿单位的四舍五入匹配），找不到就让模型重写，仍不合格则只返回原始结果表。
- **改动靠评测说话。** 每次调整都在同一批题上做按题配对比较，结论附带置信区间；
  消融结果不支持的假设会被写下来并据此修改设计。

## 评测结果

模型：gpt-5.5（经 OpenAI 兼容接口）。原始结果与逐题记录在 [eval/results/](eval/results/)，
下表可用 `make report` 从这些 JSON 复算。

| 评测集 | 配置 | EX（95% CI） |
|---|---|---|
| 业务集 dev · 165 题 × 3 次 | 基线 | 93.3% [90.1, 95.6] |
| 业务集 dev · 165 题 × 3 次 | 业务口径注入 + 输出纪律 | **97.8%** [95.5, 98.9] |
| 业务集 holdout · 71 题 × 3 次 | 同上（调优期间未运行） | **97.7%** [92.3, 99.3] |
| BIRD dev · 150 题固定子集 | 全量 schema 直供 | 64.7% [56.7, 71.9] |
| BIRD dev · 150 题固定子集 | 检索选表 | 61.3% [53.3, 68.8] |

EX 为执行准确率：比较结果集而非 SQL 文本，与 BIRD/Spider 口径一致。

**口径修复的提升是真实的。** 在同一批 165 题上按题配对：平均 +4.4 个点，
95% CI [+1.7, +7.2]；23 题变好、7 题变差，符号检验 p = 0.005。
失败分析与每类的修复过程见 [docs/badcases.md](docs/badcases.md)。

**检索选表没有带来可测的收益。** BIRD 上两种方式按题配对的差异为 +3.3 个点，
95% CI [−2.1, +8.7]，p = 0.33，无法区分；而检索只节省约 3% 的 token，
却多了一个召回失败点（选表召回率 95.5%）。因此默认把完整 schema 直接交给模型，
只有 schema 超过体积阈值时才启用检索。

**区间的算法。** 同一题重复运行 3 次，结果高度相关，不能当作 495 个独立样本。
区间按题目聚类估计设计效应（dev 1.52、holdout 2.21）后折算有效样本量再计算。

## 架构

```mermaid
flowchart LR
    Q([提问]) --> CTX["上下文组装<br/>schema 直供或检索选表<br/>+ 业务口径 / 例句 / 记忆"]
    CTX --> GEN[生成 SQL]
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

外层流转由 LangGraph 状态机负责；修复节点内部是手写的循环：按数据库返回的错误类型
（表/列不存在、语法错误、超时、守卫拒绝、空结果）给出针对性提示，并检测模型是否在
重复提交同一条 SQL。

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
make demo-db                  # 生成电商演示库（客户/商品/订单/支付，6 张表）
make ask Q="下单次数最多的前5名客户是谁？"
make serve                    # 网页 http://localhost:8000
```

也可以 `docker compose up --build` 一键启动服务、Redis、Prometheus 和 Grafana。
部署到服务器见 [docs/DEPLOY.md](docs/DEPLOY.md)。

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

生产接入请使用**只读数据库账号**：它是权限的硬边界，应用层守卫是它之上的第二道防线。
`.env` 中的 `ALLOWED_TABLES` 可以进一步限制 Agent 可见的表。

## 安全设计

| 风险 | 措施 |
|---|---|
| 模型写出修改数据的 SQL | 语法树只放行单条 SELECT；SQLite 以只读 URI + `query_only` + authorizer 打开；服务器引擎用只读会话与只读账号 |
| 访问未授权的表 | 表白名单同时作用于提示词中的 schema、守卫和 API，模型看不到也查不了 |
| 模型生成的画图代码 | 不在主进程执行：宿主机有 Docker 时用断网容器（内存 / CPU / 进程数限额），否则用带资源限额的独立子进程；产物只接受普通 PNG 文件，不跟随符号链接 |
| 回答编造数字 | 数字溯源校验，重写一次仍不合格则降级为原始结果 |
| 成本失控 | 单次提问的 token 与金额预算熔断，修复轮次上限 |

## 局限

- **业务评测集由模板生成。** 236 道题由模板参数展开，去掉参数后只有 32 种 SQL 结构；
  holdout 按题随机切分，其中 69/71 题的 SQL 结构在 dev 中出现过。因此 holdout 能说明
  结果不依赖具体参数，但不能证明对新题型的泛化。按结构切分的评测集尚未构建。
- **BIRD 只跑了 150 题子集、单次、单一模型**，区间较宽，不能与官方排行榜直接比较。
- **只支持单轮提问。** "那上个月呢"这类依赖上文的追问目前会被当作独立问题处理。
- **访问控制是演示级别**：全站访问口令 + 表级白名单，不是多租户鉴权。

## 更多文档

- [docs/DEPLOY.md](docs/DEPLOY.md)：服务器部署、nginx 反代、访问口令
- [docs/benchmarks.md](docs/benchmarks.md)：BIRD / Spider 接入与统计口径
- [docs/badcases.md](docs/badcases.md)：失败案例逐条复盘
- [docs/frontend-spec.md](docs/frontend-spec.md)：SSE 事件与 API 约定

其他入口：`uv sync --extra mcp && uv run deepquery-mcp` 启动 MCP server，可接入 Claude Desktop 等客户端；
`deepquery remember "销售额指已完成订单的成交金额"` 为当前用户保存业务口径，后续提问自动带上。
