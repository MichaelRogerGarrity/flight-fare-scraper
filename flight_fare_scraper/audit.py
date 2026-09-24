"""Data-quality report over collected snapshots.

Counts rows, checks each snapshot against what the schedule expected, and looks for
the failure modes this scraper has actually shipped before: a parser change that
turns a column silently NULL, price sorting that evicts every nonstop, reseller
duplicates swamping an itinerary, absurd prices from OTA listings.

Every line is safe for a public CI log. Routes are reported as counts and as salted
ids, never as airport codes, so this can run in Actions against the real bucket.
"""

import logging
from datetime import date
from typing import Dict, List, Optional, Tuple

import duckdb

from . import redact, schedule
from .db import COLUMNS

logger = logging.getLogger(__name__)

# Columns that should essentially always be populated. A null rate above the
# threshold means the parser stopped finding something it used to find.
EXPECTED_POPULATED: Dict[str, float] = {
    "price": 0.0,
    "currency": 0.0,
    "outbound_airline": 0.01,
    "return_airline": 0.01,
    "outbound_depart": 0.0,
    "outbound_arrive": 0.0,
    "outbound_duration_min": 0.01,
    "return_duration_min": 0.01,
    "outbound_stops": 0.0,
    "return_stops": 0.0,
    "cabin_class": 0.05,
}
# Populated only when the itinerary has a connection, or when the site chose to say.
CONDITIONAL_COLUMNS = (
    "outbound_layover_min", "return_layover_min", "outbound_layover_airports",
    "return_layover_airports", "price_prediction", "carry_on_fee", "checked_bag_fee",
    "second_checked_bag_fee", "outbound_operated_by", "return_operated_by",
)
PLAUSIBLE_PRICE = (20.0, 20000.0)
_OFFSET_CHECK = " OR ".join(
    f"(({leg}_duration_min - date_diff('minute', {leg}_depart, {leg}_arrive)) % 15 <> 0"
    f" OR abs({leg}_duration_min - date_diff('minute', {leg}_depart, {leg}_arrive)) > 14 * 60)"
    for leg in ("outbound", "return")
)
PLAUSIBLE_DURATION_MIN = (30, 3000)  # half an hour to just over two days
MISSING_TOLERANCE = 0.05  # share of a day's searches that may legitimately return nothing


def _scalar(con: duckdb.DuckDBPyConnection, sql: str, params=None):
    row = con.execute(sql, params or []).fetchone()
    return row[0] if row else None


def snapshots(con: duckdb.DuckDBPyConnection, table: str) -> List[Tuple]:
    return con.execute(f"""
        SELECT snapshot_date,
               count(*)                                        AS rows,
               count(DISTINCT (site, origin, destination, depart_date, return_date)) AS searches,
               count(DISTINCT depart_date)                     AS departures,
               min(depart_date)                                AS first_departure,
               max(depart_date)                                AS last_departure
        FROM {table} GROUP BY 1 ORDER BY 1
    """).fetchall()


def coverage(con: duckdb.DuckDBPyConnection, table: str, spec: schedule.Spec,
             snapshot: date) -> Tuple[int, int, List]:
    """Compare one snapshot against the searches the schedule says were due that day."""
    expected = {
        (query.site, query.origin, query.destination, query.depart_date, query.return_date)
        for query in schedule.due_today(spec, snapshot)
    }
    found = set(con.execute(
        f"SELECT DISTINCT site, origin, destination, depart_date, return_date "
        f"FROM {table} WHERE snapshot_date = ?", [snapshot]).fetchall())
    missing = sorted(expected - found, key=lambda row: (row[3], row[4]))
    return len(expected), len(found), missing


def null_rates(con: duckdb.DuckDBPyConnection, table: str) -> List[Tuple[str, float]]:
    total = _scalar(con, f"SELECT count(*) FROM {table}") or 0
    if not total:
        return []
    selects = ", ".join(f'count(*) FILTER (WHERE "{name}" IS NULL)' for name in COLUMNS)
    counts = con.execute(f"SELECT {selects} FROM {table}").fetchone()
    return [(name, nulls / total) for name, nulls in zip(COLUMNS, counts)]


def shape(con: duckdb.DuckDBPyConnection, table: str) -> Dict[str, object]:
    total = _scalar(con, f"SELECT count(*) FROM {table}") or 0
    if not total:
        return {}
    nonstop = _scalar(con, f"SELECT count(*) FROM {table} WHERE outbound_stops = 0 AND return_stops = 0")
    itineraries = _scalar(con, f"""
        SELECT count(*) FROM (
            SELECT 1 FROM {table}
            GROUP BY snapshot_date, origin, destination, depart_date, return_date,
                     outbound_depart, return_depart, outbound_airline)
    """)
    return {
        "rows": total,
        "nonstop_share": nonstop / total,
        "distinct_itineraries": itineraries,
        "rows_per_itinerary": total / itineraries if itineraries else 0,
        "cabins": con.execute(
            f"SELECT coalesce(cabin_class, '(null)'), count(*) FROM {table} "
            f"GROUP BY 1 ORDER BY 2 DESC LIMIT 6").fetchall(),
    }


