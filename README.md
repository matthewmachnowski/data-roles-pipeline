# PL Data Roles Pipeline

Mapping the Polish **data-role job market** to choose a specialization niche —
ending in a verified dataset, a landscape analysis, and a one-page decision.

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

Three stages, each re-running from the previous stage's **saved** output — never
from scratch. Pull is split from parse, so a parsing fix never re-hits the API,
and `data/raw/` is **write-once** (the immutable source of truth).

```
   Adzuna API
       │   src/ingest.py             fetch only · write-once · resumable
       ▼
   data/raw/*.json                   immutable source of truth
       │   src/discover_adzuna.py    dedup → bucket titles into role families
       ├──▶ data/processed/adzuna_*.csv  +  adzuna_landscape.png
       │
       │   src/extract.py (upcoming) LLM: skills + salary → frozen schema
       ▼
   data/processed/postings.parquet → analysis → one-page decision
```

## Source

**Adzuna only (v1).** One source carries the whole project — title, description,
salary, company, location. A manual spot-check found Adzuna descriptions ~1:1
with the original board offers, so they're rich enough for skill extraction; the
PL boards (justjoin.it, No Fluff Jobs) are out of v1. Adzuna salaries are often
estimates — treated as approximate.

## Schema

`docs/schema.md` — the frozen clean-table field schema (one row = one unique
posting; Parquet canonical store).

## Status

Phase 1 — Adzuna landscape **done**. Full pull of 22 data-role search terms
(385 raw pages → ~10.8k unique postings after dedup). `src/discover_adzuna.py`
buckets every title into a role family; all 22 search terms map to a specific
family. Most un-bucketed volume is `unknown` — non-data jobs whose *descriptions*
(not titles) merely mention a search term, the expected cost of broad keyword
search. Next: `src/extract.py` (skills + salary into the schema).

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # add your Adzuna app_id / app_key
.venv/bin/python src/ingest.py           # pull raw (write-once, resumable)
.venv/bin/python src/discover_adzuna.py  # landscape: family counts + chart
```
