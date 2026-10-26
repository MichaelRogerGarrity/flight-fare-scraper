"""Per-search records: what each search was, how it went, and how long it took."""

import logging
from argparse import Namespace
from datetime import date

import duckdb
import pytest

from flight_fare_scraper import cli
from flight_fare_scraper.config import build_query
from flight_fare_scraper.runner import SEARCH_RECORD_TYPES, BatchReport, run_queries, search_record
from flight_fare_scraper.scrapers.base import SearchTimeoutError
from tests.factories import make_result
from tests.test_runner import ScriptedScraper

TOKYO = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false")
BANGKOK = build_query("MIA", "BKK", "2026-11-13", "2026-11-22", nonstop="false")


class StatsScraper:
    def __init__(self, stats):
        self.last_stats = stats


MAIN = {"first_page_seconds": 26.1, "pages_available": 19, "pages_fetched": 5,
        "results_reported": 935, "truncated": False, "ignored_polls": 1, "unreadable_polls": 0}


def test_a_successful_search_record():
    record = search_record(TOKYO, StatsScraper({"main": MAIN, "rows_kept": 707, "rows_dropped_by_filters": 3}),
                           attempts=1, seconds=48.84, in_page_clicks=0, error=None)
    assert record["outcome"] == "ok"
    assert (record["origin"], record["destination"]) == ("MIA", "TYO")
    assert record["seconds"] == 48.8
    assert record["first_page_seconds"] == 26.1
    assert (record["pages_fetched"], record["pages_available"]) == (5, 19)
    assert record["rows_kept"] == 707 and record["rows_dropped_by_filters"] == 3
    assert record["nonstop_pass"] is False and record["error_type"] is None


def test_a_search_cut_short_is_partial():
    stats = {"main": {**MAIN, "truncated": True}, "rows_kept": 50}
    record = search_record(TOKYO, StatsScraper(stats), 1, 10.0, 4, None)
    assert record["outcome"] == "partial" and record["truncated"] is True
    assert record["in_page_clicks"] == 4


def test_a_failed_search_keeps_the_full_error_and_what_it_learned_first():
    # Page 1 never answered, but the pass had already counted an unrecognized response:
    # exactly the evidence a change to the site's response format would leave.
    stats = {"main": {"ignored_polls": 3, "unreadable_polls": 0}}
    error = SearchTimeoutError("route-x: no results response for page 1 within 45s")
    record = search_record(TOKYO, StatsScraper(stats), 3, 190.0, 0, error)
    assert record["outcome"] == "failed" and record["attempts"] == 3
    assert record["error_type"] == "SearchTimeoutError"
    assert "page 1 within 45s" in record["error_message"]
    assert record["ignored_polls"] == 3
    assert record["first_page_seconds"] is None and record["rows_kept"] == 0


def test_every_record_field_has_a_declared_type():
    keys = set(search_record(TOKYO, StatsScraper({}), 1, 1.0, 0, None))
    assert keys == set(SEARCH_RECORD_TYPES)


def test_the_runner_records_every_search_including_failures():
    scraper = ScriptedScraper({
        TOKYO.label: [SearchTimeoutError("1"), SearchTimeoutError("2"), SearchTimeoutError("3")],
        BANGKOK.label: [[make_result(destination="BKK")]],
    })
    report = run_queries([TOKYO, BANGKOK], delays=(0, 0), sleep=lambda _s: None,
                         scraper_for=lambda site: lambda headless: scraper, search_jitter_s=None)
    by_destination = {r["destination"]: r for r in report.records}
    assert set(by_destination) == {"TYO", "BKK"}
    assert by_destination["TYO"]["outcome"] == "failed" and by_destination["TYO"]["attempts"] == 3
    assert by_destination["BKK"]["outcome"] == "ok" and by_destination["BKK"]["attempts"] == 1


def _report_with_records():
    ok = search_record(TOKYO, StatsScraper({"main": MAIN, "rows_kept": 707}), 1, 48.8, 0, None)
    ok_too = search_record(BANGKOK, StatsScraper({"main": MAIN, "rows_kept": 600}), 1, 40.0, 0, None)
    return BatchReport(records=[ok, ok_too])


def test_diagnostics_have_a_fixed_schema_even_when_a_column_is_all_empty(tmp_path):
    """No errors in this run, so error_type is entirely NULL. Its type must still be
    VARCHAR, or this file won't combine cleanly with one from a run that had errors."""
    out = tmp_path / "diag.parquet"
    con = duckdb.connect()
    args = Namespace(diagnostics=str(out), push=False, shard="2/6")
    cli._write_diagnostics(con, _report_with_records(), args, date(2026, 10, 13), "run-1")
    types = dict(duckdb.sql(f"SELECT column_name, column_type FROM (DESCRIBE SELECT * FROM '{out}')").fetchall())
    assert types["error_type"] == "VARCHAR" and types["error_message"] == "VARCHAR"
    assert types["depart_date"] == "DATE" and types["snapshot_date"] == "DATE"
    assert types["nonstop_pages_fetched"] == "INTEGER"
    rows = duckdb.sql(f"SELECT shard, run_id, days_out FROM '{out}' ORDER BY days_out").fetchall()
    assert rows == [("2/6", "run-1", 31), ("2/6", "run-1", 45)]


def test_a_diagnostics_failure_never_fails_the_run(caplog, monkeypatch, tmp_path):
    con = duckdb.connect()
    args = Namespace(diagnostics=str(tmp_path / "diag.parquet"), push=False, shard="0/6")

    def broken(*_args):
        raise RuntimeError("MIA-TYO 2026-11-27: disk full")
    monkeypatch.setattr(cli.checkpoint, "records_frame", broken)
    with caplog.at_level(logging.WARNING, logger="flight_fare_scraper"):
        cli._write_diagnostics(con, _report_with_records(), args, date(2026, 10, 13), "run-1")
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("DIAGNOSTICS_NOT_SAVED") for m in messages)
    # The warning names only the error type: no routes, no bucket, no paths.
    assert not any("MIA" in m or "TYO" in m for m in messages)


def test_nothing_is_written_without_a_destination(tmp_path):
    con = duckdb.connect()
    args = Namespace(diagnostics="", push=False, shard=None)
    cli._write_diagnostics(con, _report_with_records(), args, date(2026, 10, 13), "run-1")
    assert not list(tmp_path.iterdir())
