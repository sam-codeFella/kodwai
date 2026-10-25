# Plan: Idempotent multi-partner ETL with schema drift

## Context

Three partners drop one CSV of order events per day. Their schemas differ from each other and drift over time (renames, type changes, added columns). Files can be re-delivered, and one file a week lands a day late. We must land everything into one canonical `orders` table that analytics can trust, with these properties:

- **Idempotent**: re-running a date never double-counts.
- **Loud on structural breakage, quiet on benign drift**: missing required column = failure; extra column = tolerated.
- **Bad rows are quarantined, not dropped**: `rejects` table keeps the original row and the reason.
- **Observable**: per-run, per-partner metrics.
- **Scales and extends**: a 5GB file must not OOM, and a 4th partner must not require touching core code.

The repo is greenfield (only `PROBLEM.md`, `package.json`, `.gitignore`). Python 3.10 (so no `tomllib`; stdlib only, no new deps). Assessment has a 90-minute limit, so scope stays tight.

## How I read the problem

This is a **canonical-model ingestion problem**: N messy sources, 1 clean contract. The architecture follows from that:

```
partner CSV ──► [reader: stream + header resolution] ──► [normalizer: row → canonical | reject]
                                                                   │                │
                                                              valid batch      reject batch
                                                                   ▼                ▼
                                                        orders (UPSERT)       rejects (replace per file)
                                                                   └──► run + per-partner stats
```

Principle: **core is partner-agnostic; everything partner-specific is data (config), not code.**

## Key design decisions

### 1. Idempotency (the heart of it)
- `orders` PRIMARY KEY is **(partner, order_id)**. `order_id` is only unique within a partner, so the partner is part of the key. This is a stated assumption.
- Load with `INSERT ... ON CONFLICT(partner, order_id) DO UPDATE SET ... WHERE <any business column differs>` (SQLite `IS NOT` compares NULL-safely).
  - Same data again: conflict, `WHERE` false, **zero rows touched**. `ingested_at` and `run_id` stay at their original values, so a re-run is a true no-op.
  - Changed data: the row is updated cleanly, and `ingested_at`/`run_id`/`source_file` advance.
  - This handles both "file arrived twice" and "late file", with no ordering dependence between days.
- `orders` keeps **exactly the 9 columns the spec lists**. Change detection compares the business columns directly rather than adding a hash column.
- **File-level guard** (optimization, not the correctness mechanism): a `file_loads` table records each file's sha256. If identical content was already loaded successfully, even under a different filename (a re-delivered duplicate), skip it fast and record `skipped_duplicate`. Row-level upsert remains the actual guarantee.
- **Atomicity**: each file loads in one transaction (orders upsert + rejects replace + stats). A crash mid-file leaves nothing half-written, and the re-run just redoes it.
- **Rejects idempotency**: inside the same transaction, `DELETE FROM rejects WHERE source_file=?` and then insert the fresh rejects. Rejects always reflect the *current* state of the file and never accumulate duplicates across re-runs. `run_id` on each reject records which run produced it.

### 2. Schema drift handling (config-driven)
`config/partners.json` (JSON because stdlib on 3.10; adding a partner = adding an entry). Per partner:
- `file_pattern`, e.g. `data/inbox/{partner}/{partner}_{date}.csv`
- `fields`: canonical field → list of accepted **aliases**, each alias with a parser tag. For example `customer_email: [email, customerEmail, customer_email]`, and `amount: [{col: "amount", fmt: "decimal_major"}, {col: "amount_cents", fmt: "minor_int"}]`.
- timestamp format and source timezone
- optional `effective_from` overrides, for when a column keeps its **name** but changes **meaning** on a date (e.g. `amount` flips from "42.50" strings to integer minor units on day 5).
- `required` canonical fields.

Resolution happens **once per file from the header**, not per row:
- Any required canonical field with no matching alias → **`SchemaError`**: the file is not touched, the partner's stats row is `failed` with the missing columns, and the process exits non-zero at the end. Other partners still load (failure isolation).
- Unknown extra columns → ignored and listed in the stats row as `extra_columns`, so additive drift stays visible.

Type drift (`"42.50"` / `4250` / `42.5`):
- Parse with `Decimal`, **never float**.
- The unit is never guessed from the value (`"4250"` is ambiguous). It comes from the alias/format/effective-date config.
- Major→minor conversion must land on an integer after applying the currency exponent (a small table: JPY=0, default=2). Fractional cents (e.g. 42.505) → reject `amount_not_integral_minor_units`.

