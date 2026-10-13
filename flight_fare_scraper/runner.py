import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from playwright.sync_api import Error as PlaywrightError

from . import redact
from .models import FlightResult, SearchQuery
from .scrapers import get_scraper_class
from .scrapers.base import BaseScraper, BotBlockedError, ScraperError

logger = logging.getLogger(__name__)

RETRY_DELAYS_S = (15, 60)  # wait before attempts 2 and 3
SEARCH_JITTER_S = (5.0, 20.0)  # random pause between consecutive searches
BOT_BLOCK_DELAY_MULTIPLIER = 3
RETRYABLE_ERRORS = (ScraperError, PlaywrightError)


@dataclass
class SearchFailure:
    query: SearchQuery
    error: str
    bot_blocked: bool


@dataclass
class BatchReport:
    results: List[FlightResult] = field(default_factory=list)
    succeeded: List[SearchQuery] = field(default_factory=list)
    failures: List[SearchFailure] = field(default_factory=list)
    truncated: int = 0  # searches that succeeded with only their first pages
    in_page_clicks: int = 0  # pages reached by clicking past something covering the button
    # One flat record per search: what was searched, how it went, how long it took.
    # Carries real routes and full error text, so it is for private storage only --
    # never the console or anything uploaded with the public run.
    records: List[dict] = field(default_factory=list)


def _described(error: BaseException) -> str:
    """Name the exception type first.

    Scraper messages start with the route label, so redaction -- which keeps only
    the text before the first colon -- would otherwise report the label again and
    say nothing about what went wrong.
    """
    return f"{type(error).__name__}: {error}"


def search_with_retry(
    scraper: BaseScraper,
    query: SearchQuery,
    delays: Sequence[float] = RETRY_DELAYS_S,
    sleep: Callable[[float], None] = time.sleep,
    record: Optional[dict] = None,
) -> List[FlightResult]:
    """Retry one search, relaunching the browser between attempts; other searches are unaffected.

    `record`, if given, gets the number of attempts made.
    """
    attempts = len(delays) + 1
    for attempt in range(1, attempts + 1):
        if record is not None:
            record["attempts"] = attempt
        try:
            if attempt > 1:
                scraper.reset()
            return scraper.search(query)
        except RETRYABLE_ERRORS as error:
            blocked = isinstance(error, BotBlockedError)
            tag = "BOT_BLOCKED" if blocked else "SEARCH_FAILED"
            if attempt == attempts:
                logger.error("%s %s: attempt %d/%d failed, giving up: %s",
                             tag, redact.label(query), attempt, attempts, redact.error(_described(error)))
                raise
            delay = delays[attempt - 1] * (BOT_BLOCK_DELAY_MULTIPLIER if blocked else 1)
            logger.warning(
                "%s %s: attempt %d/%d failed: %s -- relaunching browser and retrying in %ss",
                tag, redact.label(query), attempt, attempts, redact.error(_described(error)), delay,
            )
            sleep(delay)
    raise AssertionError("unreachable")


def run_queries(
    queries: List[SearchQuery],
    headless: bool = True,
    delays: Sequence[float] = RETRY_DELAYS_S,
    sleep: Callable[[float], None] = time.sleep,
    scraper_for: Callable[[str], Callable[..., BaseScraper]] = get_scraper_class,
    search_jitter_s: Optional[Tuple[float, float]] = SEARCH_JITTER_S,
    max_pages: Optional[int] = None,
) -> BatchReport:
    report = BatchReport()
    searched_any = False
    by_site: Dict[str, List[SearchQuery]] = {}
    for query in queries:
        by_site.setdefault(query.site, []).append(query)

    for site, site_queries in by_site.items():
        try:
            options = {} if max_pages is None else {"max_pages": max_pages}
            scraper = scraper_for(site)(headless=headless, **options)
            scraper.__enter__()
        except Exception as error:
            logger.exception("couldn't start the %s scraper; failing its %d searches", site, len(site_queries))
            report.failures.extend(
                SearchFailure(query, f"scraper startup failed: {error}", bot_blocked=False) for query in site_queries
            )
            continue

        try:
            for query in site_queries:
                if searched_any and search_jitter_s:
                    pause = random.uniform(*search_jitter_s)
                    logger.debug("pausing %.1fs before the next search", pause)
                    sleep(pause)
                searched_any = True
                logger.info("searching %s: %s", site, redact.label(query))
                attempt_info: dict = {}
                started = time.monotonic()
                clicks_before = getattr(scraper, "in_page_clicks", 0)
                error: Optional[BaseException] = None
                try:
                    results = search_with_retry(scraper, query, delays, sleep, record=attempt_info)
                except Exception as caught:
                    error = caught
                    if not isinstance(caught, RETRYABLE_ERRORS):
                        logger.exception("%s: unexpected error (not retried)", redact.label(query))
                    report.failures.append(SearchFailure(
                        query, f"{type(caught).__name__}: {caught}", bot_blocked=isinstance(caught, BotBlockedError),
                    ))
                finally:
                    report.records.append(search_record(
                        query, scraper, attempt_info.get("attempts", 0), time.monotonic() - started,
                        getattr(scraper, "in_page_clicks", 0) - clicks_before, error,
                    ))
                if error is not None:
                    continue
                logger.info("%s: %d results", redact.label(query), len(results))
                report.results.extend(results)
                report.succeeded.append(query)
        finally:
            report.truncated += getattr(scraper, "truncated_searches", 0)
            report.in_page_clicks += getattr(scraper, "in_page_clicks", 0)
            scraper.__exit__(None, None, None)

    _log_summary(len(queries), report)
    return report


