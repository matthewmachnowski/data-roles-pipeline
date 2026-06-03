# PL Data Roles Pipeline

Mapping the Polish data-role job market to decide a specialization niche.
Output: a verified dataset + analysis + a one-page decision.

## Pipeline

Three stages, each re-running from the previous stage's saved output:

    SOURCE -> data/raw/ (immutable) -> EXTRACT -> data/processed/ -> ANALYZE

- `src/ingest.py` — fetch-only; saves raw source responses to `data/raw/` (write-once).
- `src/extract.py` — parse-only; LLM extraction into the frozen schema, writes `data/processed/postings.parquet`.

## Sources

- **Adzuna** — breadth, counts, taxonomy (salaries are estimates).
- **justjoin.it** & **No Fluff Jobs** — rich skill extraction and the deep dive.

## Docs

- `docs/schema.md` — the frozen clean-table field schema.
- `CLAUDE.md` — project contract and working rules.

## Status

Phase 0 — schema frozen, pipeline entry points stubbed. Not yet pulling data.
