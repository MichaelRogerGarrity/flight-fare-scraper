"""Publish snapshots to S3-compatible object storage, and read them back.

The scheduled runs have nowhere durable to keep a DuckDB file, so each run writes
its rows as a Parquet object and never rewrites what is already there. DuckDB's
httpfs extension does both directions, so this needs no extra dependency.

Hugging Face Storage Buckets are the intended target: the gateway lives at
s3.hf.co/<namespace> and requires path-style addressing. Any S3-compatible store
works as long as the endpoint is given without a scheme.

Credentials come from the environment and are never logged.
"""

import logging
import os
import re
from datetime import date
from typing import List, Optional

import duckdb

logger = logging.getLogger(__name__)

ENDPOINT_ENV = "FFS_S3_ENDPOINT"  # host and optional path, no scheme: s3.hf.co/<namespace>
KEY_ID_ENV = "FFS_S3_KEY_ID"
SECRET_ENV = "FFS_S3_SECRET"
BUCKET_ENV = "FFS_S3_BUCKET"
REGION = "us-east-1"  # the Hugging Face gateway is single-region but still requires one

SNAPSHOT_PREFIX = "snapshots"
_SAFE_KEY_PART = re.compile(r"[^A-Za-z0-9._-]")


class StorageError(RuntimeError):
    pass


def configured() -> bool:
    return all(os.environ.get(name, "").strip() for name in (ENDPOINT_ENV, KEY_ID_ENV, SECRET_ENV))


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise StorageError(f"{name} is not set; object storage is not configured")
    return value


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def configure(con: duckdb.DuckDBPyConnection) -> None:
    """Load httpfs and install the S3 credentials on this connection."""
    endpoint = require_env(ENDPOINT_ENV).removeprefix("https://").removeprefix("http://").rstrip("/")
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    # CREATE SECRET takes no bind parameters, so the values are escaped by hand.
    # None of them reach the log.
    con.execute(
        f"""
        CREATE OR REPLACE SECRET ffs_object_store (
            TYPE s3,
            KEY_ID {_sql_literal(require_env(KEY_ID_ENV))},
            SECRET {_sql_literal(require_env(SECRET_ENV))},
            ENDPOINT {_sql_literal(endpoint)},
            URL_STYLE 'path',
            REGION '{REGION}'
        )
        """
    )
    logger.debug("object storage configured for endpoint %s", endpoint)


def snapshot_key(snapshot: date, run_id: str, shard: Optional[str] = None) -> str:
    """Hive-partitioned so a reader can prune by snapshot_date without opening files."""
    parts = [_SAFE_KEY_PART.sub("-", part) for part in (run_id, shard or "") if part]
    return f"{SNAPSHOT_PREFIX}/snapshot_date={snapshot.isoformat()}/{'-'.join(parts)}.parquet"


def uri(bucket: str, key: str) -> str:
    return f"s3://{bucket.strip('/')}/{key}"


def push_snapshot(
    con: duckdb.DuckDBPyConnection,
    destination: str,
    snapshot: date,
    table: str = "fares",
) -> int:
    """Copy one snapshot's rows to `destination`. Returns the row count written."""
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE ffs_push AS SELECT * FROM {table} WHERE snapshot_date = ?",
        [snapshot],
    )
    (rows,) = con.execute("SELECT count(*) FROM ffs_push").fetchone()
    if not rows:
        logger.warning("nothing to publish for snapshot %s", snapshot)
        return 0
    con.execute(f"COPY ffs_push TO {_sql_literal(destination)} (FORMAT PARQUET, COMPRESSION ZSTD)")
    con.execute("DROP TABLE ffs_push")
    logger.info("published %d rows to object storage", rows)
    return rows


def pull(
    con: duckdb.DuckDBPyConnection,
    bucket: str,
    columns: List[str],
    snapshot_key_columns: List[str],
    table: str = "fares",
    prefix: str = SNAPSHOT_PREFIX,
) -> int:
    """Merge every published Parquet object into the local table.

    Named columns rather than SELECT *: the objects carry a hive snapshot_date
    column and may have been written by an older or newer schema.

    Two levels of dedup, both keyed the same way `db.insert_frame` keys a local
    snapshot. QUALIFY keeps only the newest run for a route-day, so a re-run that
    published a second object for the same day doesn't land twice; the anti join
    then drops whole groups the local table already holds.
    """
    pattern = uri(bucket, f"{prefix}/**/*.parquet")
    selected = ", ".join(f'remote."{name}"' for name in columns)
    names = ", ".join(f'"{name}"' for name in columns)
    key_columns = ", ".join(f'"{name}"' for name in snapshot_key_columns)
    key_match = " AND ".join(
        f'remote."{name}" = local."{name}"' for name in snapshot_key_columns
    )
    inserted = con.execute(
        f"""
        INSERT INTO {table} ({names})
        SELECT {selected}
        FROM (
            SELECT * FROM read_parquet({_sql_literal(pattern)}, union_by_name = true)
            QUALIFY snapshot_datetime = max(snapshot_datetime) OVER (PARTITION BY {key_columns})
        ) AS remote
        ANTI JOIN (SELECT DISTINCT {key_columns} FROM {table}) AS local
          ON {key_match}
        """
    ).fetchone()
    count = inserted[0] if inserted else 0
    logger.info("pulled %s new row(s) from object storage", count)
    return count