# The shape of a search record, with fixed types. Inferred types drift between files --
# a column that is empty in one run comes out INTEGER, and VARCHAR in the next -- and
# files whose types disagree are a nuisance to read together.
SEARCH_RECORD_TYPES = {
    "site": "VARCHAR", "origin": "VARCHAR", "destination": "VARCHAR",
    "depart_date": "DATE", "return_date": "DATE", "nonstop_only": "BOOLEAN",
    "outcome": "VARCHAR", "attempts": "INTEGER", "seconds": "DOUBLE",
    "error_type": "VARCHAR", "error_message": "VARCHAR",
    "first_page_seconds": "DOUBLE", "pages_fetched": "INTEGER", "pages_available": "INTEGER",
    "results_reported": "INTEGER", "nonstop_pass": "BOOLEAN",
    "nonstop_first_page_seconds": "DOUBLE", "nonstop_pages_fetched": "INTEGER",
    "nonstop_pages_available": "INTEGER", "truncated": "BOOLEAN",
    "ignored_polls": "INTEGER", "unreadable_polls": "INTEGER",
    "rows_kept": "INTEGER", "rows_dropped_by_filters": "INTEGER", "in_page_clicks": "INTEGER",
}


def search_record(query: SearchQuery, scraper: BaseScraper, attempts: int, seconds: float,
                  in_page_clicks: int, error: Optional[BaseException]) -> dict:
    """Flatten one search's outcome and the scraper's stats into a single row."""
    stats = getattr(scraper, "last_stats", None) or {}
    main = stats.get("main") or {}
    nonstop = stats.get("nonstop") or {}
    truncated = bool(main.get("truncated") or nonstop.get("truncated"))
    return {
        "site": query.site,
        "origin": query.origin,
        "destination": query.destination,
        "depart_date": query.depart_date,
        "return_date": query.return_date,
        "nonstop_only": query.nonstop_only,
        "outcome": "failed" if error is not None else "partial" if truncated else "ok",
        "attempts": attempts,
        "seconds": round(seconds, 1),
        "error_type": type(error).__name__ if error is not None else None,
        "error_message": str(error) if error is not None else None,
        "first_page_seconds": main.get("first_page_seconds"),
        "pages_fetched": main.get("pages_fetched"),
        "pages_available": main.get("pages_available"),
        "results_reported": main.get("results_reported"),
        "nonstop_pass": bool(nonstop),
        "nonstop_first_page_seconds": nonstop.get("first_page_seconds"),
        "nonstop_pages_fetched": nonstop.get("pages_fetched"),
        "nonstop_pages_available": nonstop.get("pages_available"),
        "truncated": truncated,
        "ignored_polls": main.get("ignored_polls", 0) + nonstop.get("ignored_polls", 0),
        "unreadable_polls": main.get("unreadable_polls", 0) + nonstop.get("unreadable_polls", 0),
        "rows_kept": stats.get("rows_kept") if error is None else 0,
        "rows_dropped_by_filters": stats.get("rows_dropped_by_filters"),
        "in_page_clicks": in_page_clicks,
    }


def _log_summary(total: int, report: BatchReport) -> None:
    if report.in_page_clicks:
        logger.info("%d page(s) loaded by clicking past something covering the button",
                    report.in_page_clicks)
    if report.truncated:
        logger.warning("PARTIAL_RESULTS: %d of %d searches kept only their first pages "
                       "because a later page wouldn't load", report.truncated, total)
    if not report.failures:
        logger.info("batch complete: %d/%d searches succeeded, %d rows", total, total, len(report.results))
        return
    tag = "PARTIAL_BATCH_FAILURE" if report.succeeded else "BATCH_FAILED"
    blocked = sum(failure.bot_blocked for failure in report.failures)
    logger.error(
        "%s: %d/%d searches failed (%d bot-blocked); %d rows from the %d that succeeded",
        tag, len(report.failures), total, blocked, len(report.results), len(report.succeeded),
    )
    for failure in report.failures:
        logger.error("  failed: %s -- %s", redact.label(failure.query), redact.error(failure.error))
