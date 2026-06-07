"""Stage 2 — EXTRACT (parse-only).

Reads the immutable raw payloads from ``data/raw/``, builds the **frozen**
clean-table schema (``docs/schema.md``), and writes the canonical Parquet table
to ``data/processed/postings.parquet``.

This stage never fetches. A parsing or prompt bug is fixed by re-running this
module against SAVED raw — never by re-pulling a source (see ``CLAUDE.md``).

Hybrid extractor:
- **Deterministic** for every field the Adzuna JSON already carries structured:
  ids, url, dates, title, company, location, salary, contract. Free + fully
  reproducible.
- **LLM (Claude Haiku 4.5, Anthropic Batch API, fixed-JSON-schema structured
  output)** for ``skills`` + ``seniority`` from the 500-char description — the
  only fields that need a model. Run **only on data-family rows** (the role
  families + the ``other`` data-titled bucket); ``unknown`` rows get every
  deterministic field but ``skills = null`` ("extraction did not run").

Key rules carried from the schema:
- Salary: Adzuna pay is **employer-stated** (``salary_is_predicted='0'`` →
  ``salary_is_estimated=false``) and **annual PLN**; normalize period → monthly
  (÷12), currency stays PLN. Flag gross/net and UoP/B2B as ``unknown`` (Adzuna
  gives neither), never convert. Implausibly low values are flagged as outliers,
  not divided.
- Dedup on ``posting_id`` (fallback ``url``) before any extraction/counting.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

# Reuse the discovery stage's raw loader + identity/classification helpers —
# same saved raw, same dedup key, same coarse role buckets (see CLAUDE.md: one
# source of truth). discover_adzuna.py lives alongside this file in src/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_adzuna import (  # noqa: E402
    classify_family,
    _posting_id,
    load_raw as load_raw_results,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROCESSED_DIR = REPO_ROOT / "data" / "processed"
PARQUET_PATH = PROCESSED_DIR / "postings.parquet"
SKILLS_CACHE_PATH = PROCESSED_DIR / "skills_cache.jsonl"

# --- LLM config ------------------------------------------------------------
LLM_MODEL = "claude-haiku-4-5"
# Families the LLM skills pass runs on: every coarse bucket except 'unknown'
# (non-data jobs whose description merely mentions a search term). 'other' is
# data-titled, so it's in scope.
LLM_FAMILIES = {
    "data_analyst", "data_engineer", "data_scientist", "ml_engineer",
    "analytics_engineer", "bi_developer", "data_architect", "other",
}

# Fixed extraction instructions — identical across every request so the Batch
# API can prompt-cache this prefix. Keep BELOW any per-posting text.
SKILLS_SYSTEM = (
    "You extract structured data from Polish job-posting snippets for data roles.\n"
    "From the TITLE and DESCRIPTION, return JSON with two fields:\n"
    "- skills: a list of concrete technical skills/tools explicitly named or "
    "clearly required (e.g. sql, python, spark, airflow, dbt, power bi, snowflake, "
    "aws, gcp, tableau, kafka, pytorch). Lowercase. Canonicalise obvious variants "
    "(e.g. 'PySpark' -> 'pyspark', 'PowerBI'/'Power BI' -> 'power bi'). Only skills "
    "actually present in the text — never invent. Empty list if none are stated.\n"
    "- seniority: one of junior, mid, senior, lead, unknown — inferred from the "
    "title/description. Use unknown when not stated.\n"
    "The description is truncated to ~500 characters; extract from what is present."
)

# Structured-output JSON schema (additionalProperties:false; no string-length
# constraints — both required for the API's json_schema format).
SKILLS_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "skills": {"type": "array", "items": {"type": "string"}},
            "seniority": {
                "type": "string",
                "enum": ["junior", "mid", "senior", "lead", "unknown"],
            },
        },
        "required": ["skills", "seniority"],
        "additionalProperties": False,
    },
}

# Salaries below this annual PLN floor are implausible (~5% of salaried rows go
# as low as single digits) — flagged as outliers, not divided by 12. Documented,
# human-editable (see docs/schema.md → Adzuna annual-basis rule).
SALARY_ANNUAL_FLOOR = 12_000


# --- deterministic field extraction ---------------------------------------

def _seniority_from_text(title, description):
    """Coarse seniority from title/description keywords (EN + PL). Default unknown.

    A cheap baseline for every row; the LLM may overwrite it for data-family
    rows. Order matters — most-senior label wins.
    """
    t = f"{title or ''} {description or ''}".lower()
    if any(k in t for k in ("lead", "principal", "staff", "head of", "kierownik")):
        return "lead"
    if any(k in t for k in ("senior", "sr.", "sr ", "starszy", "ekspert", "expert")):
        return "senior"
    if any(k in t for k in ("junior", "jr.", "jr ", "młodszy", "mlodszy",
                            "intern", "staż", "staz", "trainee", "praktyk")):
        return "junior"
    if any(k in t for k in ("mid", "regular", "medior", "specjalista")):
        return "mid"
    return "unknown"


def normalize_role(title, description):
    """Map a posting's title/content to ``role_category`` and ``seniority``.

    Produces coarse pre-cluster buckets only — the clusters themselves are run
    later and named by the human (CLAUDE.md). ``role_category`` reuses the
    discovery heuristic; ``seniority`` is a keyword baseline.
    """
    return {
        "role_category": classify_family(title),
        "seniority": _seniority_from_text(title, description),
    }


def normalize_salary(salary_min, salary_max, salary_is_predicted):
    """Normalize salary onto the lossless axes and set the basis flags.

    Adzuna PL pay is employer-stated and annual PLN: period → monthly (÷12),
    currency stays PLN (fx 1.0). **Never** converts across gross/net or UoP/B2B
    (Adzuna gives neither → both ``unknown``). Implausibly low values are flagged
    as outliers (period ``unknown``, monthly left null), not divided.
    """
    present = salary_min is not None
    # salary_is_estimated derived from the raw flag, never hardcoded (schema).
    is_estimated = str(salary_is_predicted) == "1"
    out = {
        "salary_present": present,
        "salary_min_pln_month": None,
        "salary_max_pln_month": None,
        "salary_midpoint_pln_month": None,
        "salary_basis_gross_net": "unknown",
        "salary_basis_contract": "unknown",
        "salary_currency_raw": "PLN" if present else None,
        "salary_period_raw": None,
        "salary_fx_rate": 1.0 if present else None,
        "salary_is_estimated": is_estimated,
        "salary_raw": None,
    }
    if not present:
        return out

    out["salary_raw"] = json.dumps({"min": salary_min, "max": salary_max})
    lo = float(salary_min)
    hi = float(salary_max) if salary_max is not None else lo
    if lo < SALARY_ANNUAL_FLOOR:
        # Implausible as an annual salary — flag, don't normalize.
        out["salary_period_raw"] = "unknown"
        return out
    out["salary_period_raw"] = "year"
    out["salary_min_pln_month"] = round(lo / 12, 2)
    out["salary_max_pln_month"] = round(hi / 12, 2)
    out["salary_midpoint_pln_month"] = round((lo + hi) / 2 / 12, 2)
    return out


def _city(location):
    """Normalized primary locality from Adzuna's area list (country is area[0])."""
    area = (location or {}).get("area") or []
    return area[-1] if len(area) > 1 else None


