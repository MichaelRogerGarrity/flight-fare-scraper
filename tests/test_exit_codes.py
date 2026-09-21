import pytest

from flight_fare_scraper import cli
from flight_fare_scraper.config import build_query
from flight_fare_scraper.runner import BatchReport, SearchFailure, _described
from flight_fare_scraper.scrapers.base import BotBlockedError, SearchTimeoutError

QUERY = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false")


def report(timeouts=0, blocks=0):
    return BatchReport(failures=(
        [SearchFailure(QUERY, "SearchTimeoutError: ...", False)] * timeouts
        + [SearchFailure(QUERY, "BotBlockedError: ...", True)] * blocks
    ))


def test_a_clean_run_exits_zero():
    assert cli._track_exit_code(report(), tolerated=0) == 0


def test_failures_within_tolerance_exit_zero():
    assert cli._track_exit_code(report(timeouts=1), tolerated=3) == 0
    assert cli._track_exit_code(report(timeouts=3), tolerated=3) == 0


def test_failures_beyond_tolerance_exit_nonzero():
    assert cli._track_exit_code(report(timeouts=4), tolerated=3) == 1


def test_no_tolerance_by_default():
    assert cli._track_exit_code(report(timeouts=1), tolerated=0) == 1


def test_a_bot_block_fails_however_tolerant_the_run_is():
    # The whole point of the tolerance is to stay quiet about transient timeouts
    # without ever muffling a block.
    assert cli._track_exit_code(report(blocks=1), tolerated=100) == 1


@pytest.mark.parametrize("error", [
    SearchTimeoutError("route-abc123: no results response for page 3 within 45s"),
    BotBlockedError("route-abc123: redirected to https://www.kayak.com/help/bots.html"),
])
def test_described_puts_the_type_first_so_redaction_keeps_it(error, monkeypatch):
    """Scraper messages open with the route label, so redacting to the text before the
    first colon used to log the label twice and never say what failed."""
    from flight_fare_scraper import redact
    monkeypatch.setenv(redact.REDACT_ENV, "1")

    redacted = redact.error(_described(error))

    assert redacted == type(error).__name__
    assert "route-abc123" not in redacted
    assert "kayak" not in redacted
