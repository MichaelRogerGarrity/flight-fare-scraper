"""Loading later result pages: the in-page click fallback, and keeping the pages
already in hand when a later one won't load."""

import csv
import json

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from flight_fare_scraper import cli, redact
from flight_fare_scraper.config import build_query
from flight_fare_scraper.runner import BatchReport
from flight_fare_scraper.scrapers.base import PaginationError, SearchTimeoutError
from flight_fare_scraper.scrapers.kayak import KayakScraper, _Pass
from tests.factories import make_result

QUERY = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false")


class FakePage:
    url = "https://www.example.com/flights/somewhere"

    def __init__(self):
        self.waits = 0

    def wait_for_timeout(self, _ms):
        self.waits += 1

    def evaluate(self, _script):
        return "en"


class FakeButton:
    def __init__(self, matches=1, click_times_out=True, covered=True):
        self.matches = matches
        self.click_times_out = click_times_out
        self.covered = covered
        self.in_page_clicks = 0
        self.ordinary_clicks = 0

    def click(self, timeout):
        self.ordinary_clicks += 1
        if self.click_times_out:
            raise PlaywrightTimeoutError("click timed out")

    def count(self):
        return self.matches

    def evaluate(self, script):
        if "getBoundingClientRect" in script:
            return {"visible": True, "covered": self.covered, "on_top": "div#consent.overlay"}
        self.in_page_clicks += 1
        return None


def scraper_with(button):
    scraper = KayakScraper(page_jitter_s=(0, 0))

    class Locator:
        first = button

    page = FakePage()
    page.get_by_text = lambda text, exact: Locator()
    return scraper, page


# --- the click itself ---------------------------------------------------------------

def test_an_ordinary_click_that_works_needs_no_fallback():
    button = FakeButton(click_times_out=False, covered=False)
    scraper, page = scraper_with(button)
    scraper._click_show_more(page, 2, "route-x")
    assert button.in_page_clicks == 0


def test_a_covered_button_is_clicked_from_inside_the_page_without_waiting():
    """An ordinary click waits for the button to be unobstructed, so a dialog drawn
    over it could only time out. Skipping it saved ~45 minutes a shard on runners
    that get the overlay on every page."""
    button = FakeButton(click_times_out=True, covered=True)
    scraper, page = scraper_with(button)
    scraper._click_show_more(page, 2, "route-x")
    assert button.in_page_clicks == 1
    assert button.ordinary_clicks == 0
    assert scraper.in_page_clicks == 1


def test_an_unclickable_button_that_looks_uncovered_still_falls_back():
    # The covered check is a guess; when it misses, the old path still recovers.
    button = FakeButton(click_times_out=True, covered=False)
    scraper, page = scraper_with(button)
    scraper._click_show_more(page, 2, "route-x")
    assert button.ordinary_clicks == 1 and button.in_page_clicks == 1


def test_only_the_first_in_page_click_warns(caplog):
    import logging
    button = FakeButton(click_times_out=True, covered=True)
    scraper, page = scraper_with(button)
    with caplog.at_level(logging.DEBUG, logger="flight_fare_scraper"):
        for number in range(2, 6):
            scraper._click_show_more(page, number, "route-x")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "in-page" in r.getMessage()]
    assert len(warnings) == 1
    assert scraper.in_page_clicks == 4


def test_a_missing_button_raises_with_facts_about_the_page():
    button = FakeButton(matches=0)
    scraper, page = scraper_with(button)
    with pytest.raises(PaginationError) as raised:
        scraper._click_show_more(page, 2, "route-x")
    message = str(raised.value)
    assert "page 2" in message
    assert "host=www.example.com" in message and "lang=en" in message and "matches=0" in message
    assert "/flights/" not in message  # host only, never the path


def test_page_facts_survive_redaction_without_leaking(monkeypatch):
    monkeypatch.setenv(redact.REDACT_ENV, "1")
    button = FakeButton(matches=0)
    scraper, page = scraper_with(button)
    with pytest.raises(PaginationError) as raised:
        scraper._click_show_more(page, 2, "route-x")
    shown = redact.error(f"PaginationError: {raised.value}")
    assert "matches=0" in shown
    assert "http" not in shown