def _posted_at(created):
    """Parse Adzuna's ``created`` ISO timestamp to a date, or None."""
    if not created:
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def load_raw():
    """Read raw Adzuna results into one deterministic record per source result.

    Reads only — never fetches. Delegates to ``discover_adzuna.load_raw`` for the
    envelope walk, then assembles the schema's deterministic fields. The LLM-only
    fields (skills/seniority overwrite) are filled later.
    """
    records = []
    for result, term, pulled_at, raw_file in load_raw_results():
        posting_id, source_id = _posting_id(result)
        title = result.get("title", "")
        description = result.get("description")
        company = (result.get("company") or {}).get("display_name")
        location = result.get("location") or {}
        contract_time = result.get("contract_time")

        rec = {
            # provenance / identity
            "posting_id": posting_id,
            "source": "adzuna",
            "source_id": source_id,
            "url": result.get("redirect_url"),
            "raw_file": raw_file,
            "pulled_at": pulled_at,
            "posted_at": _posted_at(result.get("created")),
            # role
            "title": title,
            **normalize_role(title, description),
            # company / location
            "company": company,
            "location_raw": location.get("display_name"),
            "city": _city(location),
            "remote_mode": "unknown",  # Adzuna API has no explicit remote field
            # contract / employment
            "employment_types": None,  # Adzuna gives no b2b/uop basis
            "work_time": contract_time if contract_time in ("full_time", "part_time") else "unknown",
            # salary
            **normalize_salary(result.get("salary_min"), result.get("salary_max"),
                               result.get("salary_is_predicted")),
            # skills / extraction content (LLM pass fills these for data rows)
            "skills": None,
            "skills_raw": None,
            "description_text": description,
            # extraction metadata
            "extracted": False,
            "extraction_ok": False,
            "extraction_model": None,
            "extraction_run_id": None,
        }
        records.append(rec)
    return records


def dedup(df):
    """Drop duplicate postings BEFORE any extraction/counting.

    Dedup on ``posting_id`` (URL-hash fallback already folded in). ``dup_count``
    = source rows collapsed into each kept row — a cross-query popularity signal,
    not a reason to double-count.
    """
    dup_counts = df.groupby("posting_id").size().rename("dup_count")
    deduped = df.drop_duplicates(subset="posting_id").merge(dup_counts, on="posting_id")
    return deduped.reset_index(drop=True)


