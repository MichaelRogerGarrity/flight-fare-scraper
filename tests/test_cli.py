import duckdb
import pytest

from flight_fare_scraper import cli
from flight_fare_scraper.config import load_queries
from flight_fare_scraper.runner import BatchReport, SearchFailure
from tests.factories import make_result


@pytest.fixture(autouse=True)
def no_log_files(monkeypatch):
    monkeypatch.setattr(cli, "configure_logging", lambda verbose, log_file: None)


def test_track_records_the_successes_and_exits_nonzero_on_partial_failure(tmp_path, monkeypatch):
    config = tmp_path / "routes.csv"
    config.write_text("origin,destination,depart,return_date\nMIA,TYO,2026-11-27,2026-12-13\nMIA,BKK,2026-11-13,2026-11-22\n")
    tokyo, bangkok = load_queries(str(config))

    def fake_run(queries, headless, **options):
        return BatchReport(
            results=[make_result()],
            succeeded=[tokyo],
            failures=[SearchFailure(bangkok, "BotBlockedError: served /help/bots", bot_blocked=True)],
        )

    monkeypatch.setattr(cli, "run_queries", fake_run)
    db_path = tmp_path / "fares.duckdb"

    assert cli.main(["track", "--config", str(config), "--db", str(db_path)]) == 1

    con = duckdb.connect(str(db_path))
    try:
        assert con.execute("SELECT destination FROM fares").fetchall() == [("TYO",)]
    finally:
        con.close()


def test_invalid_config_exits_2_without_searching(tmp_path, monkeypatch):
    config = tmp_path / "routes.csv"
    config.write_text('origin,destination,depart,return_date,takeoff_window\nMIA,TYO,2026-11-27,2026-12-13,"1600,2300"\n')

    def must_not_run(queries, headless):
        raise AssertionError("searches should not run with an invalid config")

    monkeypatch.setattr(cli, "run_queries", must_not_run)

    assert cli.main(["batch", "--config", str(config), "--output", str(tmp_path / "out.csv")]) == 2
