.PHONY: install demo-db olist-db olist-set olist-gold olist-eval behavior-gold behavior-eval analysis-eval test smoke smoke-gold ask schema bird-prepare spider-prepare bird report

install:            ## 安装依赖（含 dev）
	uv sync --extra dev

demo-db:            ## 生成确定性演示库
	uv run python -m deepquery.demo_data

olist-db:           ## 下载 Olist 巴西电商真实数据并导入 data/olist/olist.sqlite（约 45MB）
	uv run python -m deepquery.datasets build olist

test:               ## 离线测试（不需要 API Key）
	uv run pytest

smoke-gold:         ## 离线自检评测基建：gold 回放必须 100%
	uv run python -m deepquery.evalkit.runner --cases eval/cases/smoke.jsonl --gold-replay

check-api:          ## API 体检：连通/延迟/usage 回传/SQL 围栏遵循（需要 .env，花费忽略不计）
	uv run deepquery check-api

smoke:              ## 真实 LLM 跑冒烟评测（需要 .env）
	uv run python -m deepquery.evalkit.runner --cases eval/cases/smoke.jsonl

ask:                ## 提问：make ask Q="上海的客户一共有多少个？"
	uv run deepquery ask "$(Q)" --trace

schema:             ## 查看喂给模型的 schema 上下文
	uv run deepquery schema

bird-prepare:       ## 转换 BIRD dev 子集：make bird-prepare ROOT=~/data/bird_dev
	uv run python -m deepquery.evalkit.prepare bird $(ROOT) --out eval/cases/bird-dev.jsonl --limit 150

spider-prepare:     ## 转换 Spider dev 子集：make spider-prepare ROOT=~/data/spider
	uv run python -m deepquery.evalkit.prepare spider $(ROOT) --out eval/cases/spider-dev.jsonl --limit 150

bird:               ## BIRD 跑分：make bird ROOT=~/data/bird_dev LABEL=baseline
	uv run python -m deepquery.evalkit.runner --cases eval/cases/bird-dev.jsonl --db-root $(ROOT) --repeats 3 --label $(LABEL)

report:             ## 消融对比表：make report FILES="eval/results/a.json eval/results/b.json"
	uv run python -m deepquery.evalkit.report $(FILES) --out eval/results/report.md

business-set:       ## 重新生成自建业务评测集（dev/holdout）
	uv run python -m deepquery.evalkit.business_set

business:           ## 业务集跑分：make business LABEL=baseline
	uv run python -m deepquery.evalkit.runner --cases eval/cases/business-dev.jsonl --repeats 3 --label $(LABEL)

olist-set:          ## 重新生成 Olist 真实数据评测集（dev/holdout，需先 make olist-db）
	uv run python -m deepquery.evalkit.olist_set

olist-gold:         ## Olist 评测集自检：gold 回放必须 100%（不需要 API Key）
	uv run python -m deepquery.evalkit.runner --cases eval/cases/olist-dev.jsonl --db data/olist/olist.sqlite --gold-replay

olist-eval:         ## Olist 真实数据跑分：make olist-eval LABEL=baseline（需要 .env 里的模型 API）
	uv run python -m deepquery.evalkit.runner --cases eval/cases/olist-dev.jsonl --db data/olist/olist.sqlite --repeats 3 --label $(or $(LABEL),olist-baseline)

behavior-gold:      ## 行为评测集自检：标准 SQL 与追问的上一轮 SQL 都能跑通（不需要 API Key）
	uv run python -m deepquery.evalkit.behavior --db data/olist/olist.sqlite --gold-check

behavior-eval:      ## 行为评测：该问的问、该直接回答的回答、该查的查，追问的准确率：make behavior-eval LABEL=baseline
	uv run python -m deepquery.evalkit.behavior --db data/olist/olist.sqlite --label $(or $(LABEL),baseline)

analysis-eval:      ## 分析模式评测：结论能否逐句核对出处、各步成功率、耗时花费：make analysis-eval LABEL=baseline
	uv run python -m deepquery.evalkit.behavior --db data/olist/olist.sqlite --analysis --label $(or $(LABEL),baseline)

serve:              ## 启动服务（网页 http://localhost:8000）
	uv run deepquery serve

db-dumps:           ## 生成 MySQL/PG 演示库初始化脚本（配合 docker-compose.dbs.yml）
	uv run python -m deepquery.demo_data --dump mysql > docker/dbs/mysql/10-data.sql
	uv run python -m deepquery.demo_data --dump postgres > docker/dbs/postgres/10-data.sql
	@echo "已生成 docker/dbs/{mysql,postgres}/10-data.sql"

web-dev:            ## 前端开发服务器（需先 cd web && npm install）
	cd web && npm run dev

web-build:          ## 构建前端（产物自动被后端托管为主页）
	cd web && npm run build
