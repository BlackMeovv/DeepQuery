# 公开基准接入：BIRD / Spider

评测数字的可信度来自公开基准——别人可以下载同样的数据、跑同样的子集来复现你的结果。
本项目用 **执行准确率（EX）** 作为主指标，与 BIRD/Spider 官方口径一致（比结果集，不比 SQL 文本）。

## 1. 下载数据（在你自己的机器上）

**BIRD dev**（推荐主基准，更接近真实业务库）：
- 官网 https://bird-bench.github.io/ → 下载 dev set（`dev.zip`，约 1-2 GB，含 `dev.json` 与 `dev_databases/`）
- 解压到任意目录，如 `~/data/bird_dev/`

**Spider 1.0 dev**（补充基准，题目更"教科书"）：
- 官网 https://yale-lily.github.io/spider → 下载 spider.zip（含 `dev.json` 与 `database/`）
- 解压到如 `~/data/spider/`

## 2. 转换为评测子集（固定 seed，可复现）

```bash
make bird-prepare ROOT=~/data/bird_dev      # 抽 150 条，gold 逐条执行校验，坏 gold 剔除
make spider-prepare ROOT=~/data/spider
```

生成 `eval/cases/bird-dev.jsonl`：每条带 `db` 字段（相对 ROOT 的库路径）。
**子集文件要提交进 git**——这是"评测集固定化"的一部分，别人拿到仓库就能复现同一子集。

## 3. 跑分

```bash
# baseline（3 次重复，汇总为 Wilson 95% 置信区间）
make bird ROOT=~/data/bird_dev LABEL=baseline

# 每做一个优化跑一次，label 换成配置名
make bird ROOT=~/data/bird_dev LABEL=schema-rag
```

表结构的三种给法（环境变量切换，其余配置不动，结果按题配对比较）：

```bash
SCHEMA_RAG=off make bird ROOT=~/data/bird_dev LABEL=full-schema
SCHEMA_RAG=on make bird ROOT=~/data/bird_dev LABEL=retrieval
SCHEMA_RAG=disclose make bird ROOT=~/data/bird_dev LABEL=disclose
```

渐进式披露多一次模型调用（选表），报告里的成本、延迟列会反映出来；选表召回率照常统计
（按实际展开的表计算）。表目录的体积可以用 `uv run deepquery schema --catalog --db <库文件>` 直接看。

修复时自动查过滤列真实取值的效果，同样用开关做对照：

```bash
REPAIR_VALUE_PROBE=false make bird ROOT=~/data/bird_dev LABEL=no-value-probe
```

多候选投票（自洽性）：先多采样一条 SQL，两条执行结果一致就采用，不一致再补采样，按执行结果多数决。
报告里每题的 `vote` 字段记录候选数和一致票数，成本列反映多出来的调用：

```bash
SQL_CANDIDATES=3 make bird ROOT=~/data/bird_dev LABEL=vote3
```

成本参考：150 条 × 3 次重复 ≈ 450 次调用；按 DeepSeek 价格每次全流程约 0.002-0.01 元，
一轮全量约 1-5 元。日常改动跑 `make smoke`（20 条演示库冒烟集）即可，全量留给里程碑。

## 4. 汇总消融表

```bash
make report FILES="eval/results/<基线>.json eval/results/<改动后>.json"
```

输出 markdown 表：每行一个配置，EX 带置信区间 + 设计效应 + 成本 + 延迟；
并对前两个配置做按题配对比较（平均差区间 + 符号检验），回答"这次提升是真的还是抖动"。

统计口径：

- **区间按题目聚类校正。** `--repeats 3` 时同一题的三次结果高度相关，不能当成 3 倍的
  独立样本。先估计设计效应 deff（题目成功率的实测方差 / 独立假设下的二项方差），
  用有效样本量 = 总试验数 / deff 计算 Wilson 区间。repeats=1 时退化为普通 Wilson 区间。
