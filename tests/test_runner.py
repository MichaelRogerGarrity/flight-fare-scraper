import logging

from flight_fare_scraper.config import build_query
from flight_fare_scraper.runner import run_queries
from flight_fare_scraper.scrapers.base import BaseScraper, BotBlockedError, SearchTimeoutError
from tests.factories import make_result

TOKYO = build_query("MIA", "TYO", "2026-11-27", "2026-12-13")
BANGKOK = build_query("MIA", "BKK", "2026-11-13", "2026-11-22")


class ScriptedScraper(BaseScraper):
    name = "scripted"

    def __init__(self, script):
        self.script = {label: list(outcomes) for label, outcomes in script.items()}
        self.calls = []
        self.resets = 0
        self.exited = False

    def __exit__(self, exc_type, exc, tb):
        self.exited = True

    def reset(self):
        self.resets += 1

    def search(self, query):
        self.calls.append(query.label)
        outcome = self.script[query.label].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def run(scraper, queries, sleeps):
    return run_queries(queries, delays=(1, 2), sleep=sleeps.append,
                       scraper_for=lambda site: lambda headless: scraper, search_jitter_s=None)


def test_bot_block_is_retried_after_relaunching_the_browser():
    result = make_result()
    scraper = ScriptedScraper({TOKYO.label: [BotBlockedError("served /help/bots"), [result]]})
    sleeps = []

    report = run(scraper, [TOKYO], sleeps)

    assert report.results == [result]
    assert report.failures == []
    assert scraper.resets == 1
    assert sleeps == [3]  # the first delay, tripled because it was a bot block
    assert scraper.exited


def test_a_search_that_exhausts_its_retries_does_not_stop_the_batch(caplog):
    bangkok_result = make_result(destination="BKK", depart_date="2026-11-13", return_date="2026-11-22")
    scraper = ScriptedScraper({
        TOKYO.label: [SearchTimeoutError("1"), SearchTimeoutError("2"), SearchTimeoutError("3")],
        BANGKOK.label: [[bangkok_result]],
    })
    sleeps = []

    with caplog.at_level(logging.INFO, logger="flight_fare_scraper"):
        report = run(scraper, [TOKYO, BANGKOK], sleeps)

    assert scraper.calls == [TOKYO.label] * 3 + [BANGKOK.label]
    assert sleeps == [1, 2]
    assert report.succeeded == [BANGKOK]
    assert report.results == [bangkok_result]
    assert [failure.query for failure in report.failures] == [TOKYO]
    assert report.failures[0].bot_blocked is False
    assert "PARTIAL_BATCH_FAILURE" in caplog.text


def test_non_retryable_errors_fail_fast():
    scraper = ScriptedScraper({TOKYO.label: [KeyError("results")]})
    sleeps = []

    report = run(scraper, [TOKYO], sleeps)

    assert scraper.calls == [TOKYO.label]
    assert sleeps == []
    assert scraper.resets == 0
    assert "KeyError" in report.failures[0].error


def test_a_scraper_that_cannot_start_fails_its_searches_without_crashing(caplog):
    def broken_site(site):
        def factory(headless):
            raise RuntimeError("chrome not installed")
        return factory

    with caplog.at_level(logging.INFO, logger="flight_fare_scraper"):
        report = run_queries([TOKYO, BANGKOK], delays=(1,), sleep=lambda seconds: None, scraper_for=broken_site)

    assert [failure.query for failure in report.failures] == [TOKYO, BANGKOK]
    assert "chrome not installed" in report.failures[0].error
    assert "BATCH_FAILED" in caplog.text


def test_consecutive_searches_are_separated_by_a_random_pause():
    scraper = ScriptedScraper({TOKYO.label: [[]], BANGKOK.label: [[]]})
    sleeps = []

    run_queries([TOKYO, BANGKOK], delays=(1, 2), sleep=sleeps.append,
                scraper_for=lambda site: lambda headless: scraper, search_jitter_s=(5.0, 20.0))

    assert len(sleeps) == 1  # between the two searches, not before the first
    assert 5.0 <= sleeps[0] <= 20.0
