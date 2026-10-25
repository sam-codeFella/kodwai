# Idempotent ETL Pipeline with Schema Drift

# Idempotent ETL Pipeline with Schema Drift
You are the first data engineer at a small analytics startup. Three
partners send you daily CSV exports of order events. Their schemas drift
without warning, files sometimes arrive twice, and once a week one file
arrives a day late. Your job is to land all of it in a clean `orders`
table that the analytics team can query.
You are starting from an empty directory. As part of your work,
**generate the sample CSVs yourself** — three partners, each with one
CSV per day for a week (7 days). Introduce realistic drift between
partners and across days: rename a column on day 3, change a price from
string to integer minor units on day 5, add an extra column on day 6
that earlier days don't have. We will read both your generator and your
pipeline.
## What to build
A pipeline runnable as `python pipeline.py --run-date YYYY-MM-DD` that:
1. Reads each partner's CSV for that date.
2. Normalizes each row to the `orders` table schema:
   `(order_id, partner, customer_email, amount_minor, currency, placed_at, ingested_at, source_file, run_id)`.
3. Loads to the target table (SQLite or Postgres, your choice)
   **idempotently** — re-running for the same date is a no-op (or, if
   the source data changed, updates cleanly).
4. Rejects bad rows to a `rejects` table with the original row + reason
   + source file + run_id.
5. Handles schema drift:
   - Renames (`customer_email` vs `email` vs `customerEmail`) via a
     mapping you control.
   - Type drift (`amount` as `"42.50"` vs `4250` vs `42.5`).
   - Late-arriving columns (additive) are tolerated; missing required
     columns are loud failures.
6. Emits metrics or a status row per run: rows in / out / rejected, per
   partner, plus duration.
## What we care about
- Re-running yesterday's pipeline does not double-count.
- A 5GB file (you don't need to provide one, but design for it) would
  not OOM your process.
- Three months from now, a fourth partner can be added without
  rewriting the core pipeline.
## Out of scope
- A real orchestrator (Airflow / Dagster); a CLI is fine.
- Dashboards.
- Real-time / streaming.

