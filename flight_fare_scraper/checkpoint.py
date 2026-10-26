"""Save each search as it finishes and publish in parts, so a run cut short keeps what
it collected.

A run used to hold every result in memory until its last search, then record and
publish them in one go: a shutdown, a cancelled job or a lost runner cost the whole
run. Now each search's fares go into the local database as soon as it finishes, and
are queued for publishing beside its search record. Every few searches, or every half
hour, the queue goes up as one more object in the day's partition.

The queue lives in the database too, so what a killed run saved but never published
goes up with the next run's first part. Parts are named after the run that saved them
and the first item in them, so publishing the same items twice -- the upload went
through, then the run died before clearing the queue -- overwrites rather than
duplicates.
"""

import logging
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Set, Tuple

import duckdb
import pandas as pd

from . import db, storage
from .models import FlightResult, SearchQuery
from .runner import SEARCH_RECORD_TYPES

logger = logging.getLogger(__name__)

EVERY_SEARCHES = 25
EVERY_MINUTES = 30.0

FARES_QUEUE = "publish_queue_fares"
RECORDS_QUEUE = "publish_queue_records"
_SEQUENCE = "publish_queue_seq"
RECORD_COLUMNS = {"snapshot_date": "DATE", "run_id": "VARCHAR", "shard": "VARCHAR",
                  **SEARCH_RECORD_TYPES, "days_out": "INTEGER"}
_QUEUE_COLUMNS = {"seq": "BIGINT", "saved_by": "VARCHAR"}
_KEY_TYPES = {"snapshot_date": "DATE", **{name: db.COLUMN_TYPES[name] for name in db.ROUTE_KEY}}


def records_frame(records: Sequence[dict], snapshot: date, run_id: str, shard: Optional[str]) -> pd.DataFrame:
    """Search records as rows, with the run's identity and each search's days out."""
    frame = pd.DataFrame(list(records), columns=list(SEARCH_RECORD_TYPES))
    frame.insert(0, "snapshot_date", snapshot)
    frame.insert(1, "run_id", run_id)
    frame.insert(2, "shard", shard or "1/1")
    frame["days_out"] = [(depart - snapshot).days for depart in frame["depart_date"]]
    return frame


def typed_select(columns: dict, source: str) -> str:
    """Cast every column to its declared type, so files from different runs combine."""
    return "SELECT " + ", ".join(f'CAST("{name}" AS {kind}) AS "{name}"' for name, kind in columns.items()) + \
        f" FROM {source}"


def query_key(query: SearchQuery) -> tuple:
    return (query.site, query.origin, query.destination, query.depart_date, query.return_date)


def ensure_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"CREATE SEQUENCE IF NOT EXISTS {_SEQUENCE}")
    for table, columns in ((FARES_QUEUE, _KEY_TYPES), (RECORDS_QUEUE, RECORD_COLUMNS)):
        defs = ", ".join(f'"{name}" {kind}' for name, kind in {**_QUEUE_COLUMNS, **columns}.items())
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({defs})")
        # A column added to the records since the queue was made: add it, NULL for old rows.
        present = {row[0] for row in con.execute(f"SELECT column_name FROM (DESCRIBE {table})").fetchall()}
        for name, kind in columns.items():
            if name not in present:
                con.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {kind}')


class Target:
    """Where parts are published. `put` takes (table, key) pairs and uploads them together."""

    def put(self, con: duckdb.DuckDBPyConnection, tables: List[Tuple[str, str]]) -> None:
        raise NotImplementedError


class S3Target(Target):
    def __init__(self, con: duckdb.DuckDBPyConnection, bucket: str):
        storage.configure(con)
        self.bucket = bucket

    def put(self, con, tables):
        for table, key in tables:
            storage.push_table(con, table, storage.uri(self.bucket, key))


class HuggingFaceTarget(Target):
    def __init__(self, bucket_id: str):
        self.bucket_id = bucket_id

    def put(self, con, tables):
        directory = Path(tempfile.mkdtemp(prefix="ffs-part-"))
        try:
            files = []
            for position, (table, key) in enumerate(tables):
                local = directory / f"{position}.parquet"
                storage.push_table(con, table, str(local))
                files.append((str(local), key))
            storage.publish_hf(files, self.bucket_id)
        finally:
            for path in directory.glob("*"):
                path.unlink()
            directory.rmdir()