def anomalies(con: duckdb.DuckDBPyConnection, table: str) -> List[Tuple[str, int]]:
    low, high = PLAUSIBLE_PRICE
    short, long = PLAUSIBLE_DURATION_MIN
    checks = {
        f"price below ${low:.0f}": f"price < {low}",
        f"price above ${high:.0f}": f"price > {high}",
        "price null or non-positive": "price IS NULL OR price <= 0",
        f"leg under {short} min": f"outbound_duration_min < {short} OR return_duration_min < {short}",
        f"leg over {long} min": f"outbound_duration_min > {long} OR return_duration_min > {long}",
        # Not "arrive < depart": timestamps are local wall-clock, so an eastbound
        # transpacific leg legitimately lands earlier in the day than it took off.
        # What must hold is that duration minus the wall-clock gap is a real UTC
        # offset difference -- a multiple of 15 minutes, within 14 hours.
        "timestamps disagree with duration": _OFFSET_CHECK,
        "return departs before outbound arrives": "return_depart < outbound_arrive",
        "negative stops": "outbound_stops < 0 OR return_stops < 0",
        "nonstop with a layover": "(outbound_stops = 0 AND outbound_layover_min > 0)"
                                  " OR (return_stops = 0 AND return_layover_min > 0)",
        "connection with no layover recorded": "(outbound_stops > 0 AND outbound_layover_min IS NULL)"
                                               " OR (return_stops > 0 AND return_layover_min IS NULL)",
    }
    out = []
    for label, predicate in checks.items():
        out.append((label, _scalar(con, f"SELECT count(*) FROM {table} WHERE {predicate}") or 0))
    return out


def report(con: duckdb.DuckDBPyConnection, table: str,
           spec: Optional[schedule.Spec] = None) -> bool:
    """Print the report. Returns True if nothing looked wrong."""
    clean = True
    rows = snapshots(con, table)
    if not rows:
        logger.error("no rows found in %s", table)
        return False

    logger.info("== snapshots ==")
    for snapshot_date, count, searches, departures, first, last in rows:
        horizon = (last - snapshot_date).days
        logger.info("  %s  %8d rows  %4d searches  %3d departures  horizon %3dd",
                    snapshot_date, count, searches, departures, horizon)

    if spec is not None:
        logger.info("== coverage against the schedule ==")
        for snapshot_date, *_ in rows:
            expected, found, missing = coverage(con, table, spec, snapshot_date)
            if missing:
                # A search that ran fine can still yield nothing: airlines publish
                # schedules roughly 330 days out, and a nonstop-only search on a date
                # with no nonstop returns an empty result set. Only a large shortfall
                # means something actually broke.
                serious = len(missing) > max(2, round(expected * MISSING_TOLERANCE))
                log = logger.error if serious else logger.warning
                clean = clean and not serious
                log("  %s  expected %d, found %d, %d absent%s", snapshot_date, expected, found,
                    len(missing), " -- ABOVE TOLERANCE" if serious else " (may be genuinely empty)")
                for site, origin, destination, depart, returns in missing[:5]:
                    log("    absent: %s depart %s (%dd out) return %s",
                        redact.route_id(_Stub(site, origin, destination, depart, returns)),
                        depart, (depart - snapshot_date).days, returns)
            else:
                extra = found - expected
                logger.info("  %s  expected %d, found %d%s", snapshot_date, expected, found,
                            f" (+{extra} carried from an earlier spec)" if extra > 0 else "")

    logger.info("== column completeness ==")
    for name, rate in null_rates(con, table):
        threshold = EXPECTED_POPULATED.get(name)
        if threshold is not None and rate > threshold:
            clean = False
            logger.error("  %-28s %5.1f%% null  (expected under %.0f%%)", name, rate * 100, threshold * 100)
        elif rate >= 0.999 and name not in CONDITIONAL_COLUMNS:
            clean = False
            logger.error("  %-28s %5.1f%% null  (entirely empty)", name, rate * 100)
        elif rate > 0:
            logger.info("  %-28s %5.1f%% null", name, rate * 100)

    facts = shape(con, table)
    logger.info("== shape ==")
    logger.info("  rows                     %d", facts["rows"])
    logger.info("  distinct itineraries     %d", facts["distinct_itineraries"])
    logger.info("  rows per itinerary       %.1f  (resellers listing the same flight)",
                facts["rows_per_itinerary"])
    logger.info("  nonstop share            %.1f%%", facts["nonstop_share"] * 100)
    for cabin, count in facts["cabins"]:
        logger.info("  cabin %-18s %d", cabin, count)
    if facts["nonstop_share"] == 0:
        clean = False
        logger.error("  no nonstop itineraries at all -- price sorting may be evicting them again")

    logger.info("== anomalies ==")
    for label, count in anomalies(con, table):
        if count:
            logger.warning("  %-38s %d rows", label, count)
        else:
            logger.info("  %-38s none", label)

    return clean


class _Stub:
    """Just enough of a query for redact.route_id, which keys on these fields."""

    def __init__(self, site, origin, destination, depart_date, return_date):
        self.site = site
        self.origin = origin
        self.destination = destination
        self.depart_date = depart_date
        self.return_date = return_date
        self.nonstop_only = False