# --- keeping what is in hand --------------------------------------------------------

def fetch_with(scraper, outcomes):
    """Run _fetch_pages where page N's outcome is outcomes[N]: a payload or an exception."""
    def click(page, number, label):
        if isinstance(outcomes[number], PaginationError):
            raise outcomes[number]

    def wait(page, polls, number, label):
        if isinstance(outcomes[number], Exception):
            raise outcomes[number]
        return outcomes[number]

    scraper._click_show_more = click
    scraper._wait_for_page = wait
    return scraper._fetch_pages(FakePage(), {}, {"page": 1}, max(outcomes), "route-x")


def test_all_pages_load():
    scraper = KayakScraper(page_jitter_s=(0, 0))
    payloads, truncated = fetch_with(scraper, {2: {"page": 2}, 3: {"page": 3}})
    assert [p["page"] for p in payloads] == [1, 2, 3]
    assert truncated is False


def test_a_stuck_button_keeps_page_one():
    """Results are price-sorted: page 1 alone holds the cheapest fares. Discarding it
    because page 2 wouldn't load recorded nothing at all for 47 searches in one run."""
    scraper = KayakScraper(page_jitter_s=(0, 0))
    payloads, truncated = fetch_with(scraper, {2: PaginationError("no button"), 3: {"page": 3}})
    assert [p["page"] for p in payloads] == [1]
    assert truncated is True


def test_a_later_page_that_never_answers_keeps_the_earlier_ones():
    scraper = KayakScraper(page_jitter_s=(0, 0))
    payloads, truncated = fetch_with(
        scraper, {2: {"page": 2}, 3: SearchTimeoutError("no results response for page 3"), 4: {"page": 4}})
    assert [p["page"] for p in payloads] == [1, 2]
    assert truncated is True


def test_a_truncated_search_is_counted_once_however_many_passes_were_cut_short():
    class Connected:
        def is_connected(self):
            return True

    scraper = KayakScraper(max_pages=20, pass_jitter_s=(0, 0))
    scraper._browser = Connected()
    cut = _Pass([(("a", "b", "X", 900.0), make_result(price=900.0))], 1200, 24, None, None, None, truncated=True)
    scraper._run_pass = lambda query, nonstop: cut
    rows = scraper.search(QUERY)
    assert rows, "a truncated search still returns the pages it got"
    assert scraper.truncated_searches == 1


# --- reporting it -------------------------------------------------------------------

def test_the_summary_reports_partial_searches():
    from argparse import Namespace
    report = BatchReport(succeeded=[QUERY], truncated=1)
    summary = cli.summarize(report, [QUERY], Namespace(run_id="1", shard="0/6", max_pages=5), 10, 1.0, "2026-10-01")
    assert summary["partial"] == 1


def test_runlog_upgrades_a_file_written_before_a_column_existed(tmp_path):
    out = tmp_path / "run-log.csv"
    old_fields = [name for name in cli.RUN_LOG_FIELDS if name != "partial"]
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=old_fields)
        writer.writeheader()
        writer.writerow({**{name: "" for name in old_fields},
                         "run_date": "2026-09-30", "run_id": "1", "shard": "0/6", "rows": "100",
                         "error_types": "PaginationError"})
    summaries = tmp_path / "summaries"
    summaries.mkdir()
    (summaries / "s.json").write_text(json.dumps(
        {"run_date": "2026-10-01", "run_id": "2", "shard": "0/6", "rows": 200, "partial": 3}))

    assert cli.main(["runlog", "--summaries", str(summaries), "--out", str(out)]) == 0

    with out.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        assert tuple(reader.fieldnames) == cli.RUN_LOG_FIELDS
    assert [row["rows"] for row in rows] == ["100", "200"]
    assert rows[0]["error_types"] == "PaginationError" and rows[0]["partial"] == ""
    assert rows[1]["partial"] == "3"
