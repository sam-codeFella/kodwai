# Idempotent partner-orders ETL

Loads daily CSV order exports from three partners into one canonical `orders` table (SQLite).
Re-running a date never double-counts; schema drift is absorbed by config; bad rows are
quarantined with their reason; every run records metrics.

```bash
python3 generate_samples.py                    # 3 partners x 7 days -> data/inbox (+ data/late, data/manifest.json)
for d in 01 02 03 04 05 06 07; do python3 pipeline.py --run-date 2026-01-$d; done
python3 generate_samples.py --deliver-late     # the withheld cobalt day-4 file finally arrives
python3 pipeline.py --run-date 2026-01-04      # loads it; every other date can be re-run safely too
python3 -m unittest discover -s tests -v       # 18 tests, stdlib only
```

Python 3.10+, standard library only. Exit codes: `0` ok (a missing or duplicate file is *not* a
failure), `1` a partner file failed, `2` bad usage/config. A JSON summary goes to stdout, logs to stderr.

## Pipeline

```
partner CSV -> reader (stream, resolve header) -> normalizer (row -> canonical | reject)
                                                      |                    |
                                                 orders (upsert)     rejects (replace per file)
                                                      +--> pipeline_runs / partner_run_stats
```

| module | job |
|---|---|
| `pipeline.py` | CLI: `--run-date YYYY-MM-DD` (`--db`, `--inbox`, `--config`) |
| `config/partners.json` | the only place partner knowledge lives |
| `etl/config.py` | load + validate the registry |
| `etl/reader.py` | streaming CSV, header -> canonical-field resolution, `SchemaError` |
| `etl/parsers.py` | amount / timestamp / email / currency parsers (`PARSERS` registry) |
| `etl/normalize.py` | one row -> canonical record or `RowReject(reason)` |
| `etl/db.py` | schema + every SQL statement (swap this module for Postgres) |
| `etl/runner.py` | orchestration, per-partner isolation, metrics |
| `generate_samples.py` | seeded sample data with drift, bad rows, a duplicate and a late file |

## How the requirements are met

**Idempotency.** `orders` is keyed on `(partner, order_id)` and loaded with
`INSERT ... ON CONFLICT DO UPDATE ... WHERE <a business column differs>`. Identical data touches
zero rows, so `ingested_at`/`run_id` keep their first-load values: a re-run is a true no-op.
Changed data updates the row in place. Each file loads in one transaction, so a crash leaves
nothing half-written. A byte-identical file re-delivered under a *new name* is detected by sha256
and skipped (`skipped_duplicate`); the upsert is still the real guarantee.
Rejects for a file are deleted and re-inserted inside that same transaction, so they mirror the
file's current state instead of accumulating.

**Late files.** No file for a partner/date is a `missing` status row, not an error. Re-run the date
once it lands. Because loading is keyed per order, days can load in any order. The upsert also has
a stale-write guard (`excluded.source_file >= orders.source_file`): file names embed the business
date, so a late *older* file cannot overwrite data already loaded from a *newer* one.

**Schema drift.** Header names map to canonical fields through `config/partners.json` aliases,
resolved once per file.
- Renames: list every accepted header (`"customer_email": ["email", "customerEmail"]`).
- Type/unit drift: parsing uses `Decimal`, never float. `"42.50"`, `42.5` -> `decimal_major`;
  `4250` -> `minor_int`. Units are never guessed from a value (`4250` is ambiguous), they come from
  the alias. If a partner keeps the *same* column name but changes meaning, scope the alias with
  `effective_from` (see `bolt.total`). Currency exponents (JPY=0, BHD=3) are honoured.
- Additive columns are ignored and listed in `partner_run_stats.extra_columns`.
- A missing required column raises `SchemaError` before any row is read: the file loads nothing,
  its stats row is `failed`, other partners still load, the process exits 1.

**Bad rows.** Rejected to `rejects` with the original row (JSON keyed by the partner's own header
names), `reason`, `source_file`, `line_no`, `run_id`. Reasons: `missing_<field>`, `invalid_email`,
`invalid_amount`, `negative_amount`, `amount_not_integral_minor_units`, `invalid_currency`,
`invalid_timestamp`, `wrong_column_count`.

**Metrics.** `pipeline_runs` (status, duration) and `partner_run_stats` per file: `rows_in`,
`rows_out` (valid), `rows_rejected`, `rows_changed` (inserted or updated), `rows_unchanged`,
`extra_columns`, `error`, `duration_s`. A second run of the same date shows `rows_changed = 0`.

**5GB files.** The CSV is streamed row by row (`csv.reader`, never `list()`/`read()`), hashed in 1MB
chunks, and flushed in 5,000-row batches inside the file transaction, so memory is O(batch), not
O(file). `tests/test_pipeline.py::ScaleTests` loads 150k rows (9MB) with a ~3MB traced peak.

**Adding a fourth partner.** Add an entry to `config/partners.json` (glob, timezone, aliases,
parsers). No core change; `ExtensibilityTests` does exactly this. If a quirk can't be expressed,
add one function to `etl/parsers.py` and register it in `PARSERS`.

## Assumptions
- `(partner, order_id)` is the business key. A redelivered key with different content is a
  correction: last file (by business date) wins.
- All five canonical fields are required. `placed_at` is stored as UTC ISO-8601; naive timestamps
  are read in the partner's configured timezone (`UTC` or a fixed `UTC+HH:MM`).
- Files live at `<inbox>/<partner>/<partner>_<date>*.csv`; the glob also picks up re-deliveries
  such as `acme_2026-01-02_redelivery.csv`.
- Within a single file, `rows_changed` counts every insert/update, so a repeated key inside one
  file with differing content counts twice; the final row is still correct.
- Data volume above SQLite's comfort zone would call for Postgres; only `etl/db.py` would change.
