"""A CAPTCHA counts as a block: not retried, it stops the run, and leaves an alert."""

import json
from datetime import datetime

import pytest

from flight_fare_scraper import cli
from flight_fare_scraper.config import build_query
from flight_fare_scraper.runner import STOPPED_CAPTCHA, BatchReport, SearchFailure, run_queries, search_record
from flight_fare_scraper.scrapers.base import CaptchaError, SearchTimeoutError
from flight_fare_scraper.scrapers.kayak import _CHALLENGED, KayakScraper, is_challenge
from tests.factories import make_result
from tests.test_runner import ScriptedScraper

QUERIES = [build_query("MIA", "TYO", f"2026-12-{day:02d}", f"2026-12-{day + 16:02d}", nonstop="false")
           for day in range(1, 5)]
# The keys of the responses Kayak sent from 2026-10-26 21:40 (values made up).
CHALLENGE = {"captchaRedirectUrl": "/help/bots?x", "errors": [], "url": "/flights/x"}


class FakePage:
    url = "https://www.example.com/flights/somewhere"

    def wait_for_timeout(self, _ms):
        pass


def test_a_challenge_response_is_recognised():
    assert is_challenge(CHALLENGE)
    assert not is_challenge({"status": "complete", "results": []})
    assert not is_challenge(["not", "a", "dict"])


def test_waiting_for_a_page_ends_at_once_on_a_challenge():
    scraper = KayakScraper()
    with pytest.raises(CaptchaError):
        scraper._wait_for_page(FakePage(), {_CHALLENGED: [CHALLENGE]}, 1, "route-x")


def _run(scraper, queries=QUERIES, breaker_after=5):
    sleeps = []
    report = run_queries(queries, delays=(15, 60), sleep=sleeps.append,
                         scraper_for=lambda site: lambda headless: scraper, search_jitter_s=None,
                         breaker_after=breaker_after)
    return report, sleeps


def test_a_captcha_is_not_retried_and_stops_the_run():
    scraper = ScriptedScraper({QUERIES[0].label: [[make_result()]], QUERIES[1].label: [CaptchaError("challenged")],
                               QUERIES[2].label: [[make_result()]], QUERIES[3].label: [[make_result()]]})
    report, sleeps = _run(scraper)
    assert scraper.calls == [QUERIES[0].label, QUERIES[1].label]  # one attempt, then nothing more
    assert sleeps == []  # no retry delays
    assert report.stopped == STOPPED_CAPTCHA
    assert [q.label for q in report.skipped] == [QUERIES[2].label, QUERIES[3].label]
    assert report.failures[0].bot_blocked  # counted as a block


def test_a_captcha_on_the_last_search_still_marks_the_run():
    scraper = ScriptedScraper({QUERIES[0].label: [CaptchaError("challenged")]})
    report, _ = _run(scraper, queries=QUERIES[:1])
    assert report.stopped == STOPPED_CAPTCHA and report.skipped == []


def test_ordinary_timeouts_still_get_retried():
    scraper = ScriptedScraper({QUERIES[0].label: [SearchTimeoutError("slow"), [make_result()]]})
    report, sleeps = _run(scraper, queries=QUERIES[:1])
    assert sleeps == [15] and report.stopped == ""


def test_later_shards_skip_after_a_captcha(tmp_path):
    (tmp_path / "shard-0.json").write_text(json.dumps({"stopped": STOPPED_CAPTCHA}))
    assert cli._upstream_tripped(str(tmp_path))


# --- alerts ---------------------------------------------------------------------------

AT = datetime(2026, 10, 26, 21, 40)


def test_alert_messages_are_short_and_name_no_routes():
    captcha = BatchReport(succeeded=QUERIES[:1], stopped=STOPPED_CAPTCHA)
    breaker = BatchReport(succeeded=QUERIES[:2], stopped="breaker")
    blocked = BatchReport(succeeded=QUERIES[:3],
                          failures=[SearchFailure(QUERIES[3], "BotBlockedError: x", bot_blocked=True)])
    messages = [cli.alert_message(report, QUERIES, AT) for report in (captcha, breaker, blocked)]
    assert messages[0] == "Fare tracking stopped at 9:40 PM: Kayak answered with a CAPTCHA after 1 of 4 searches."
    assert "5 searches failed in a row after 2 of 4" in messages[1]
    assert "1 search(es) hit Kayak's bot page" in messages[2]
    assert all(len(m) < 200 and "MIA" not in m and "TYO" not in m for m in messages)


def test_a_good_run_leaves_no_alert():
    assert cli.alert_message(BatchReport(succeeded=QUERIES), QUERIES, AT) is None


def _spec(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"routes": [{"origin": "MIA", "destination": "TYO", "nights": 16, "nonstop": False}]}))
    return str(path)


def test_a_captcha_run_leaves_an_alert_that_is_printed_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "configure_logging", lambda verbose, log_file: None)

    def fake_run(queries, **options):
        record = search_record(queries[0], None, 1, 3.0, 0, CaptchaError("challenged"))
        return BatchReport(failures=[SearchFailure(queries[0], "CaptchaError: challenged", bot_blocked=True)],
                           records=[record], stopped=STOPPED_CAPTCHA)

    monkeypatch.setattr(cli, "run_queries", fake_run)
    alerts = tmp_path / "alerts"
    assert cli.main(["track", "--spec", _spec(tmp_path), "--today", "2026-11-01", "--db", str(tmp_path / "x.duckdb"),
                     "--alert-dir", str(alerts)]) == 1
    assert len(list(alerts.glob("*.json"))) == 1
    capsys.readouterr()

    assert cli.main(["alerts", "--dir", str(alerts)]) == 0
    printed = capsys.readouterr().out.strip().splitlines()
    assert len(printed) == 1 and "CAPTCHA" in printed[0]
    assert cli.main(["alerts", "--dir", str(alerts)]) == 0
    assert capsys.readouterr().out == ""  # already sent
    assert len(list((alerts / "sent").glob("*.json"))) == 1
