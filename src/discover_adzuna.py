"""Adzuna DISCOVERY parse — landscape sketch (read-only on saved raw).

Reads the immutable Adzuna raw envelopes from ``data/raw/adzuna/``, flattens
posting titles into a table, buckets them into coarse **role families**, dedups,
counts, and renders a bar chart of the landscape. **Never fetches** — a fix here
re-runs this script against SAVED raw, never re-hits the API (see ``CLAUDE.md``).

What this is NOT
----------------
- NOT the frozen-schema LLM extraction (``extract.py`` →
  ``data/processed/postings.parquet``). This writes its own discovery CSVs.
- NOT the clusters. ``classify_family`` is a coarse, deterministic keyword
  heuristic that maps titles onto the schema's ``role_category`` buckets to get
  a first read of the market. The real role taxonomy comes from extraction +
  clustering, and **naming clusters stays the human's job** (``CLAUDE.md``).

Salary and description are intentionally not used *here* — this pass is just the
role-count landscape. They are pulled and kept in raw, and consumed later by
``extract.py`` (salary is Adzuna-estimated, so treated as approximate).
"""

import hashlib
import json
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: write a PNG, never open a window
import matplotlib.pyplot as plt
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_ADZUNA_DIR = REPO_ROOT / "data" / "raw" / "adzuna"
PROCESSED_DIR = REPO_ROOT / "data" / "processed"

# Ordered, most-specific-first keyword rules. Each maps to a bucket of the
# schema's ``role_category`` enum. Bilingual: Adzuna PL carries EN and PL titles.
# Order matters — the first family whose pattern matches wins.
# Every ingest QUERY_TERM resolves to a specific family here, so a posting whose
# title *is* one of the search terms is never left in 'other'/'unknown'.
FAMILY_RULES = [
    ("analytics_engineer", r"analytics engineer|inżynier analityk"),
    ("ml_engineer", r"\bml\b|machine learning|deep learning|mlops|uczeni\w* maszynow"),
    ("data_scientist", r"data scien|scientist|data science|nauk\w* o danych|naukowiec danych"),
    ("data_engineer",
     r"data engineer|inżynier\w* danych|inzynier\w* danych|\betl\b|big data"
     r"|data warehouse|hurtowni\w* danych"),
    ("bi_developer", r"\bbi\b|business intelligence|power\s*bi|tableau|qlik|looker"),
    ("data_architect", r"data architect|architekt danych"),
    ("data_analyst",
     r"data analyst|data analytics|analiza danych|analityk\w* danych|analyst|analityk"),
]
# Catch-alls applied after the rules above:
#   - "other"   : title mentions data/dane but matched no specific family
#   - "unknown" : no data signal at all
_DATA_HINT = re.compile(r"\bdata\b|\bdane\b|\bdanych\b", re.IGNORECASE)


def classify_family(title):
    """Map a raw title to one coarse role family (a ``role_category`` bucket).

    Deterministic keyword heuristic, most-specific-first. Returns ``other`` when
    the title is data-related but unspecific, ``unknown`` when there's no data
    signal at all. This is a discovery sketch, not the extraction or clusters.
    """
    t = (title or "").lower()
    for family, pattern in FAMILY_RULES:
        if re.search(pattern, t):
            return family
    if _DATA_HINT.search(t):
        return "other"
    return "unknown"


def load_raw():
    """Yield per-posting records from saved Adzuna raw envelopes. Read-only.

    Each record is tagged with its originating ``raw_file``, query ``term``, and
    ``pulled_at`` for traceability. Never fetches.
    """
    files = sorted(RAW_ADZUNA_DIR.glob("*.json"))
    if not files:
        raise SystemExit(
            f"No raw Adzuna files in {RAW_ADZUNA_DIR}. Run `python src/ingest.py` first."
        )
    for path in files:
        envelope = json.loads(path.read_text())
        term = envelope.get("term")
        pulled_at = envelope.get("pulled_at")
        raw_file = str(path.relative_to(REPO_ROOT))
        for result in envelope.get("payload", {}).get("results", []):
            yield result, term, pulled_at, raw_file


def _posting_id(result):
    """Schema-aligned dedup key: ``adzuna:{id}``, else a hash of the URL."""
    sid = result.get("id")
    if sid:
        return f"adzuna:{sid}", str(sid)
    url = result.get("redirect_url", "")
    return f"adzuna:url:{hashlib.sha1(url.encode()).hexdigest()[:12]}", None


