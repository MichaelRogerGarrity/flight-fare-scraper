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
) -> List[FlightResult]:
    """Retry one search, relaunching the browser between attempts; other searches are unaffected."""
    attempts = len(delays) + 1
    for attempt in range(1, attempts + 1):
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
                try:
                    results = search_with_retry(scraper, query, delays, sleep)
                except Exception as error:
                    if not isinstance(error, RETRYABLE_ERRORS):
                        logger.exception("%s: unexpected error (not retried)", redact.label(query))
                    report.failures.append(SearchFailure(
                        query, f"{type(error).__name__}: {error}", bot_blocked=isinstance(error, BotBlockedError),
                    ))
                    continue
                logger.info("%s: %d results", redact.label(query), len(results))
                report.results.extend(results)
                report.succeeded.append(query)
        finally:
            scraper.__exit__(None, None, None)

    _log_summary(len(queries), report)
    return report


def _log_summary(total: int, report: BatchReport) -> None:
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
