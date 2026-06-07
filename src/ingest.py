"""Stage 1 — INGEST (fetch-only).

Hits Adzuna, saves the immutable raw response to ``data/raw/adzuna/``, and does
**nothing else**. This module never parses, normalizes, or reads raw back — that
is ``extract.py``'s (and, for the discovery pass, ``discover_adzuna.py``'s) job.
Keeping pull and parse separate means a parsing bug is fixed by re-running the
parser, never by re-hitting the API.

Contract (see ``CLAUDE.md``):
- **Adzuna is the only source (v1).** Its job postings carry the full text we
  analyze — title, description, salary, company, location. Salary is often
  Adzuna-*estimated*, so pay is treated as approximate downstream.
- ``data/raw/`` is **write-once**. ``save_raw`` refuses to overwrite an existing
  file; if raw exists, use it — never re-pull to "refresh". This also makes a
  big pull resumable: re-running skips saved pages and continues.

This pulls the **full** landscape: every role term, all pages, so the analysis
runs on everything (``discover_adzuna.py`` then counts roles into families).
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# --- paths -----------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw"

# --- Adzuna config ---------------------------------------------------------
ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"
COUNTRY = "pl"
RESULTS_PER_PAGE = 50  # Adzuna max
REQUEST_DELAY = 1.0  # seconds between calls — be polite to the rate limit
MAX_PAGES = 50  # safety ceiling per term; the count-based stop ends most terms
# far sooner. 50 * 50 = up to 2500 postings/term, more than any PL data term has.

# Data-role vocabulary (EN + PL), roles only. Each term is one Adzuna `what`
# keyword query, paginated to exhaustion; results are deduped + bucketed
# downstream by discover_adzuna.py. A vocabulary net (not a bare "data" query)
# so adjacent roles without the word "data" — ML / BI / analytics engineer — are
# still caught. Edit freely BEFORE the pull: raw is write-once, so adding a term
# later pulls only that new term.
QUERY_TERMS = [
    # core data roles (EN)
    "data analyst",
    "data engineer",
    "data scientist",
    "data architect",
    "data analytics",
    "data science",
    "big data",
    "data warehouse",
    # ML / AI
    "machine learning",
    "ml engineer",
    "deep learning",
    "mlops",
    # BI / analytics / pipelines
    "analytics engineer",
    "business intelligence",
    "bi developer",
    "etl developer",
    # core data roles (PL)
    "analityk danych",
    "inżynier danych",
    "inżynieria danych",
    "naukowiec danych",
    "analiza danych",
    "uczenie maszynowe",
]


def _slug(term: str) -> str:
    """Filesystem-safe slug for a query term (``data analyst`` -> ``data-analyst``)."""
    return re.sub(r"[^a-z0-9]+", "-", term.lower()).strip("-")


def fetch_adzuna(term, page, app_id, app_key):
    """Query one Adzuna search page and return its raw JSON body.

    Returns the parsed response dict for ``save_raw`` — does **not** parse or
    normalize. Raises on a non-200 status; retries once on HTTP 429.
    """
    url = f"{ADZUNA_BASE}/{COUNTRY}/search/{page}"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": RESULTS_PER_PAGE,
        "what": term,
        "content-type": "application/json",
    }
    for attempt in (1, 2):
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 429 and attempt == 1:
            # Rate limited — back off once, then retry.
            time.sleep(5 * REQUEST_DELAY)
            continue
        if resp.status_code != 200:
            raise RuntimeError(
                f"Adzuna {resp.status_code} for what='{term}' page={page}: "
                f"{resp.text[:300]}"
            )
        return resp.json()


def save_raw(payload, source, term, page, pulled_at):
    """Write a raw payload to ``data/raw/`` as immutable JSON. **Write-once.**

    Wraps the source response in a provenance envelope
    (``source``/``term``/``page``/``endpoint``/``pulled_at``/``payload``) so the
    parse stage gets the schema's ``pulled_at`` and ``raw_file`` traceability.
    The filename is deterministic (no timestamp) so a re-run is a no-op: if the
    file already exists this **skips and returns** without overwriting.

    Returns the ``Path`` written, or ``None`` if it already existed (skipped).
    """
    out_dir = RAW_DIR / source
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{_slug(term)}_p{page}.json"

    if out_path.exists():
        return None  # write-once: never overwrite an existing raw file

    envelope = {
        "source": source,
        "term": term,
        "page": page,
        "endpoint": f"{ADZUNA_BASE}/{COUNTRY}/search/{page}",
        "pulled_at": pulled_at,
        "payload": payload,
    }
    out_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2))
    return out_path


def pull_adzuna():
    """Pull the full Adzuna landscape: every term, all pages, saved as raw.

    Loops ``QUERY_TERMS``; for each, fetches pages until the reported ``count``
    is exhausted (or the ``MAX_PAGES`` safety ceiling), saving each page's raw
    envelope and sleeping ``REQUEST_DELAY`` between calls. No parsing here.

    Resumable: already-saved pages are skipped, and if Adzuna's daily/rate cap is
    hit the pull stops cleanly with everything saved so far — re-run to continue.
    """
    load_dotenv(REPO_ROOT / ".env")
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise SystemExit(
            "Missing ADZUNA_APP_ID / ADZUNA_APP_KEY. Copy .env.example to .env "
            "and fill in your Adzuna credentials."
        )

    for term in QUERY_TERMS:
        fetched = skipped = 0
        count = None
        for page in range(1, MAX_PAGES + 1):
            pulled_at = datetime.now(timezone.utc).isoformat()
            out_path = RAW_DIR / "adzuna" / f"{_slug(term)}_p{page}.json"

            # If raw already exists, skip the network call entirely (write-once).
            if out_path.exists():
                skipped += 1
                continue

            try:
                payload = fetch_adzuna(term, page, app_id, app_key)
            except RuntimeError as err:
                # Likely rate-limited or daily cap reached. Stop cleanly — every
                # page saved so far is kept (write-once), so just re-run later to
                # resume from here; already-saved pages are skipped.
                print(f"\n  stopped on '{term}' page {page}: {err}")
                print("  → already-saved raw kept; re-run `python src/ingest.py` to resume.")
                return
            save_raw(payload, "adzuna", term, page, pulled_at)
            fetched += 1
            time.sleep(REQUEST_DELAY)

            # Stop paginating once we've covered all reported results.
            count = payload.get("count", 0)
            results = payload.get("results", [])
            if not results or page * RESULTS_PER_PAGE >= count:
                break

        total = "?" if count is None else count
        print(
            f"  {term:<22} fetched={fetched} skipped(existing)={skipped} "
            f"reported_count={total}"
        )


def main():
    """Pull the full Adzuna landscape (fetch-only, write-once raw)."""
    print("Ingest — Adzuna full pull (fetch-only, write-once raw):")
    pull_adzuna()


if __name__ == "__main__":
    main()
