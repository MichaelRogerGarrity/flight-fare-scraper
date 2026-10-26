"""Saving each search as it finishes, and publishing in parts, so a run cut short keeps its work."""

import json
from datetime import date, datetime, timedelta

import duckdb
import pytest

from flight_fare_scraper import checkpoint, cli, db
from flight_fare_scraper.checkpoint import Checkpoint
from flight_fare_scraper.config import build_query
from flight_fare_scraper.runner import STOPPED_INTERRUPTED, BatchReport, run_queries, search_record
from flight_fare_scraper.scrapers.base import SearchTimeoutError
from tests.factories import make_result
from tests.test_runner import ScriptedScraper

DAY = date(2026, 11, 1)
QUERIES = [build_query("MIA", "TYO", f"2026-12-{day:02d}", f"2026-12-{day + 16:02d}", nonstop="false")
           for day in range(1, 8)]


def _result(query):
    return make_result(depart_date=str(query.depart_date), return_date=str(query.return_date))


def _record(query, error=None):
    return search_record(query, None, 1, 40.0, 0, error)


class FakeTarget(checkpoint.Target):
    """Keeps what each part held, by key, in place of a bucket."""

    def __init__(self, fail_times=0):
        self.objects = {}
        self.puts = []
        self.fail_times = fail_times

    def put(self, con, tables):
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("bucket unreachable")
        self.puts.append([key for _, key in tables])
        for table, key in tables:
            self.objects[key] = con.execute(f"SELECT * FROM {table}").fetchdf()

    def fares(self):
        return [row for key, frame in self.objects.items() if key.startswith("snapshots/")
                for row in frame.to_dict("records")]

    def records(self):
        return [row for key, frame in self.objects.items() if key.startswith("diagnostics/")
                for row in frame.to_dict("records")]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _saver(con, target, run_id="run-1", every=3, minutes=30.0, clock=None):
    return Checkpoint(con, DAY, run_id, None, target, every=every, minutes=minutes,
                      clock=clock or Clock(), now=lambda: datetime(2026, 11, 1, 9, 0))


def test_each_search_is_in_the_database_as_soon_as_it_finishes(tmp_path):
    con = db.connect(str(tmp_path / "fares.duckdb"))
    saver = _saver(con, None)
    saver.add(QUERIES[0], [_result(QUERIES[0])], _record(QUERIES[0]))
    assert con.execute("SELECT count(*) FROM fares").fetchone()[0] == 1


def test_parts_go_up_every_n_searches_and_the_rest_at_the_end():
    con = db.connect(":memory:")
    target = FakeTarget()
    saver = _saver(con, target, every=3)
    for query in QUERIES:
        saver.add(query, [_result(query)], _record(query))
    assert len(target.puts) == 2  # after searches 3 and 6
    saver.close()
    assert len(target.puts) == 3
    assert len(target.fares()) == 7 and len(target.records()) == 7
    assert saver.pending() == 0


def test_a_part_also_goes_up_after_enough_minutes():
    con = db.connect(":memory:")
    target, clock = FakeTarget(), Clock()
    saver = _saver(con, target, every=100, minutes=30, clock=clock)
    saver.add(QUERIES[0], [_result(QUERIES[0])], _record(QUERIES[0]))
    assert target.puts == []
    clock.now = 31 * 60
    saver.add(QUERIES[1], [_result(QUERIES[1])], _record(QUERIES[1]))
    assert len(target.puts) == 1


def test_a_failed_upload_stays_queued_and_goes_with_the_next_part():
    con = db.connect(":memory:")
    target = FakeTarget(fail_times=1)
    saver = _saver(con, target, every=2)
    for query in QUERIES[:4]:
        saver.add(query, [_result(query)], _record(query))
    assert len(target.puts) == 1  # the first part failed; the second carried both
    assert len(target.fares()) == 4


def test_a_failed_final_upload_raises_without_naming_routes():
    con = db.connect(":memory:")
    saver = _saver(con, FakeTarget(fail_times=5), every=100)
    saver.add(QUERIES[0], [_result(QUERIES[0])], _record(QUERIES[0]))
    with pytest.raises(cli.storage.StorageError) as caught:
        saver.close()
    assert "ConnectionError" in str(caught.value) and "MIA" not in str(caught.value)


def test_what_a_killed_run_never_published_goes_up_with_the_next_run(tmp_path):
    path = str(tmp_path / "home.duckdb")
    con = db.connect(path)
    killed = _saver(con, FakeTarget(), run_id="run-1", every=100)
    for query in QUERIES[:2]:
        killed.add(query, [_result(query)], _record(query))
    con.close()  # no close(): the machine went off

    con = db.connect(path)
    target = FakeTarget()
    later = _saver(con, target, run_id="run-2", every=100)
    later.add(QUERIES[2], [_result(QUERIES[2])], _record(QUERIES[2]))
    later.close()
    assert len(target.fares()) == 3
    # The leftovers keep the name of the run that saw them.
    assert any("run-1-" in key for key in target.objects) and any("run-2-" in key for key in target.objects)


def test_publishing_the_same_items_again_overwrites_rather_than_duplicates():
    """The upload went through, then the run died before clearing its queue."""
    con = db.connect(":memory:")
    target = FakeTarget()
    saver = _saver(con, target, every=100)
    saver.add(QUERIES[0], [_result(QUERIES[0])], _record(QUERIES[0]))
    queued = {t: con.execute(f"SELECT * FROM {t}").fetchdf()
              for t in (checkpoint.FARES_QUEUE, checkpoint.RECORDS_QUEUE)}
    saver.flush()
    for table, frame in queued.items():  # as if the delete never happened
        con.register("restore", frame)
        con.execute(f"INSERT INTO {table} SELECT * FROM restore")
        con.unregister("restore")
    saver.flush()
    assert len(target.puts) == 2 and target.puts[0] == target.puts[1]
    assert len(target.fares()) == 1


