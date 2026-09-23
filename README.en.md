# DeepQuery

[![CI](https://github.com/BlackMeovv/DeepQuery/actions/workflows/ci.yml/badge.svg)](https://github.com/BlackMeovv/DeepQuery/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11-blue)
[中文](README.md)

**Ask your database in plain language and get answers you can trace and verify.**

Most ad-hoc data questions in a company never make it onto a dashboard; they sit in a
ticket queue waiting for an analyst to write SQL. DeepQuery turns "pick tables → write
SQL → run it → fix errors → summarize" into an agent, and every answer expands to the
exact SQL it ran and the raw result.

![Conversation and run inspector](docs/assets/ui-run.png)

## Design

Getting a model to write SQL is easy; getting people to trust the result is not.
DeepQuery keeps reliability in deterministic code rather than in the model:

- **The model proposes, code decides.** SQL is checked on its syntax tree (single SELECT,
  table allowlist, forced row limit) and executed read-only; repair rounds and the
  token/cost budget are hard-capped.
- **Every number in an answer must have a source.** Numbers must appear in the result,
  the question, or the SQL (with rounding-aware matching for thousands separators,
  percents and CJK units). Otherwise the model rewrites once, then only the raw table is shown.
- **Changes are justified by evaluation.** Every change is compared item-by-item on the
  same questions with confidence intervals, and ablations that contradict an assumption
  are written down and acted on.

## Results

Model: gpt-5.5 via an OpenAI-compatible API. Raw per-question results live in
[eval/results/](eval/results/); `make report` recomputes this table from them.

| Set | Config | EX (95% CI) |
|---|---|---|
| Business dev · 165 × 3 runs | baseline | 93.3% [90.1, 95.6] |
| Business dev · 165 × 3 runs | glossary injection + output rules | **97.8%** [95.5, 98.9] |
| Business holdout · 71 × 3 runs | same (never run during tuning) | **97.7%** [92.3, 99.3] |
| BIRD dev · fixed 150-question subset | full schema in context | 64.7% [56.7, 71.9] |
| BIRD dev · fixed 150-question subset | retrieved tables only | 61.3% [53.3, 68.8] |

- **The fix is a real improvement.** Paired by question: +4.4 points, 95% CI [+1.7, +7.2];
  23 questions improved, 7 regressed, sign test p = 0.005.
- **Table retrieval showed no measurable benefit.** Paired difference on BIRD: +3.3 points,
  95% CI [−2.1, +8.7], p = 0.33. Retrieval saves ~3% of tokens but adds a recall failure
  point (95.5% table recall), so the full schema is used unless it exceeds a size threshold.
- **How the intervals are computed.** Three runs of the same question are correlated, not
  495 independent trials; intervals use an effective sample size from a per-question design
  effect (1.52 on dev, 2.21 on holdout).

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and any OpenAI-compatible model API.

```bash
make install
cp .env.example .env          # set LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
make demo-db                  # deterministic e-commerce demo database
make ask Q="Which 5 customers placed the most orders?"
make serve                    # web UI at http://localhost:8000
make test                     # 280+ offline tests, no API key needed
```

Point `--db` at any SQLite file or a `mysql://` / `postgres://` DSN; use a read-only
database account in production. Server deployment: [docs/DEPLOY.md](docs/DEPLOY.md).

## Limitations

- The business set is generated from templates: 236 questions reduce to 32 SQL structures,
  and 69 of 71 holdout questions share a structure with dev. The holdout shows results do
  not depend on specific parameter values, not that they generalize to new question types.
- BIRD numbers are from a 150-question subset, a single run and a single model; they are
  not comparable to the official leaderboard.
- Single-turn only: follow-ups like "what about last month?" are treated as new questions.
- Access control is demo-grade (site-wide access code + table allowlist), not multi-tenant auth.