class Checkpoint:
    """Records one search at a time into `con`, and publishes queued searches in parts."""

    def __init__(self, con: duckdb.DuckDBPyConnection, snapshot: date, run_id: str, shard: Optional[str],
                 target: Optional[Target], every: int = EVERY_SEARCHES, minutes: float = EVERY_MINUTES,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], datetime] = db.snapshot_now):
        self.con, self.snapshot, self.run_id, self.shard = con, snapshot, run_id, shard
        self.target, self.every, self.minutes = target, every, minutes
        self.clock, self.now = clock, now
        # A re-run of a CI job keeps its run id; the start time keeps its parts apart.
        self.saved_by = f"{run_id}-{now():%H%M%S}"
        self.rows = 0
        self._since = 0
        self._last_flush = clock()
        if target is not None:
            ensure_tables(con)

    def done_today(self) -> Set[tuple]:
        """Searches that already have fares recorded on this run's snapshot date."""
        rows = self.con.execute(
            f"SELECT DISTINCT {', '.join(db.ROUTE_KEY)} FROM fares WHERE snapshot_date = ?",
            [self.snapshot]).fetchall()
        return {(site, origin, dest, _as_date(depart), _as_date(ret)) for site, origin, dest, depart, ret in rows}

    def add(self, query: SearchQuery, results: List[FlightResult], record: Optional[dict]) -> None:
        """Save one finished search, and publish the queue if a part is due."""
        if self.target is not None:
            if results:  # queued before the insert: a crash between leaves an empty key, not lost rows
                self._enqueue(FARES_QUEUE, pd.DataFrame([dict(zip(_KEY_TYPES, (self.snapshot, *query_key(query))))]),
                              _KEY_TYPES)
            if record is not None:
                self.add_records([record])
        if results:
            self.rows += db.insert_snapshot(self.con, results, self.now(), self.snapshot)
        self._since += 1
        if self.target is not None and (self._since >= self.every
                                        or self.clock() - self._last_flush >= self.minutes * 60):
            try:
                self.flush()
            except Exception as error:  # left queued; the next part or the final one retries it
                logger.warning("CHECKPOINT_NOT_PUBLISHED: %s; will retry with the next part", type(error).__name__)
                self._since, self._last_flush = 0, self.clock()

    def add_records(self, records: Sequence[dict]) -> None:
        """Queue search records with no fares of their own, such as searches never reached."""
        if self.target is None or not records:
            return
        self._enqueue(RECORDS_QUEUE, records_frame(records, self.snapshot, self.run_id, self.shard), RECORD_COLUMNS)

    def _enqueue(self, table: str, frame: pd.DataFrame, columns: dict) -> None:
        self.con.register("ffs_enqueue", frame)
        try:
            self.con.execute(
                f"INSERT INTO {table} BY NAME SELECT nextval('{_SEQUENCE}') AS seq, "
                f"{_sql_text(self.saved_by)} AS saved_by, * FROM ({typed_select(columns, 'ffs_enqueue')})")
        finally:
            self.con.unregister("ffs_enqueue")

    def pending(self) -> int:
        if self.target is None:
            return 0
        return sum(self.con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                   for table in (FARES_QUEUE, RECORDS_QUEUE))

    def flush(self) -> int:
        """Publish everything queued, one part per snapshot date and saving run.

        Raises if an upload fails, leaving that part queued. Returns parts published.
        """
        if self.target is None:
            return 0
        groups = self.con.execute(
            f"SELECT snapshot_date, saved_by, min(seq) FROM (SELECT snapshot_date, saved_by, seq FROM {FARES_QUEUE} "
            f"UNION ALL SELECT snapshot_date, saved_by, seq FROM {RECORDS_QUEUE}) GROUP BY ALL ORDER BY 3").fetchall()
        for snapshot, saved_by, first in groups:
            snapshot = _as_date(snapshot)
            (top,) = self.con.execute(
                f"SELECT max(seq) FROM (SELECT seq FROM {FARES_QUEUE} WHERE snapshot_date = ? AND saved_by = ? "
                f"UNION ALL SELECT seq FROM {RECORDS_QUEUE} WHERE snapshot_date = ? AND saved_by = ?)",
                [snapshot, saved_by] * 2).fetchone()
            in_part = "snapshot_date = ? AND saved_by = ? AND seq <= ?"
            params = [snapshot, saved_by, top]
            match = " AND ".join(f"fares.{name} = queued.{name}" for name in ("snapshot_date", *db.ROUTE_KEY))
            self.con.execute(
                f"CREATE OR REPLACE TEMP TABLE ffs_part_fares AS SELECT fares.* FROM fares "
                f"SEMI JOIN (SELECT * FROM {FARES_QUEUE} WHERE {in_part}) AS queued ON {match}", params)
            self.con.execute(
                f"CREATE OR REPLACE TEMP TABLE ffs_part_records AS "
                f"SELECT * EXCLUDE (seq, saved_by) FROM {RECORDS_QUEUE} WHERE {in_part} ORDER BY seq", params)
            name = f"{saved_by}-{first:06d}"
            (fare_rows,) = self.con.execute("SELECT count(*) FROM ffs_part_fares").fetchone()
            (record_rows,) = self.con.execute("SELECT count(*) FROM ffs_part_records").fetchone()
            tables = [(table, storage.snapshot_key(snapshot, name, prefix=prefix))
                      for table, prefix, rows in (("ffs_part_fares", storage.SNAPSHOT_PREFIX, fare_rows),
                                                  ("ffs_part_records", storage.DIAGNOSTICS_PREFIX, record_rows))
                      if rows]
            if tables:
                self.target.put(self.con, tables)
            for table in (FARES_QUEUE, RECORDS_QUEUE):
                self.con.execute(f"DELETE FROM {table} WHERE {in_part}", params)
            logger.info("published a part: %d fare row(s), %d search record(s)", fare_rows, record_rows)
        self._since = 0
        self._last_flush = self.clock()
        return len(groups)

    def close(self) -> None:
        """Publish whatever is still queued. Raises StorageError, naming only the error type."""
        try:
            self.flush()
        except Exception as error:
            raise storage.StorageError(
                f"{type(error).__name__}; {self.pending()} item(s) stay queued in the local database "
                "and go up with the next run") from None


def _as_date(value) -> date:
    return value if isinstance(value, date) and not isinstance(value, datetime) else \
        datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