def test_each_day_gets_its_own_part():
    con = db.connect(":memory:")
    target = FakeTarget()
    first = _saver(con, target, every=100)
    first.add(QUERIES[0], [_result(QUERIES[0])], _record(QUERIES[0]))
    second = Checkpoint(con, DAY + timedelta(days=1), "run-2", None, target, every=100)
    second.add(QUERIES[1], [_result(QUERIES[1])], _record(QUERIES[1]))
    second.close()
    days = sorted({key.split("/")[1] for key in target.objects})
    assert days == ["snapshot_date=2026-11-01", "snapshot_date=2026-11-02"]


def test_searches_never_reached_are_published_as_records():
    con = db.connect(":memory:")
    target = FakeTarget()
    saver = _saver(con, target)
    saver.add_records([search_record(QUERIES[0], None, 0, 0.0, 0, None, outcome="skipped")])
    saver.close()
    assert [r["outcome"] for r in target.records()] == ["skipped"]
    assert target.fares() == []


def test_done_today_names_the_searches_already_recorded():
    con = db.connect(":memory:")
    saver = _saver(con, None)
    saver.add(QUERIES[0], [_result(QUERIES[0])], _record(QUERIES[0]))
    saver.add(QUERIES[1], [], _record(QUERIES[1], SearchTimeoutError("x")))  # failed: not done
    assert saver.done_today() == {checkpoint.query_key(QUERIES[0])}


# --- the runner -------------------------------------------------------------------------

def test_the_runner_hands_over_each_search_as_it_finishes():
    seen = []
    scraper = ScriptedScraper({q.label: [[_result(q)]] for q in QUERIES[:2]})
    run_queries(QUERIES[:2], sleep=lambda _s: None, scraper_for=lambda site: lambda headless: scraper,
                search_jitter_s=None, on_search=lambda q, results, record: seen.append((q.label, len(results),
                                                                                       record["outcome"])))
    assert seen == [(QUERIES[0].label, 1, "ok"), (QUERIES[1].label, 1, "ok")]


def test_an_interrupt_mid_search_stops_the_run_and_keeps_what_finished():
    seen = []
    scraper = ScriptedScraper({QUERIES[0].label: [[_result(QUERIES[0])]], QUERIES[1].label: [KeyboardInterrupt()],
                               QUERIES[2].label: [[_result(QUERIES[2])]]})
    report = run_queries(QUERIES[:3], sleep=lambda _s: None, scraper_for=lambda site: lambda headless: scraper,
                         search_jitter_s=None, on_search=lambda q, results, record: seen.append(q.label))
    assert report.stopped == STOPPED_INTERRUPTED
    assert seen == [QUERIES[0].label]
    assert [q.label for q in report.skipped] == [QUERIES[1].label, QUERIES[2].label]
    # The interrupted search is recorded once, as skipped -- not as a success.
    assert [r["outcome"] for r in report.records] == ["ok", "skipped", "skipped"]
    assert scraper.exited


def test_an_interrupt_during_a_pause_skips_from_the_next_search():
    def sleep(_seconds):
        raise KeyboardInterrupt
    scraper = ScriptedScraper({q.label: [[_result(q)]] for q in QUERIES[:3]})
    report = run_queries(QUERIES[:3], sleep=sleep, scraper_for=lambda site: lambda headless: scraper,
                         search_jitter_s=(1.0, 2.0))
    assert report.stopped == STOPPED_INTERRUPTED
    assert [r["outcome"] for r in report.records] == ["ok", "skipped", "skipped"]


# --- end to end ---------------------------------------------------------------------------

def _spec(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"routes": [{"origin": "MIA", "destination": "TYO", "nights": 16, "nonstop": False}]}))
    return str(path)


def test_resume_runs_only_the_searches_not_yet_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "configure_logging", lambda verbose, log_file: None)
    searched = []

    def fake_run(queries, **options):
        searched.append(len(queries))
        report = BatchReport()
        for query in queries[:2]:  # cut short after two
            record = _record(query)
            report.records.append(record)
            options["on_search"](query, [_result(query)], record)
        report.stopped = STOPPED_INTERRUPTED
        return report

    monkeypatch.setattr(cli, "run_queries", fake_run)
    argv = ["track", "--spec", _spec(tmp_path), "--today", "2026-11-01", "--snapshot-date", "2026-11-01",
            "--db", str(tmp_path / "home.duckdb"), "--resume"]
    assert cli.main(argv) == 1  # stopped early
    assert cli.main(argv) == 1
    assert searched[1] == searched[0] - 2
    con = duckdb.connect(str(tmp_path / "home.duckdb"))
    assert con.execute("SELECT count(*) FROM fares").fetchone()[0] == 4


def test_an_interrupted_run_still_publishes_and_writes_its_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "configure_logging", lambda verbose, log_file: None)
    target = FakeTarget()
    monkeypatch.setattr(cli, "_publish_target", lambda con, args: target)

    def fake_run(queries, **options):
        report = BatchReport(stopped=STOPPED_INTERRUPTED)
        record = _record(queries[0])
        report.records.append(record)
        options["on_search"](queries[0], [_result(queries[0])], record)
        return report

    monkeypatch.setattr(cli, "run_queries", fake_run)
    summary = tmp_path / "summary.json"
    code = cli.main(["track", "--spec", _spec(tmp_path), "--today", "2026-11-01", "--snapshot-date", "2026-11-01",
                     "--db", str(tmp_path / "x.duckdb"), "--summary", str(summary)])
    assert code == 1
    assert len(target.fares()) == 1
    assert json.loads(summary.read_text())["stopped"] == STOPPED_INTERRUPTED