# --- LLM skills pass (Batch API, structured output, resumable cache) -------

def _load_skills_cache():
    """Load already-extracted postings from the resumable JSONL cache."""
    cache = {}
    if SKILLS_CACHE_PATH.exists():
        for line in SKILLS_CACHE_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            cache[row["posting_id"]] = row
    return cache


def _append_skills_cache(rows):
    """Append extraction results to the JSONL cache (write-once per posting)."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    with SKILLS_CACHE_PATH.open("a") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _cid(posting_id):
    """Batch custom_id (alphanumerics/_/- only, ≤64 chars) from a posting_id."""
    return posting_id.replace(":", "_")[:64]


def extract_posting(df, limit=None):
    """Extract ``skills`` + ``seniority`` for data-family rows via the Batch API.

    Uses Anthropic structured output (fixed JSON schema) on the title + 500-char
    description, prompt-caching the shared instructions. Resumable: results are
    cached per ``posting_id`` in ``skills_cache.jsonl`` and re-runs only submit
    postings not already cached. Returns a dict ``posting_id -> result row``
    (cached + newly extracted).

    ``limit`` caps how many *new* postings to submit (smoke testing). Requires
    ``ANTHROPIC_API_KEY``; callers guard on that.
    """
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    cache = _load_skills_cache()
    in_scope = df[df["role_category"].isin(LLM_FAMILIES)]
    todo = in_scope[~in_scope["posting_id"].isin(cache.keys())]
    if limit is not None:
        todo = todo.head(limit)

    print(f"  skills pass: {len(in_scope)} data-family rows, "
          f"{len(cache)} cached, {len(todo)} to extract.")
    if todo.empty:
        return cache

    client = anthropic.Anthropic()
    cid_to_pid = {}
    requests = []
    for row in todo.itertuples(index=False):
        cid = _cid(row.posting_id)
        cid_to_pid[cid] = row.posting_id
        user_text = f"TITLE: {row.title}\n\nDESCRIPTION: {row.description_text or ''}"
        requests.append(Request(
            custom_id=cid,
            params=MessageCreateParamsNonStreaming(
                model=LLM_MODEL,
                max_tokens=256,
                system=[{"type": "text", "text": SKILLS_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                output_config={"format": SKILLS_FORMAT},
                messages=[{"role": "user", "content": user_text}],
            ),
        ))

    batch = client.messages.batches.create(requests=requests)
    print(f"  submitted batch {batch.id} ({len(requests)} requests); polling…")
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        time.sleep(15)

    new_rows = []
    for result in client.messages.batches.results(batch.id):
        posting_id = cid_to_pid.get(result.custom_id)
        if posting_id is None:
            continue
        if result.result.type == "succeeded":
            msg = result.result.message
            text = next((b.text for b in msg.content if b.type == "text"), "")
            try:
                data = json.loads(text)
                skills = [str(s).strip().lower() for s in data.get("skills", []) if str(s).strip()]
                new_rows.append({
                    "posting_id": posting_id,
                    "skills": skills,
                    "seniority": data.get("seniority", "unknown"),
                    "extraction_ok": True,
                    "extraction_model": LLM_MODEL,
                    "extraction_run_id": batch.id,
                })
                continue
            except (json.JSONDecodeError, AttributeError):
                pass
        # errored / canceled / expired / unparseable — record the attempt
        new_rows.append({
            "posting_id": posting_id, "skills": [], "seniority": "unknown",
            "extraction_ok": False, "extraction_model": LLM_MODEL,
            "extraction_run_id": batch.id,
        })

    _append_skills_cache(new_rows)
    ok = sum(1 for r in new_rows if r["extraction_ok"])
    print(f"  extracted {len(new_rows)} ({ok} ok); cache now {len(cache) + len(new_rows)}.")
    for r in new_rows:
        cache[r["posting_id"]] = r
    return cache


def _apply_skills(df, cache):
    """Merge cached LLM results into the deduped frame (skills/seniority/metadata)."""
    for i, posting_id in df["posting_id"].items():
        row = cache.get(posting_id)
        if not row:
            continue
        df.at[i, "skills"] = row["skills"]
        df.at[i, "skills_raw"] = df.at[i, "description_text"]
        df.at[i, "seniority"] = row.get("seniority") or df.at[i, "seniority"]
        df.at[i, "extracted"] = True
        df.at[i, "extraction_ok"] = bool(row.get("extraction_ok"))
        df.at[i, "extraction_model"] = row.get("extraction_model")
        df.at[i, "extraction_run_id"] = row.get("extraction_run_id")
    return df


# --- assemble + write ------------------------------------------------------

# (column, dtype) — frozen schema order (docs/schema.md). list/date columns stay
# as object so PyArrow stores native list[str] / date32; the rest are cast.
_DTYPES = {
    "posting_id": "string", "source": "category", "source_id": "string",
    "url": "string", "raw_file": "string",
    "pulled_at": "datetime64[ns, UTC]", "dup_count": "Int64",
    "title": "string", "role_category": "category", "seniority": "category",
    "company": "string", "location_raw": "string", "city": "category",
    "remote_mode": "category", "work_time": "category",
    "salary_present": "bool",
    "salary_min_pln_month": "Float64", "salary_max_pln_month": "Float64",
    "salary_midpoint_pln_month": "Float64",
    "salary_basis_gross_net": "category", "salary_basis_contract": "category",
    "salary_currency_raw": "category", "salary_period_raw": "category",
    "salary_fx_rate": "Float64", "salary_is_estimated": "bool",
    "salary_raw": "string",
    "skills_raw": "string", "description_text": "string",
    "extracted": "bool", "extraction_ok": "bool",
    "extraction_model": "string", "extraction_run_id": "string",
}
_COLUMN_ORDER = [
    "posting_id", "source", "source_id", "url", "raw_file", "pulled_at",
    "posted_at", "dup_count", "title", "role_category", "seniority", "company",
    "location_raw", "city", "remote_mode", "employment_types", "work_time",
    "salary_present", "salary_min_pln_month", "salary_max_pln_month",
    "salary_midpoint_pln_month", "salary_basis_gross_net", "salary_basis_contract",
    "salary_currency_raw", "salary_period_raw", "salary_fx_rate",
    "salary_is_estimated", "salary_raw", "skills", "skills_raw",
    "description_text", "extracted", "extraction_ok", "extraction_model",
    "extraction_run_id",
]


def build_clean_table(df):
    """Cast every column to its frozen dtype and write ``postings.parquet``.

    Enforces the missing-value convention (enums never null, booleans always
    populated, list null = extraction-did-not-run). ``pulled_at`` is parsed to a
    tz-aware UTC datetime; ``posted_at``/list columns stay native for PyArrow.
    """
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    df = df.copy()
    df["pulled_at"] = pd.to_datetime(df["pulled_at"], utc=True)
    for col, dtype in _DTYPES.items():
        df[col] = df[col].astype(dtype)
    df = df[_COLUMN_ORDER]
    df.to_parquet(PARQUET_PATH, index=False)
    return df


def _summary(df):
    n = len(df)
    sal = int(df["salary_present"].sum())
    ext = int(df["extracted"].sum())
    print(f"\nClean table: {n} unique postings")
    print(f"  salary_present: {sal} ({100 * sal / n:.0f}%)")
    print(f"  extracted (skills): {ext} ({100 * ext / n:.0f}%)")
    print("  role_category:")
    for fam, c in df["role_category"].value_counts().items():
        print(f"    {fam:<20} {c}")
    print(f"\nWrote {PARQUET_PATH}")


def main():
    """Run the parse stage: load_raw → dedup → [LLM skills] → write.

    The LLM skills pass is **paid** and OFF by default — building the
    deterministic table never spends money or calls the API. Opt in explicitly
    with ``python src/extract.py skills`` (or ``EXTRACT_RUN_SKILLS=1``). Cached
    results are always merged in if present, so re-running without the flag still
    reflects skills already extracted. ``EXTRACT_LLM_LIMIT=N`` caps new submissions
    (smoke testing).
    """
    load_dotenv(REPO_ROOT / ".env")
    print("Extract — Adzuna parse → frozen-schema Parquet (read-only on saved raw):")

    records = load_raw()
    df = pd.DataFrame(records)
    print(f"Loaded {len(df)} raw result rows.")
    df = dedup(df)
    print(f"After dedup: {len(df)} unique postings.")

    run_skills = "skills" in sys.argv[1:] or os.environ.get("EXTRACT_RUN_SKILLS") == "1"
    if run_skills:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("  'skills' requested but no ANTHROPIC_API_KEY in env/.env.")
        limit = os.environ.get("EXTRACT_LLM_LIMIT")
        cache = extract_posting(df, limit=int(limit) if limit else None)
        df = _apply_skills(df, cache)
    else:
        # Default: deterministic table only. Still merge any already-cached skills
        # so we never re-pay for postings extracted in a prior run.
        cache = _load_skills_cache()
        if cache:
            print(f"  merging {len(cache)} cached skill results "
                  f"(no new API calls; pass 'skills' to extract more).")
            df = _apply_skills(df, cache)
        else:
            print("  skills pass OFF (paid). Run `python src/extract.py skills` to "
                  "extract skills/seniority for data-family rows; skills=null until then.")

    df = build_clean_table(df)
    _summary(df)


if __name__ == "__main__":
    main()
