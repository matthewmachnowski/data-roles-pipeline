# Clean-table field schema (FROZEN)

> ⚠️ **This schema is FROZEN.** Changing any column — name, type, or meaning —
> means re-running the entire pipeline from `data/raw/`. Do not change it
> silently; flag it loudly. See `CLAUDE.md` → "Rules that don't change".

**Grain:** one row = **one unique posting** (after dedup).
**Store:** canonical format is **Parquet** (`data/processed/postings.parquet`),
so list fields stay native `list[str]` and dtypes are exact. Types below are
given as pandas / PyArrow dtypes.

---

## Missing-value convention

| field kind | when present | when absent |
|---|---|---|
| **Enum / categorical** | one of the listed values | the literal `"unknown"` — **never null** |
| **Free string / text** | the value | null (`pd.NA`) |
| **Numeric** (`Int64`/`Float64`) | the value | null; paired with a `_present` boolean where the absence is analytically meaningful (salary) |
| **Boolean flag** | `true` / `false` | always populated — **never null** |
| **List** (`list[str]`) | the list | `[]` if extraction ran but found nothing; **null** if extraction did not run |

---

## Dedup

Primary key = `posting_id`. Dedup on the source's stable id (`source_id`); fall
back to the canonical `url` when no stable id exists. **Drop duplicates before
any counting** (the same posting appears under multiple queries). `dup_count`
records how many source rows collapsed into each kept row — a cross-query
popularity signal, not a reason to double-count.

---

## Columns

### Provenance / identity

| column | dtype | null | notes |
|---|---|---|---|
| `posting_id` | string | no | dedup PK: `"{source}:{source_id}"`, or a URL hash if the source has no stable id |
| `source` | category {adzuna, justjoin, nofluffjobs} | no | which source the row came from |
| `source_id` | string | yes | native id as given by the source |
| `url` | string | no | canonical posting URL; dedup fallback key |
| `raw_file` | string | no | relative path to the immutable raw file this row derived from (traceability) |
| `pulled_at` | datetime (UTC, tz-aware) | no | when the raw payload was fetched |
| `posted_at` | date | yes | publication date, if the source provides it |
| `dup_count` | Int64 | no | source rows collapsed into this row (default `1`) |

### Role

| column | dtype | null | notes |
|---|---|---|---|
| `title` | string | no | raw posted title |
| `role_category` | category | no (default `unknown`) | coarse normalized bucket: {data_analyst, data_engineer, data_scientist, ml_engineer, analytics_engineer, bi_developer, data_architect, other, unknown}. **A coarse pre-cluster label, NOT the clusters** — clusters are run later and named by the human (CLAUDE.md). Extracted. |
| `seniority` | category {junior, mid, senior, lead, unknown} | no (default `unknown`) | extracted |

### Company / location

| column | dtype | null | notes |
|---|---|---|---|
| `company` | string | yes | employer name |
| `location_raw` | string | yes | location text as posted |
| `city` | category | yes | normalized primary city (e.g. `Warszawa`, `Kraków`, `remote`, `many`) |
| `remote_mode` | category {remote, hybrid, onsite, unknown} | no (default `unknown`) | work-location mode |

### Contract / employment

| column | dtype | null | notes |
|---|---|---|---|
| `employment_types` | list[str] | yes | ALL contract types offered, e.g. `['b2b','uop']` |
| `work_time` | category {full_time, part_time, unknown} | no (default `unknown`) | full- vs part-time |

### Salary — standardize axis, flag basis (no lossy conversion)

Only the **lossless axes** are normalized: period → monthly, currency → PLN.
The **lossy axes** (gross/net, UoP/B2B) are **flagged, never converted** in
stored data — converting them requires assumptions that distort the numbers.
Any such conversion happens explicitly and visibly at analysis time, never here.

| column | dtype | null | notes |
|---|---|---|---|
| `salary_present` | bool | no | was any salary disclosed |
| `salary_min_pln_month` | Float64 | yes | lower bound, normalized to monthly PLN |
| `salary_max_pln_month` | Float64 | yes | upper bound, normalized to monthly PLN |
| `salary_midpoint_pln_month` | Float64 | yes | convenience midpoint (single value if no range) |
| `salary_basis_gross_net` | category {gross, net, unknown} | no (default `unknown`) | **flagged, not converted** |
| `salary_basis_contract` | category {uop, b2b, mandate, unknown} | no (default `unknown`) | which contract the stored range refers to — **flagged, not converted** |
| `salary_currency_raw` | category {PLN, EUR, USD, …} | yes | original currency before normalization |
| `salary_period_raw` | category {month, year, hour, unknown} | yes | original period before normalization |
| `salary_fx_rate` | Float64 | yes | PLN-per-unit rate applied if currency was normalized (else null / `1.0`) |
| `salary_is_estimated` | bool | no | `true` for Adzuna estimates, else `false` |
| `salary_raw` | string | yes | original salary text/struct, for audit |

> **Multiple-range rule (documented, human-editable):** when a posting lists
> separate B2B and UoP ranges, store the **B2B** range and set
> `salary_basis_contract='b2b'`; fall back to UoP otherwise. All offered
> contracts are still captured in `employment_types`. This is a deliberate,
> visible choice — flag it before flipping it.

### Skills / extraction content

| column | dtype | null | notes |
|---|---|---|---|
| `skills` | list[str] | yes | normalized skill tokens (lowercased). Rich from PL boards; sparse/empty for Adzuna. `null` if extraction did not run, `[]` if it ran but found none |
| `skills_raw` | string | yes | original tech/requirements block fed to extraction |
| `description_text` | string | yes | cleaned full JD text (extraction input + audit) |

### Extraction metadata (reproducibility + independent eval)

| column | dtype | null | notes |
|---|---|---|---|
| `extracted` | bool | no | did LLM extraction run on this row |
| `extraction_ok` | bool | no | did structured output return schema-conformant JSON |
| `extraction_model` | string | yes | model id used |
| `extraction_run_id` | string | yes | ties the row to a run/log |

---

## Notes & caveats

- **Salary bias:** postings that disclose pay are not representative of all
  postings. Always report `salary_present` coverage alongside any pay figure;
  never drop rows to make the numbers look clean.
- **Source roles:** Adzuna is breadth / counts / taxonomy and its pay is
  *estimated* (`salary_is_estimated = true`) — treat its bands as approximate.
  The PL boards (justjoin.it, No Fluff Jobs) carry rich skills and the deep
  dive. `source` + `salary_is_estimated` let analysis honor this split.
- **Lossless vs lossy:** `salary_*_pln_month` reflect only period+currency
  normalization. To compare across gross/net or UoP/B2B, filter on
  `salary_basis_*` or apply a conversion explicitly downstream — the stored
  number is never silently cross-converted.
