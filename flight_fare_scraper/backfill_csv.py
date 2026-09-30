"""Load a legacy batch/search CSV export into the DuckDB price-history database.

The file's modification time (US Eastern) becomes its snapshot time. Handles the
pre-airline-fix export schema (a single `airline` column), and leaves columns an
export predates as NULL rather than guessing.
"""
import argparse
import logging
import os
import sys
from datetime import datetime
from typing import List, Optional

import pandas as pd

from . import db

logger = logging.getLogger(__name__)


def load_csv_as_frame(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "airline" in frame.columns and "booking_provider" not in frame.columns:
        # providerCode happened to equal the airline for these direct-from-airline results.
        frame["booking_provider"] = frame["airline"]
        frame["outbound_airline"] = frame["airline"]
        frame["return_airline"] = frame["airline"]
    for name in db.RESULT_COLUMNS:
        if name not in frame.columns:
            frame[name] = None
    return frame[db.RESULT_COLUMNS]


def backfill(path: str, db_path: str) -> int:
    snapshot_at = datetime.fromtimestamp(os.path.getmtime(path), tz=db.SNAPSHOT_TZ)
    frame = load_csv_as_frame(path)
    con = db.connect(db_path)
    try:
        recorded = db.insert_frame(con, frame, snapshot_at)
    finally:
        con.close()
    logger.info("backfilled %d rows from %s (snapshot %s) into %s", recorded, path, snapshot_at.date(), db_path)
    return recorded


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_files", nargs="+", help="Legacy CSV exports to backfill")
    parser.add_argument("--db", default="fares.duckdb")
    args = parser.parse_args(argv)
    for path in args.csv_files:
        backfill(path, args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