def build_table():
    """Flatten raw results into a discovery DataFrame (one row per source result)."""
    rows = []
    for result, term, pulled_at, raw_file in load_raw():
        posting_id, source_id = _posting_id(result)
        rows.append(
            {
                "posting_id": posting_id,
                "source_id": source_id,
                "title": result.get("title", ""),
                "company": (result.get("company") or {}).get("display_name"),
                "location": (result.get("location") or {}).get("display_name"),
                "category": (result.get("category") or {}).get("label"),
                "url": result.get("redirect_url"),
                "term": term,
                "raw_file": raw_file,
                "pulled_at": pulled_at,
            }
        )
    return pd.DataFrame(rows)


def term_market_size():
    """Per-term total match count, straight from Adzuna's reported ``count``.

    Each search response carries ``count`` = total postings matching that
    ``what`` query (consistent across the term's pages). This is the raw
    market-size signal — overlapping across terms and not deduped, so it reads as
    "how much is out there for this search", complementing the deduped, title-
    classified family counts. Returns a DataFrame sorted by count descending.
    """
    sizes = {}
    for path in sorted(RAW_ADZUNA_DIR.glob("*.json")):
        env = json.loads(path.read_text())
        term = env.get("term")
        count = env.get("payload", {}).get("count")
        if term is not None and count is not None:
            sizes[term] = max(sizes.get(term, 0), int(count))
    return (
        pd.DataFrame(sorted(sizes.items(), key=lambda kv: -kv[1]),
                     columns=["term", "reported_count"])
    )


def dedup(df):
    """Drop duplicate postings BEFORE any counting (same posting recurs/term).

    Dedup on ``posting_id`` (the URL-hash fallback is already folded in). Records
    ``dup_count`` = how many source rows collapsed into each kept row — a
    cross-query popularity signal, not a reason to double-count.
    """
    dup_counts = df.groupby("posting_id").size().rename("dup_count")
    deduped = df.drop_duplicates(subset="posting_id").merge(
        dup_counts, on="posting_id"
    )
    return deduped.reset_index(drop=True)


def plot_landscape(counts, total, out_path):
    """Horizontal bar chart of postings per role family, sorted by count."""
    ordered = counts.sort_values("count")  # ascending -> largest on top in barh
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.barh(ordered["family"], ordered["count"], color="#4C72B0")
    for y, (c, pct) in enumerate(zip(ordered["count"], ordered["pct"])):
        ax.text(c, y, f"  {c} ({pct:.0f}%)", va="center", fontsize=9)
    ax.set_xlabel("Unique postings (deduped)")
    ax.set_title(
        f"PL data-role landscape (Adzuna discovery)\n"
        f"{total} unique postings — coarse keyword families, not clusters"
    )
    ax.margins(x=0.12)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def main():
    """Run discovery: load raw → flatten → dedup → classify → count → write + plot."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    df = build_table()
    print(f"Loaded {len(df)} raw result rows from {RAW_ADZUNA_DIR}.")

    deduped = dedup(df)
    deduped["family"] = deduped["title"].map(classify_family)
    total = len(deduped)
    print(f"After dedup: {total} unique postings ({len(df) - total} duplicates dropped).")

    counts = (
        deduped["family"]
        .value_counts()
        .rename_axis("family")
        .reset_index(name="count")
    )
    counts["pct"] = (100 * counts["count"] / total).round(1)

    market = term_market_size()

    titles_path = PROCESSED_DIR / "adzuna_titles.csv"
    counts_path = PROCESSED_DIR / "adzuna_family_counts.csv"
    market_path = PROCESSED_DIR / "adzuna_term_market_size.csv"
    chart_path = PROCESSED_DIR / "adzuna_landscape.png"

    deduped.to_csv(titles_path, index=False)
    counts.to_csv(counts_path, index=False)
    market.to_csv(market_path, index=False)
    plot_landscape(counts, total, chart_path)

    print("\nMarket size by search term (Adzuna reported count — overlapping, not deduped):")
    print(market.to_string(index=False))

    print("\nRole-family landscape (deduped, classified from titles):")
    print(counts.to_string(index=False))

    # Recall/precision audit hint: scan these two buckets in adzuna_titles.csv.
    #   unknown -> no data signal in title (possible noise, or a missed role)
    #   other   -> data-related but unspecific (a family rule may be missing)
    n_unknown = int((deduped["family"] == "unknown").sum())
    n_other = int((deduped["family"] == "other").sum())
    print(
        f"\nAudit: {n_unknown} 'unknown' + {n_other} 'other'. Scan those rows in "
        f"adzuna_titles.csv —\n  'unknown' for real data roles slipped through, "
        f"'other' for a missing FAMILY_RULES pattern."
    )
    print(f"\nWrote:\n  {titles_path}\n  {counts_path}\n  {market_path}\n  {chart_path}")


if __name__ == "__main__":
    main()
