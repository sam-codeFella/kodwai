#!/usr/bin/env python3
"""Load one day of partner order CSVs into the `orders` table, idempotently.

    python pipeline.py --run-date 2026-01-03

Exit codes: 0 ok (missing/duplicate files are not failures), 1 a partner file failed,
2 bad usage or config.
"""
import argparse
import json
import logging
import sys
from datetime import date

from etl.config import ConfigError
from etl.runner import DEFAULT_BATCH_SIZE, run


def _date(s: str) -> str:
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} is not a YYYY-MM-DD date")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-date", required=True, type=_date, help="business date to load, YYYY-MM-DD")
    ap.add_argument("--db", default="warehouse.db", help="SQLite file (default: %(default)s)")
    ap.add_argument("--inbox", default="data/inbox", help="root of partner drop folders (default: %(default)s)")
    ap.add_argument("--config", default="config/partners.json", help="partner registry (default: %(default)s)")
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    try:
        summary = run(args.run_date, args.db, args.inbox, args.config, args.batch_size)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    return summary["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