### 3. Row validation and normalization
Each row goes to canonical form or raises `RowReject(reason)`. Reasons are a small fixed set so they can be grouped in analysis: `missing_order_id`, `invalid_email`, `invalid_amount`, `negative_amount`, `invalid_currency`, `invalid_timestamp`, `wrong_column_count`.
- `placed_at` → UTC ISO-8601; the per-partner format/timezone comes from config (ISO-Z, epoch seconds, local `MM/DD/YYYY HH:MM`).
- `currency` → uppercased 3-letter code; `customer_email` → trimmed, lowercased, basic shape check.
- `ingested_at`, `source_file`, `run_id` are stamped by the pipeline, never from the partner.
- Rejects store the original row as JSON (original header names preserved), the reason, `source_file`, `line_no`, and `run_id`.

### 4. Late and missing files
- If no file exists for a partner/date, that is **not an error**. The stats row is `missing`, a warning is logged, and the exit code stays 0 (distinct from `failed`). When the file lands, re-running that date loads it. Idempotency makes this safe and also means a catch-up run can never double-load.
- No cross-day ordering assumptions: because upsert is keyed on (partner, order_id), a late day-N file after day-N+1 has loaded is harmless. Stated assumption: a redelivered `order_id` with different content is a correction, and last write wins.

### 5. Large files (5GB)
- `csv.reader` streamed with `newline=''` and `utf-8-sig` (BOM tolerant). No `list()` or `read()` of the file.
- sha256 computed in 1MB chunks.
- Rows normalized lazily, flushed in batches of ~5k via `executemany`, inside the per-file transaction. Memory is O(batch), not O(file).
- SQLite `PRAGMA journal_mode=WAL; synchronous=NORMAL`.
- Proven by a test that loads a few hundred thousand generated rows and asserts peak traced memory stays bounded.

### 6. Extensibility (4th partner)
Add one JSON entry and one `generate_samples.py` partner profile (the latter only for demo data). **Zero core changes.** For something config can't express, a named parser registry in `parsers.py` (`PARSERS = {"decimal_major": ..., "minor_int": ..., "epoch_s": ...}`) is the one extension seam: add a function, reference it by name in config.

### 7. Metrics
- `pipeline_runs(run_id, run_date, started_at, finished_at, duration_s, status)`
- `partner_run_stats(run_id, partner, source_file, status ∈ {ok, failed, missing, skipped_duplicate}, rows_in, rows_out, rows_rejected, rows_changed, rows_unchanged, extra_columns, error, duration_s)`
  - `rows_out` = valid rows delivered to the load step; `rows_changed` = inserted+updated (via `total_changes` delta); `rows_unchanged` = no-op conflicts. A second run showing `rows_changed=0` makes idempotency visible in the data itself.
- A one-line JSON summary per partner and per run is also printed to stdout.

## Files to create

```
pipeline.py               # thin CLI: argparse (--run-date, --db, --inbox, --config) → etl.runner
generate_samples.py       # deterministic (seeded) generator for 3 partners × 7 days
config/partners.json      # partner registry (mappings, formats, required fields)
etl/__init__.py
etl/config.py             # load + validate partner config
etl/parsers.py            # amount / timestamp / email / currency parsers + PARSERS registry
etl/reader.py             # streaming CSV + header→canonical resolution (SchemaError)
etl/normalize.py          # row → canonical dict | RowReject
etl/db.py                 # DDL, upsert, rejects replace, file_loads, stats
etl/runner.py             # orchestration per run-date, per-partner isolation, exit code
tests/test_*.py           # stdlib unittest (no new deps)
README.md                 # design, assumptions, how to add a partner, how to run the demo
```

`package.json` and `.kodwai/` are harness files and are left untouched.

## Sample data generator (they read this too)

Deterministic (`random.Random(seed)`), writes `data/inbox/<partner>/<partner>_<YYYY-MM-DD>.csv` for 7 days. Three partners with *different* base schemas and formats:
- **acme**: `order_id, email, amount, currency, placed_at` where `amount` is a decimal string and `placed_at` is ISO-Z. **Day 3**: `email` → `customerEmail`. **Day 5**: `amount` becomes integer minor units, with the column renamed to `amount_cents`.
- **bolt**: `OrderId, customer_email, total, ccy, order_ts` where `order_ts` is epoch seconds. **Day 5**: `total` keeps its name but flips from "42.50" to minor units, covered by an `effective_from` rule. **Day 6**: extra column `promo_code`.
- **cobalt**: `id, buyer_email, price, currency, created` where `created` is local `MM/DD/YYYY HH:MM`. **Day 5**: `price` becomes float 42.5. **Day 6**: extra column `channel`.

