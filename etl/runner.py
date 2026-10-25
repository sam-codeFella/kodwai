"""Orchestration for one run date.

For each configured partner: find that date's file(s), resolve the header, stream rows
through the normalizer in batches, and load each file in a single transaction.
Partners are isolated: one partner's structural failure never blocks the others, but
makes the process exit non-zero.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List

from .config import PartnerConfig, load_config
from .db import Warehouse
from .normalize import normalize_row, raw_row_json
from .parsers import RowReject
from .reader import SchemaError, open_rows, resolve_header, sha256_file

log = logging.getLogger("etl")

DEFAULT_BATCH_SIZE = 5000


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_files(partner: PartnerConfig, inbox: str, run_date: str) -> List[str]:
    pattern = partner.file_glob.replace("{partner}", partner.name).replace("{date}", run_date)
    return sorted(glob.glob(os.path.join(inbox, pattern)))


def _stats(run_id: str, partner: str, source_file, status: str, started: float, **kw) -> dict:
    s = {
        "run_id": run_id, "partner": partner, "source_file": source_file, "status": status,
        "rows_in": 0, "rows_out": 0, "rows_rejected": 0, "rows_changed": 0, "rows_unchanged": 0,
        "extra_columns": None, "error": None,
    }
    s.update(kw)
    s["duration_s"] = round(time.monotonic() - started, 4)
    return s


def process_file(wh: Warehouse, partner: PartnerConfig, path: str, rel: str,
                 run_id: str, run_date: str, ingested_at: str, batch_size: int) -> dict:
    started = time.monotonic()
    sha = sha256_file(path)
    twin = wh.find_identical_file(partner.name, sha, rel)
    if twin:
        log.warning("%s is byte-identical to already-loaded %s; skipping", rel, twin)
        return _stats(run_id, partner.name, rel, "skipped_duplicate", started,
                      error=f"identical content to {twin}")

    rows_in = rows_out = rows_rejected = rows_changed = 0
    extra: List[str] = []
    try:
        with open_rows(path) as (header, rows):
            resolved = resolve_header(header, partner, run_date)  # raises SchemaError pre-load
            extra = resolved.extra_columns
            wh.begin()
            wh.replace_rejects_for(rel)
            good: List[dict] = []
            bad: List[tuple] = []

            def flush_good():
                nonlocal rows_out, rows_changed
                if good:
                    rows_changed += wh.upsert_orders(good)
                    rows_out += len(good)
                    good.clear()

            def flush_bad():
                nonlocal rows_rejected
                if bad:
                    wh.insert_rejects(bad)
                    rows_rejected += len(bad)
                    bad.clear()

            for line_no, values in rows:
                rows_in += 1
                try:
                    rec = normalize_row(values, resolved, partner.timezone)
                except RowReject as r:
                    bad.append((run_id, partner.name, rel, line_no,
                                raw_row_json(header, values), r.reason, ingested_at))
                    if len(bad) >= batch_size:
                        flush_bad()
                    continue
                rec.update(partner=partner.name, ingested_at=ingested_at, source_file=rel, run_id=run_id)
                good.append(rec)
                if len(good) >= batch_size:
                    flush_good()
            flush_good()
            flush_bad()
            wh.record_file_load(rel, partner.name, sha, os.path.getsize(path), run_id, ingested_at)
            wh.commit()
    except SchemaError as e:
        wh.rollback()
        log.error("%s: schema error: %s", rel, e)
        return _stats(run_id, partner.name, rel, "failed", started, error=f"SchemaError: {e}")
    except Exception as e:  # noqa: BLE001 - any failure must roll back and be reported
        wh.rollback()
        log.exception("%s: load failed", rel)
        return _stats(run_id, partner.name, rel, "failed", started, error=f"{type(e).__name__}: {e}")

    return _stats(
        run_id, partner.name, rel, "ok", started,
        rows_in=rows_in, rows_out=rows_out, rows_rejected=rows_rejected,
        rows_changed=rows_changed, rows_unchanged=max(rows_out - rows_changed, 0),
        extra_columns=",".join(extra) if extra else None,
    )


def run(run_date: str, db_path: str, inbox: str, config_path: str,
        batch_size: int = DEFAULT_BATCH_SIZE) -> Dict[str, object]:
    """Run every partner for ``run_date``. Returns a summary; ``summary['exit_code']`` is 0/1."""
    partners = load_config(config_path)
    wh = Warehouse(db_path)
    run_id = f"{run_date}-{uuid.uuid4().hex[:8]}"
    started_at = _now()
    t0 = time.monotonic()
    wh.start_run(run_id, run_date, started_at)
    results: List[dict] = []
    try:
        for partner in partners.values():
            files = find_files(partner, inbox, run_date)
            if not files:
                log.warning("%s: no file for %s yet (late?); re-run this date once it lands",
                            partner.name, run_date)
                results.append(_stats(run_id, partner.name, None, "missing", time.monotonic()))
                continue
            for path in files:
                rel = os.path.relpath(path, inbox)
                results.append(process_file(wh, partner, path, rel, run_id, run_date, started_at, batch_size))
        for s in results:
            wh.record_partner_stats(s)
        failed = any(s["status"] == "failed" for s in results)
        duration = round(time.monotonic() - t0, 4)
        wh.finish_run(run_id, _now(), duration, "failed" if failed else "ok")
    finally:
        wh.close()

    summary = {
        "run_id": run_id, "run_date": run_date, "status": "failed" if failed else "ok",
        "duration_s": duration, "partners": results, "exit_code": 1 if failed else 0,
    }
    log.info("run %s finished: %s", run_id, json.dumps({
        "status": summary["status"], "duration_s": duration,
        "rows_in": sum(s["rows_in"] for s in results),
        "rows_out": sum(s["rows_out"] for s in results),
        "rows_rejected": sum(s["rows_rejected"] for s in results),
    }))
    return summary
