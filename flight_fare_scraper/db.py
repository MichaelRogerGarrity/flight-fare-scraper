import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd

from .models import FlightResult

logger = logging.getLogger(__name__)

# Snapshots are stamped on the US Eastern calendar (EST/EDT) as naive wall-clock time,
# so a 9pm Eastern run is recorded on the day it actually ran, not the next UTC day.
SNAPSHOT_TZ = ZoneInfo("America/New_York")

# Single source of truth for the table's shape. Creation and migration of an older
# on-disk table both derive from this list.
COLUMN_DEFS: List[Tuple[str, str]] = [
    ("snapshot_date", "DATE"),
    ("snapshot_datetime", "TIMESTAMP"),
    ("site", "VARCHAR"),
    ("origin", "VARCHAR"),
    ("destination", "VARCHAR"),
    ("depart_date", "DATE"),
    ("return_date", "DATE"),
    ("price", "DOUBLE"),
    ("currency", "VARCHAR"),
    ("booking_provider", "VARCHAR"),
    ("outbound_airline", "VARCHAR"),
    ("outbound_operated_by", "VARCHAR"),
    ("outbound_depart", "TIMESTAMP"),
    ("outbound_arrive", "TIMESTAMP"),
    ("outbound_from", "VARCHAR"),
    ("outbound_to", "VARCHAR"),
    ("outbound_stops", "INTEGER"),
    ("outbound_duration_min", "INTEGER"),
    ("outbound_layover_airports", "VARCHAR"),
    ("outbound_layover_min", "INTEGER"),
    ("outbound_airport_change", "BOOLEAN"),
    ("outbound_equipment", "VARCHAR"),
    ("return_airline", "VARCHAR"),
    ("return_operated_by", "VARCHAR"),
    ("return_depart", "TIMESTAMP"),
    ("return_arrive", "TIMESTAMP"),
    ("return_from", "VARCHAR"),
    ("return_to", "VARCHAR"),
    ("return_stops", "INTEGER"),
    ("return_duration_min", "INTEGER"),
    ("return_layover_airports", "VARCHAR"),
    ("return_layover_min", "INTEGER"),
    ("return_airport_change", "BOOLEAN"),
    ("return_equipment", "VARCHAR"),
    ("cabin_class", "VARCHAR"),
    ("carry_on_status", "VARCHAR"),
    ("carry_on_fee", "DOUBLE"),
    ("checked_bag_status", "VARCHAR"),
    ("checked_bag_fee", "DOUBLE"),
    ("second_checked_bag_fee", "DOUBLE"),
    ("free_cancellation", "BOOLEAN"),
    ("virtual_interline", "BOOLEAN"),
    ("self_transfer_protection", "BOOLEAN"),
    ("price_prediction", "VARCHAR"),
    ("price_prediction_change", "DOUBLE"),
    ("price_prediction_days", "INTEGER"),
]
COLUMNS = [name for name, _ in COLUMN_DEFS]
COLUMN_TYPES = dict(COLUMN_DEFS)
RESULT_COLUMNS = COLUMNS[2:]  # everything except the two snapshot columns

# Columns an earlier schema version had that this one removes on migration.
# is_split_ticket came from splitBookingOptions, which was never non-empty in any
# observed response; virtual_interline is the signal that actually appears.
DROPPED_COLUMNS = ["booking_url", "carry_on_included", "is_split_ticket"]

INDEXES: Dict[str, Tuple[str, ...]] = {
    "idx_route_window": ("site", "origin", "destination", "depart_date", "return_date"),
    "idx_depart_date": ("depart_date",),
    "idx_snapshot": ("snapshot_date",),
}

ROUTE_KEY = ("site", "origin", "destination", "depart_date", "return_date")

_NULLABLE_DTYPES = {"BOOLEAN": "boolean", "INTEGER": "Int64", "DOUBLE": "Float64"}


