"""Stage 1 — INGEST (fetch-only).

Hits each source, saves the immutable raw response to ``data/raw/``, and does
**nothing else**. This module never parses, normalizes, or reads raw back —
that is ``extract.py``'s job. Keeping pull and parse separate means a parsing
bug is fixed by re-running ``extract.py``, never by re-hitting an API.

Contract (see ``CLAUDE.md``):
- ``data/raw/`` is **write-once**. ``save_raw`` must refuse to overwrite an
  existing file; a hook also enforces this. If raw exists, use it — never
  re-pull to "refresh".
- Adzuna = breadth / counts / taxonomy (its pay is estimated). The PL boards
  (justjoin.it, No Fluff Jobs) carry the rich skill detail.

No fetching logic is implemented yet — these are stubs.
"""


def fetch_adzuna(*args, **kwargs):
    """Query the Adzuna API and return its raw responses.

    Used for breadth: counts, role taxonomy, and (estimated) salary bands.
    Returns the raw payload(s) for ``save_raw`` — does not parse.
    """
    ...


def fetch_justjoin(*args, **kwargs):
    """Fetch raw justjoin.it postings.

    Source for rich skill extraction and the deep dive. Returns raw payload(s)
    for ``save_raw`` — does not parse.
    """
    ...


def fetch_nofluffjobs(*args, **kwargs):
    """Fetch raw No Fluff Jobs postings.

    Source for rich skill extraction and the deep dive. Returns raw payload(s)
    for ``save_raw`` — does not parse.
    """
    ...


def save_raw(payload, source, *args, **kwargs):
    """Write a raw payload to ``data/raw/`` as immutable JSON.

    Write-once: must refuse to overwrite an existing raw file. The filename
    should encode source and pull identity so re-running ingest is a no-op
    when raw already exists.
    """
    ...


def main():
    """Orchestrate the pulls: fetch each source, then ``save_raw``.

    No parsing happens here — this stage only fetches and persists raw.
    """
    ...


if __name__ == "__main__":
    main()
