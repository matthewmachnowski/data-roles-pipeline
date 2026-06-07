# PL Data Roles Pipeline

Mapping the Polish **data-role job market** to choose a specialization niche —
ending in a verified dataset, a reproducible analysis, and the market readout
below.

## Goal & question

**Goal:** decide which data-role specialization to focus on, backed by real
market data instead of guesswork.

**Question:** across the Polish market, which data role is the best bet — judged
on three lenses?

| lens | what it answers |
|------|-----------------|
| **Demand** | How many openings exist per role family? |
| **Skills** | What tools and skills do those postings actually require? |
| **Pay** | What salary bands are advertised? |

Roles in scope: *data analyst · data engineer · data scientist · ML engineer ·
analytics engineer · BI developer · data architect*.

## Pipeline

Each stage re-runs from the previous stage's **saved** output — never from
scratch. Pull is split from parse, so a parsing fix never re-hits the API, and
`data/raw/` is **write-once** (the immutable source of truth).

```
   Adzuna API
       │   src/ingest.py             fetch only · write-once · resumable
       ▼
   data/raw/*.json                   immutable source of truth
       │   src/discover_adzuna.py    dedup → bucket titles into role families
       ├──▶ data/processed/adzuna_*.csv  +  adzuna_landscape.png
       │
       │   src/extract.py            deterministic fields + LLM skills/seniority
       ▼                             (Claude Haiku 4.5, Batch API) → frozen schema
   data/processed/postings.parquet
       │   notebooks/analyze_postings.ipynb   demand · pay · skills · clusters
       ▼
   findings (below)
```

## Source

**Adzuna only (v1).** One source carries the whole project — title, description,
salary, company, location. A manual spot-check found Adzuna descriptions ~1:1
with the original board offers, so they're rich enough for skill extraction; the
PL boards (justjoin.it, No Fluff Jobs) are out of v1. Adzuna salaries are
**employer-stated** (raw `salary_is_predicted='0'` on every posting), reported as
annual PLN and normalized to monthly, present on ~32% of postings.

## Schema

`docs/schema.md` — the frozen clean-table field schema (one row = one unique
posting; Parquet canonical store).

## Results (v1)

From **10,846 unique postings** (after dedup). **4,919** classify into a data
role family; the other 5,927 are `unknown` — non-data jobs whose *descriptions*
merely mention a search term (the expected cost of broad keyword search) and are
excluded from the per-role figures below. Skills/seniority are extracted by an
LLM (Claude Haiku 4.5) on each posting's description.

**Demand & pay by role** (pay = median monthly PLN on salary-present rows within
a 4k–80k plausibility band):

| role family | postings (demand) | median PLN/mo (n) | confidence |
|---|---:|---:|---|
| **data engineer** | **1,647** | 23,945 (636) | high |
| data analyst | 1,109 | 20,160 (315) | high — lowest pay |
| ML engineer | 556 | 24,150 (215) | medium |
| data scientist | 349 | 23,500 (113) | medium |
| BI developer | 234 | 21,835 (88) | medium |
| data architect | 164 | **32,500** (83) | senior-only, tiny demand |
| analytics engineer | 106 | 24,000 (18) | low — small n |
| _other (mixed data-titled)_ | _754_ | _26,460 (227)_ | _not one role_ |

Overall salary midpoint median ≈ **22,500 PLN/mo**.

**Top skills** (alias-merged, across data-family postings): python (905), sql
(838), databricks (499), azure (457), aws (453), gcp (427), power bi (321),
spark (288), snowflake (237), airflow (226). The high-frequency stack is
data-engineering shaped — cloud + Spark/Databricks + orchestration + warehouse —
and unsupervised skill clusters (notebook §6) resolve into cloud-DE (GCP/BigQuery),
streaming-DE (Spark/Kafka), Databricks-DE, cloud-ML (AWS/GCP/Docker), and
BI/analytics groups.

**Seniority** (data-family): senior 35% · mid 29% · unknown 24% · lead 8% ·
junior 4%. **Top locations:** Warszawa, Kraków, Wrocław, Gdańsk, Poznań.

**What the lenses say:** *data engineering* leads demand by a wide margin (~1.5×
analyst, ~3× ML) on the most robust salaried sample, pays in the upper-middle
band, and has the most coherent, transferable skill stack. The only
higher-paying families are either senior-only with negligible demand (data
architect) or low-confidence (analytics engineer); data analyst has high demand
but the lowest pay.

## Caveats

| caveat | impact |
|---|---|
| Skills & seniority are **LLM output, unvalidated** against human labels | treat skills/seniority claims as provisional |
| Salary present on **only ~32%** of postings | pay stats are a partial, possibly biased view (always read coverage) |
| Salary is employer-stated **annual PLN → monthly**; no gross/net or UoP/B2B basis | bands are approximate; cross-basis comparison not possible |
| Adzuna gives no remote flag or contract basis | `remote_mode` and `employment_types` are unknown/empty |
| `role_category` is a **coarse title heuristic** | not the final taxonomy; `other` (754) is a mixed bucket |
| Small samples for `analytics_engineer` (n=18) / `data_architect` (n=83) | low-confidence medians |

## Future plans (v2, if earned)

Additional sources (justjoin.it, No Fluff Jobs) for a richer skills signal · full
job-description text via `redirect_url` (Adzuna caps descriptions at ~500 chars)
· an independent human-labeled verification set to validate LLM extraction ·
time-series tracking of demand/pay · dashboards / orchestration.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # add Adzuna app_id/app_key; ANTHROPIC_API_KEY for skills
.venv/bin/python src/ingest.py           # pull raw (write-once, resumable)
.venv/bin/python src/discover_adzuna.py  # landscape: family counts + chart
.venv/bin/python src/extract.py          # deterministic frozen schema → postings.parquet
.venv/bin/python src/extract.py skills   # (paid, opt-in) LLM skills/seniority pass
.venv/bin/jupyter notebook notebooks/analyze_postings.ipynb  # analysis
```

The LLM skills pass is **off by default** — `python src/extract.py` builds the
deterministic table only; add `skills` to run the (billable) Anthropic Batch
extraction, which is cached in `data/processed/skills_cache.jsonl` and resumable.
