"""Stage 2 — EXTRACT (parse-only).

Reads the immutable raw payloads from ``data/raw/``, runs Anthropic structured
(fixed-JSON-schema) extraction into the **frozen** clean-table schema
(``docs/schema.md``), normalizes, dedups, and writes the canonical Parquet
table to ``data/processed/postings.parquet``.

This stage never fetches. A parsing or prompt bug is fixed by re-running this
module against SAVED raw — never by re-pulling a source (see ``CLAUDE.md``).

Key rules carried from the schema:
- Salary: normalize only the lossless axes (period → monthly, currency → PLN);
  **flag** gross/net and UoP/B2B, never convert them.
- Dedup on ``posting_id`` (fallback ``url``) before any counting.

No parsing logic is implemented yet — these are stubs.
"""


def load_raw(*args, **kwargs):
    """Read raw files from ``data/raw/`` into in-memory records.

    Reads only — never fetches. Yields/returns per-posting raw records tagged
    with their ``source`` and originating ``raw_file`` for traceability.
    """
    ...


def extract_posting(*args, **kwargs):
    """Extract one raw posting into a frozen-schema dict.

    Uses Anthropic structured output (fixed JSON schema) to pull title, role,
    skills, salary, etc. Records extraction metadata (``extraction_ok``,
    ``extraction_model``, ``extraction_run_id``).
    """
    ...


def normalize_salary(*args, **kwargs):
    """Normalize salary onto the lossless axes and set the basis flags.

    Converts period → monthly and currency → PLN (recording ``salary_fx_rate``),
    and sets ``salary_basis_gross_net`` / ``salary_basis_contract``. **Never**
    converts across gross/net or UoP/B2B. Applies the documented multiple-range
    rule (prefer B2B). Sets ``salary_present``.
    """
    ...


def normalize_role(*args, **kwargs):
    """Map a posting's title/content to ``role_category`` and ``seniority``.

    Produces coarse pre-cluster buckets only — the clusters themselves are run
    later and named by the human.
    """
    ...


def dedup(*args, **kwargs):
    """Drop duplicate postings before any counting.

    Dedup on ``posting_id`` (fallback ``url``); set ``dup_count`` to the number
    of source rows collapsed into each kept row.
    """
    ...


def build_clean_table(*args, **kwargs):
    """Assemble normalized records into the frozen schema and write Parquet.

    Casts every column to its declared dtype (``docs/schema.md``), enforces the
    missing-value convention, and writes ``data/processed/postings.parquet``.
    """
    ...


def main():
    """Run the parse stage: load_raw → extract → normalize → dedup → write."""
    ...


if __name__ == "__main__":
    main()