def connect(db_path: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db_path)
    try:
        ensure_schema(con)
    except BaseException:
        con.close()
        raise
    return con


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create or migrate the fares table, rebuilding indexes only when something changed.

    This runs on every connect, so the up-to-date path is just two catalog lookups
    rather than a rebuild of every index over the whole table.
    """
    columns_sql = ", ".join(f"{name} {type_}" for name, type_ in COLUMN_DEFS)
    con.execute(f"CREATE TABLE IF NOT EXISTS fares ({columns_sql})")

    existing = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM duckdb_columns() "
            "WHERE database_name = current_database() AND schema_name = 'main' AND table_name = 'fares'"
        ).fetchall()
    }
    to_add = [(name, type_) for name, type_ in COLUMN_DEFS if name not in existing]
    to_drop = [name for name in DROPPED_COLUMNS if name in existing]

    current = _current_indexes(con)
    if to_add or to_drop:
        # DuckDB refuses DROP COLUMN on a table with any index, even on an unindexed column,
        # so drop ours first and rebuild once after. Column changes are rare.
        for name in current:
            if name in INDEXES:
                con.execute(f"DROP INDEX {name}")
        for name, type_ in to_add:
            logger.info("migrating fares: adding column %s %s", name, type_)
            con.execute(f"ALTER TABLE fares ADD COLUMN {name} {type_}")
        for name in to_drop:
            logger.info("migrating fares: dropping column %s", name)
            con.execute(f"ALTER TABLE fares DROP COLUMN {name}")
        current = {}

    for name, columns in INDEXES.items():
        if current.get(name) == columns:
            continue
        if name in current:
            logger.info("rebuilding index %s: %s -> %s", name, current[name], columns)
            con.execute(f"DROP INDEX {name}")
        con.execute(f"CREATE INDEX {name} ON fares({', '.join(columns)})")


def _current_indexes(con: duckdb.DuckDBPyConnection) -> Dict[str, Tuple[str, ...]]:
    rows = con.execute(
        "SELECT index_name, expressions FROM duckdb_indexes() "
        "WHERE database_name = current_database() AND schema_name = 'main' AND table_name = 'fares'"
    ).fetchall()
    # `expressions` comes back as a string such as '[site, origin, destination]'.
    return {
        name: tuple(part.strip() for part in (expressions or "").strip("[]").split(",") if part.strip())
        for name, expressions in rows
    }


def snapshot_now() -> datetime:
    return datetime.now(SNAPSHOT_TZ).replace(tzinfo=None)


def _as_snapshot_time(moment: Optional[datetime]) -> datetime:
    if moment is None:
        return snapshot_now()
    if moment.tzinfo is not None:
        return moment.astimezone(SNAPSHOT_TZ).replace(tzinfo=None)
    return moment


def insert_snapshot(
    con: duckdb.DuckDBPyConnection,
    results: List[FlightResult],
    snapshot_at: Optional[datetime] = None,
) -> int:
    """Record one dated snapshot. A naive `snapshot_at` is taken as Eastern wall-clock time."""
    frame = pd.DataFrame(
        [[getattr(result, name) for name in RESULT_COLUMNS] for result in results],
        columns=RESULT_COLUMNS,
    )
    return insert_frame(con, frame, snapshot_at)


def insert_frame(
    con: duckdb.DuckDBPyConnection,
    frame: pd.DataFrame,
    snapshot_at: Optional[datetime] = None,
) -> int:
    """Record one dated snapshot, replacing rather than duplicating earlier same-day rows.

    Dedup lives in the application instead of a UNIQUE constraint (cheaper on bulk
    loads): in one transaction, delete existing rows for this snapshot date and each
    (site, origin, destination, depart_date, return_date) in `frame`, then insert.
    `site` is part of that key so two scrapers tracking the same route on the same
    day can't delete each other's rows, and the transaction means a failed insert
    can't leave the day's earlier rows deleted with nothing in their place.
    """
    if frame.empty:
        return 0

    moment = _as_snapshot_time(snapshot_at)
    incoming = frame[RESULT_COLUMNS].copy()
    for name in RESULT_COLUMNS:
        dtype = _NULLABLE_DTYPES.get(COLUMN_TYPES[name])
        if dtype:
            incoming[name] = incoming[name].astype(dtype)
    incoming.insert(0, "snapshot_date", moment.date())
    incoming.insert(1, "snapshot_datetime", moment)

    route_key = ", ".join(f"CAST({name} AS {COLUMN_TYPES[name]}) AS {name}" for name in ROUTE_KEY)
    route_match = " AND ".join(f"fares.{name} = incoming_routes.{name}" for name in ROUTE_KEY)
    # Named target columns: a migrated table's physical column order differs from COLUMNS.
    target_columns = ", ".join(COLUMNS)
    values = ", ".join(f"CAST({name} AS {type_})" for name, type_ in COLUMN_DEFS)

    con.register("incoming_fares", incoming)
    try:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                f"DELETE FROM fares USING (SELECT DISTINCT {route_key} FROM incoming_fares) AS incoming_routes "
                f"WHERE fares.snapshot_date = ? AND {route_match}",
                [moment.date()],
            )
            con.execute(f"INSERT INTO fares ({target_columns}) SELECT {values} FROM incoming_fares")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.unregister("incoming_fares")
    return len(incoming)
