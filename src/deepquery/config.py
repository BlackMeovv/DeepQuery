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

    # Schema RAG：on=强制启用 / off=全量 schema / auto=表数超过 top_k 才启用
    schema_rag: str = "auto"
    schema_rag_top_k: int = 6
    # auto 模式启用检索的阈值：全量 schema 字符数超过它才检索选表。
    # BIRD 150 题消融：装得下时全量直供与检索选表的配对差异不显著，而检索只省 ~3% token、
    # 多一个召回失败点——所以按体积而非表数决定
    schema_rag_auto_max_chars: int = 16000
    # 业务字典 / few-shot 例句（jsonl，选填；路径不存在则自动跳过）
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
    # 演示部署访问口令：配置后 /api/ask 与记忆读写需携带 ?code=（空=关闭）；
    # 同时前端会给每个浏览器分配独立的访客 ID，记忆按访客隔离
    demo_access_code: str = ""
    # 公网演示的费用防线（0 = 关闭）：单个访客每分钟最多提问/写记忆次数；
    # 全站每日模型花费上限（与 LLM_PRICE_* 同币种），超出后只返回已缓存的答案
    rate_limit_per_minute: int = 0
    daily_cost_limit: float = 0.0
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
