import csv
import json
from argparse import Namespace

import pytest

from flight_fare_scraper import cli
from flight_fare_scraper.config import build_query
from flight_fare_scraper.runner import BatchReport, SearchFailure

QUERY = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false")


def summary_args(**overrides):
    return Namespace(**{"run_id": "999", "shard": "0/3", "max_pages": 5, **overrides})


def write_summaries(directory, rows):
    directory.mkdir(parents=True, exist_ok=True)
    for index, row in enumerate(rows):
        (directory / f"s{index}.json").write_text(json.dumps(row), encoding="utf-8")


def read_log(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_summary_counts_successes_failures_and_blocks():
    report = BatchReport(
        results=[object(), object()],
        succeeded=[QUERY],
        failures=[
            SearchFailure(QUERY, "BotBlockedError: redirected to https://kayak.com/help/bots.html", True),
            SearchFailure(QUERY, "SearchTimeoutError: MIA-TYO: no results response", False),
        ],
    )
    summary = cli.summarize(report, [QUERY] * 3, summary_args(), 4100, 61.4, "2026-09-19")

    assert summary["searches"] == 3
    assert summary["succeeded"] == 1
    assert summary["failed"] == 2
    assert summary["bot_blocked"] == 1
    assert summary["rows"] == 4100
    assert summary["seconds"] == 61


def test_summary_leaks_neither_routes_nor_error_text():
    report = BatchReport(failures=[
        SearchFailure(QUERY, "BotBlockedError: redirected to https://kayak.com/help/bots.html", True),
    ])
    summary = cli.summarize(report, [QUERY], summary_args(), 0, 1.0, "2026-09-19")

    assert summary["error_types"] == "BotBlockedError"
    blob = json.dumps(summary)
    for leak in ("MIA", "TYO", "kayak.com", "http", "2026-11-27"):
        assert leak not in blob


def test_summary_reports_no_error_types_on_a_clean_run():
    summary = cli.summarize(BatchReport(succeeded=[QUERY]), [QUERY], summary_args(), 10, 1.0, "2026-09-19")
    assert summary["error_types"] == ""
    assert summary["failed"] == 0


def test_runlog_writes_a_header_once_and_appends(tmp_path):
    write_summaries(tmp_path / "day1", [
        {"run_date": "2026-09-19", "run_id": "1", "shard": "0/3", "rows": 10},
        {"run_date": "2026-09-19", "run_id": "1", "shard": "1/3", "rows": 20},
    ])
    out = tmp_path / "run-log.csv"
    assert cli.main(["runlog", "--summaries", str(tmp_path / "day1"), "--out", str(out)]) == 0

    write_summaries(tmp_path / "day2", [{"run_date": "2026-09-20", "run_id": "2", "shard": "0/3", "rows": 30}])
    assert cli.main(["runlog", "--summaries", str(tmp_path / "day2"), "--out", str(out)]) == 0

    rows = read_log(out)
    assert [row["run_date"] for row in rows] == ["2026-09-19", "2026-09-19", "2026-09-20"]
    assert out.read_text(encoding="utf-8").count("run_date,run_id") == 1


def test_runlog_is_idempotent(tmp_path):
    write_summaries(tmp_path / "day1", [{"run_date": "2026-09-19", "run_id": "1", "shard": "0/3", "rows": 10}])
    out = tmp_path / "run-log.csv"
    for _ in range(3):
        assert cli.main(["runlog", "--summaries", str(tmp_path / "day1"), "--out", str(out)]) == 0
    assert len(read_log(out)) == 1


def test_runlog_fills_missing_fields_rather_than_failing(tmp_path):
    write_summaries(tmp_path / "day1", [{"run_date": "2026-09-19", "run_id": "1", "shard": "0/3"}])
    out = tmp_path / "run-log.csv"
    assert cli.main(["runlog", "--summaries", str(tmp_path / "day1"), "--out", str(out)]) == 0
    assert read_log(out)[0]["rows"] == ""


def test_runlog_reports_an_empty_directory(tmp_path, caplog):
    (tmp_path / "empty").mkdir()
    assert cli.main(["runlog", "--summaries", str(tmp_path / "empty"), "--out", str(tmp_path / "log.csv")]) == 2


@pytest.mark.parametrize("field", cli.RUN_LOG_FIELDS)
def test_every_logged_field_comes_from_summarize(field):
    summary = cli.summarize(BatchReport(), [], summary_args(), 0, 0.0, "2026-09-19")
    assert field in summary
