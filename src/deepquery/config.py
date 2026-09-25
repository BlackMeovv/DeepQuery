"""运行配置：全部从环境变量 / .env 读取，见仓库根目录 .env.example。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM 接入
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_model: str = "deepseek-chat"

    # 成本记账（每百万 token 单价，仅用于统计）
    llm_price_input_per_m: float = 0.27
    llm_price_output_per_m: float = 1.10

    # 采样与超时
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 60
    llm_max_retries: int = 3

    # Agent 行为
    agent_max_repair_rounds: int = 3
    agent_max_tokens_per_run: int = 200_000
    agent_max_cost_per_run: float = 0.05
    # 分析模式：先拆几步、最多几步（含看完结果后追加的下钻），整次分析的预算是单次提问的几倍
    analysis_plan_steps: int = 4
    analysis_max_steps: int = 6
    analysis_budget_factor: float = 4.0

    # 表结构怎么交给模型：
    #   off=全量直供 / on=检索选表（BM25 + 可选向量）/ disclose=渐进式披露（先给表目录，模型选表后再展开）
    #   auto=装得下就全量直供，装不下走渐进式披露
    schema_rag: str = "auto"
    schema_rag_top_k: int = 6
    # auto 的分界：全量 schema 字符数超过它（且表数多于 top_k）才不再全量直供。
    # BIRD 150 题消融：全量直供比检索选表高 3.3 个点（配对差异不显著），检索只省 ~3% token、
    # 多一个召回失败点——所以按体积决定，装不下时用能补救漏选的渐进式披露，而不是一次性检索
    schema_rag_auto_max_chars: int = 16000
    schema_disclose_max_tables: int = 8  # 渐进式披露一次最多展开几张表
    # 查询返回空结果时，自动查出过滤列真实出现过的取值交给修复轮（关掉可做消融）
    repair_value_probe: bool = True
    # 多候选投票（自洽性）：1 = 关闭。≥2 时先多采样一条 SQL，两条执行结果一致就采用；
    # 不一致再补采样到这个上限，按执行结果多数决。每题多花 1 次（一致时）到 N-1 次模型调用
    sql_candidates: int = 1
    sql_vote_temperature: float = 0.7
    # 业务字典 / few-shot 例句（jsonl，选填；路径不存在则自动跳过）。
    # 保持默认值时跟随数据集：Olist 库自动改用 eval/knowledge/olist/ 下的口径
    glossary_path: str = "eval/knowledge/glossary.jsonl"
    examples_path: str = "eval/knowledge/examples.jsonl"
    knowledge_top_n: int = 3

    # 可选向量检索（任何 OpenAI 兼容 embeddings 接口；不配置则纯 BM25）
    embed_api_key: str = ""
    embed_base_url: str = ""
    embed_model: str = ""

    # 回答防幻觉数字校验
    answer_verify: bool = True

    # 表级权限（选填）：逗号分隔的可见表名，配置后 Agent 只能看见/查询这些表
    # （schema 注入、守卫白名单、/api/schema、MCP 工具同步过滤）；空 = 全部可见。
    # 多角色部署按角色起实例配不同值——权限硬边界仍应放在数据库只读账号的 GRANT
    allowed_tables: str = ""

    # 图表沙箱：docker（生产）/ subprocess（开发兜底）/ auto（有 docker 用 docker）
    chart_executor: str = "auto"
    chart_image: str = "deepquery-chart"
    chart_timeout_seconds: float = 20
    chart_out_dir: str = "data/charts"

    # 服务
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    # 前端联调 CORS：逗号分隔的允许来源（如 http://localhost:5173）；留空则关闭
    cors_allow_origins: str = ""
    # 演示部署访问口令：配置后提问与记忆读写需携带口令（POST /api/ask 放请求体，其余走 ?code=；空=关闭）；
    # 同时前端会给每个浏览器分配独立的访客 ID，记忆按访客隔离
    demo_access_code: str = ""
    # 公网演示的费用防线（0 = 关闭）：单个访客每分钟最多提问/写记忆次数；
    # 全站每日模型花费上限（与 LLM_PRICE_* 同币种），超出后只返回已缓存的答案
    rate_limit_per_minute: int = 0
    daily_cost_limit: float = 0.0
    # 全站同时运行的提问数上限，超出时提示稍后再试（小内存服务器上防止并发拖垮服务）
    max_concurrent_runs: int = 4
    # 网页空状态展示的数据说明（告诉访客这份数据是什么）；为空时演示库自动使用内置说明
    dataset_note: str = ""
    # 部署在 nginx 等反向代理之后时开启：用 X-Real-IP 区分访客。
    # 只有应用端口不对外暴露时才安全，否则访客可以伪造这个请求头
    trust_proxy_headers: bool = False
    # Vue 前端构建产物目录：存在则托管为主页（内置单文件页移至 /legacy）
    web_dist: str = "web/dist"
    # 结果缓存：redis://host:6379/0；不配置则用进程内 LRU
    redis_url: str = ""
    cache_ttl_seconds: int = 600
    # 压测/演示用 mock 模式：不调真实 LLM（回答固定套路 SQL），绝不能用于评测
    llm_mock: bool = False

    # 跨会话记忆
    memory_db_path: str = "data/memory.sqlite"
    # 运行记录与用户反馈（审计 + 差评导出成评测用例）；留空则不记录
    run_log_path: str = "data/runs.sqlite"
    run_log_keep: int = 20000  # 最多保留多少条运行记录，超出后删最旧的

    # SQL 守卫
    sql_timeout_seconds: float = 15
    sql_max_rows: int = 200

    # 数据库
    db_path: str = "data/demo/ecommerce.sqlite"

    # Langfuse 追踪（选填；两个 key 都配置才启用，还需 `uv sync --extra trace`）
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