- **配置对比按题配对。** 逐题取两个配置的成功率之差，报告平均差的 95% 区间，并对
  "变好/变差"的题数做精确符号检验（repeats=1 时即精确 McNemar）。不先把每题的多次
  结果按多数票压成对错——那会丢掉部分改进和部分退步。

## 约定

- 调 prompt / 检索只看 dev 子集；最终对外报告的数字用另抽的 held-out 子集复核（换个 seed 再 prepare 一份，标记为 holdout，平时绝不跑）。
- 报告 JSON 全部留档在 `eval/results/` 并纳入版本库：README 与复盘里的每个数字都能从这里复算。

# 真实数据评测集：Olist

自建业务集跑在程序生成的演示库上，数据干净、分布均匀。Olist 评测集用的是真实订单数据
（约 10 万订单，有缺失值、订单级客户 ID、没有品类的商品等真实数据的毛病），考察同一套 Agent 在真实库上的表现。

- 138 条中文问题：按州 / 品类 / 月份 / 支付方式 / 评分的参数化模板，加上 41 条手写的多表关联和口径类问题
- 口径与 `eval/knowledge/olist/glossary.jsonl` 一致：销售额排除已取消订单、客户数按 `unique_id` 去重、延迟送达按承诺日期判断
- 每条 gold 都在真实库上执行校验；TOP-N 题在截断处并列的会被剔除（并列时取哪个都对，无法唯一判分）
- 固定 seed 切成 dev 96 条 / holdout 42 条，holdout 只用于最终复核
- 生成器：`src/deepquery/evalkit/olist_set.py`；题目：`eval/cases/olist-{dev,holdout}.jsonl`

```bash
make olist-db
make olist-gold
make olist-eval LABEL=baseline
```

依次是：下载导入数据、离线自检（gold 回放必须 100%，不需要 API Key）、用真实模型跑 3 次。
评测时通过 `--db data/olist/olist.sqlite` 指定库，业务口径会自动换成 Olist 的那一份。

# 行为评测与分析评测

执行准确率只回答"SQL 写得对不对"。在线问答里同样重要的是"该不该写 SQL"：
有歧义时先问、数据里没有时说明缺什么、问口径时直接回答、寒暄时直接回应、追问时接上上一轮。
这些行为靠提示词和编排层的规则实现，也需要单独测。

**行为评测**（`eval/cases/olist-behavior.jsonl`，53 题，生成器 `src/deepquery/evalkit/behavior_set.py`）：

| 类别 | 题数 | 期望 | 例子 |
|---|---|---|---|
| data | 8 | 直接查（其中 6 题有标准 SQL） | 2018 年 1 月有多少笔订单？ |
| clarify | 8 | 先问口径 | 哪个卖家最好？ |
| missing | 8 | 说明缺什么数据 | 各品类的退货率是多少？ |
| meta | 9 | 不查数据直接回答 | 销售额是怎么算的？ |
| chat | 6 | 直接回应 | 你是谁？ |
| followup | 14 | 带着上一轮追问（12 题有标准 SQL，2 题追问口径） | 那只看东南部的呢？ |

报告的指标：路径准确率；反问的精确率（问了的里面该问的）、召回率（该问的里面问了的）、
"不该问却问了"的比例；有标准 SQL 的题的执行准确率。所有比例都带 95% Wilson 区间——53 题的样本不大，区间会比较宽，
比较两个版本时看区间是否明显分开，不要只看点估计。

**分析评测**（`eval/cases/olist-analysis.jsonl`，6 题）：分析题没有唯一的标准答案，只统计能自动判定的部分——
给出结论的比例、结论通过逐句溯源（没有被拦下改为列结果）的比例、各步查询成功率、平均步数、
平均核对数字个数、模型调用次数、耗时和花费。结论写得好不好，需要人读报告文件里的 answer 判断。

```bash
make behavior-gold
make behavior-eval LABEL=baseline
make analysis-eval LABEL=baseline
```

依次是：离线检查标准 SQL（不需要 API Key）、跑行为评测、跑分析评测。报告写入 `eval/results/behavior-*.json`
和 `eval/results/analysis-*.json`。
