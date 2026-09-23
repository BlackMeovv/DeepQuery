# deepquery 服务镜像
# 基础镜像与 npm 源可通过构建参数替换（国内服务器拉不到 ghcr.io / npm 官方源时用，见 .env.example）
ARG NODE_IMAGE=node:20-alpine
ARG UV_IMAGE=ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# 阶段一：构建 Vue 前端（web/dist 不入库，镜像内自行构建）
FROM ${NODE_IMAGE} AS webbuild
ARG NPM_REGISTRY=
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi && npm ci
COPY web/ ./
RUN npm run build

# 阶段二：Python 服务
FROM ${UV_IMAGE}

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# 先装依赖层（利用缓存），再拷代码
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra cache

COPY . .
COPY --from=webbuild /web/dist ./web/dist
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra cache && \
    # matplotlib 供容器内 subprocess 图表执行（容器本身即隔离边界）
    uv pip install matplotlib

EXPOSE 8000
# 启动前：数据目录只允许 root 访问（图表子进程换成无权限 uid 后读不到记忆库等数据）；
# 确保 DB_PATH 指向的库存在：演示库现场生成，Olist 首次启动时从 Kaggle 下载并导入
CMD ["sh", "-c", "mkdir -p data && chmod 700 data; uv run python -m deepquery.datasets ensure; uv run deepquery serve"]