Injected realism:
- A few bad rows per file: bad email, blank id, `abc` amount, unparseable date, negative amount, ragged row.
- A **duplicate delivery**: identical content re-sent under a second filename.
- A **late file**: one partner's day-4 file is withheld into `data/late/`. A documented copy step "delivers" it later.
- **Cross-day corrections**: a few `order_id`s reappear a day later with a changed amount, to exercise the update path.
- A **broken file** variant (`--with-broken`): a required column missing, to demo the loud failure.

## Tests (unittest, hermetic temp dirs)

1. Run the same date twice: identical `orders` contents, `ingested_at`/`run_id` unchanged, `rows_changed=0` on the 2nd run.
2. Changed source for an already-loaded date: the row updates, no duplicate.
3. Duplicate file under a new name: `skipped_duplicate`, counts unchanged.
4. Late file: run day N with the file missing (`missing`, exit 0), deliver it, re-run, it loads.
5. Missing required column: that partner `failed`, nothing from that file loaded, other partners loaded, exit code non-zero.
6. Extra column tolerated and reported in `extra_columns`.
7. Amount parsing table: `"42.50"`, `4250` (minor), `42.5`, `42.505` (reject), JPY exponent 0.
8. Rejects: original row and reason preserved, no duplicates after a re-run.
9. Streaming/memory: a ~300k-row file with bounded `tracemalloc` peak.
10. Full 7-day demo: exact orders/rejects counts match what the generator reports it injected.

## Verification (end-to-end)

```bash
python3 generate_samples.py                       # creates data/inbox/... for 7 days
for d in 2026-01-01 ... 2026-01-07; do python3 pipeline.py --run-date $d; done
python3 pipeline.py --run-date 2026-01-03         # re-run: expect rows_changed=0
sqlite3 warehouse.db "select partner,count(*) from orders group by 1;"
sqlite3 warehouse.db "select reason,count(*) from rejects group by 1;"
sqlite3 warehouse.db "select * from partner_run_stats order by rowid desc limit 10;"
# late file demo: day-4 for the withheld partner shows 'missing'; copy from data/late/; re-run day 4
python3 -m unittest discover -s tests -v
```

Expected: no duplicate (partner, order_id) rows. Re-runs show `rows_changed=0`. Rejects total matches the injected count. The broken-file demo exits non-zero while the other partners still load.

## Assumptions worth flagging in the README
- (partner, order_id) is the business key; a redelivered key with different content is a correction (last write wins).
- Units are declared in config or by effective date, never inferred from values.
- A missing file is a pending state, not a failure; a malformed header is a failure.
- SQLite chosen (zero setup); the DB access is isolated in `etl/db.py`, so Postgres would be a swap of that one module.

## Implementation notes (deviations from the plan above)

- **Stale-write guard added** to the upsert (`excluded.source_file >= orders.source_file`): file names embed the business date, so a late *older* file cannot overwrite data already loaded from a *newer* one. Closes the "last write wins" hazard for late files without extra columns on `orders`.
- **Byte-identical skip applies only to a different file name.** A re-run of the same file always reprocesses (cheap no-op upsert), so a fixed config can reprocess data a hash-skip would have blocked.
- **`file_glob`** (`{partner}/{partner}_{date}*.csv`) replaces a fixed file name so re-deliveries are discovered.
- **No per-partner `required` list**: all five canonical fields are required.
- **Timezones** are `UTC` or fixed `UTC+HH:MM` (stdlib only, no tz database).
- **`generate_samples.py --deliver-late`** moves the withheld file into the inbox for the late-arrival demo.
- **Staged merge** (added while expanding tests): rows go to a disk-backed TEMP `stage` table and are merged once, keeping the last row per key. A fuzz test showed that a file repeating a key with different content flipped the stored row and its `run_id` on every re-run; staging makes that case a true no-op too.
- **Hardening found by tests**: an unreadable file (e.g. a directory matching the glob) now fails that file instead of crashing the run; a non-object config raises `ConfigError`; ISO timestamps with `+0530`-style offsets are accepted.
