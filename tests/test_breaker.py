"""Stopping a run that can't succeed: the circuit breaker and the deadline."""

import json
from datetime import datetime, timezone

import pytest

from flight_fare_scraper import cli
from flight_fare_scraper.config import build_query
from flight_fare_scraper.runner import STOPPED_BREAKER, STOPPED_DEADLINE, run_queries
from flight_fare_scraper.scrapers.base import SearchTimeoutError
from tests.factories import make_result
from tests.test_runner import ScriptedScraper

QUERIES = [build_query("MIA", "TYO", f"2027-01-{day:02d}", f"2027-01-{day + 16:02d}", nonstop="false")
           for day in range(1, 13)]


def run(script, **options):
    scraper = ScriptedScraper(script)
    report = run_queries(QUERIES, delays=(), sleep=lambda _s: None,
                         scraper_for=lambda site: lambda headless: scraper, search_jitter_s=None, **options)
    return report, scraper


def blocked(n):
    return [SearchTimeoutError("no results response for page 1")] * n


def test_five_failures_in_a_row_stop_the_batch():
    """What 2026-10-23 looked like: every search failing from the first. Without a
    breaker each shard spent four hours retrying into the block."""
    report, scraper = run({q.label: [SearchTimeoutError("blocked")] for q in QUERIES}, breaker_after=5)
    assert len(scraper.calls) == 5
    assert report.stopped == STOPPED_BREAKER
    assert len(report.skipped) == len(QUERIES) - 5
    assert [r["outcome"] for r in report.records].count("skipped") == len(QUERIES) - 5


def test_scattered_failures_do_not_trip_it():
    # Fail four in a row, succeed, fail four more, succeed: never five consecutive.
    script = {query.label: [[make_result()]] if index in (4, 9) else [SearchTimeoutError("flaky")]
              for index, query in enumerate(QUERIES)}
    report, scraper = run(script, breaker_after=5)
    assert report.stopped == ""
    assert len(scraper.calls) == len(QUERIES)


def test_no_breaker_means_every_search_is_attempted():
    report, scraper = run({q.label: [SearchTimeoutError("blocked")] for q in QUERIES}, breaker_after=None)
    assert len(scraper.calls) == len(QUERIES) and report.stopped == ""


def test_the_deadline_stops_new_searches():
    times = iter([0.0, 0.0, 0.0, 100.0] + [100.0] * 20)
    report, scraper = run({q.label: [[make_result()]] for q in QUERIES}, deadline=50.0, clock=lambda: next(times))
    assert len(scraper.calls) == 3
    assert report.stopped == STOPPED_DEADLINE
    assert len(report.skipped) == len(QUERIES) - 3


def test_a_shard_after_a_tripped_one_skips_itself(tmp_path, monkeypatch):
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "shard-0.json").write_text(json.dumps({"shard": "0/3", "stopped": "breaker"}))
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"routes": [{"origin": "MIA", "destination": "TYO", "nights": 16, "nonstop": False}]}))
    summary = tmp_path / "summary.json"
    monkeypatch.setattr(cli, "run_queries", lambda *a, **k: pytest.fail("must not search"))
    code = cli.main(["track", "--spec", str(spec), "--today", "2026-11-01", "--db", str(tmp_path / "f.duckdb"),
                     "--halt-if-tripped", str(prior), "--summary", str(summary), "--log-file", ""])
    data = json.loads(summary.read_text())
    assert code == 1
    assert data["stopped"] == "upstream-breaker"
    assert data["skipped"] == data["searches"] > 0 and data["succeeded"] == 0


def test_no_prior_summaries_means_no_halt(tmp_path):
    assert cli._upstream_tripped(str(tmp_path)) is False
    assert cli._upstream_tripped("") is False


@pytest.mark.parametrize("now, expected", [
    ("2026-10-26T10:00:00", "2026-10-27T05:07:00"),   # cron-bound: 30 min before tomorrow's 05:37
    ("2026-10-26T05:00:00", "2026-10-26T05:07:00"),   # today's 05:37 hasn't fired yet
])
def test_the_deadline_lands_before_the_next_scheduled_run(now, expected):
    moment = datetime.fromisoformat(now).replace(tzinfo=timezone.utc)
    assert cli.run_deadline("37 5 * * *", moment, 20).isoformat() == expected + "+00:00"


def test_a_manual_run_gets_the_maximum_hours():
    moment = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    assert cli.run_deadline("", moment, 20) == datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc)
