from abc import ABC, abstractmethod
from typing import List, Optional

from ..models import FlightResult, SearchQuery


class ScraperError(Exception):
    """A search failed in a way that's worth retrying."""


class BotBlockedError(ScraperError):
    """The site detected automation and served its bot page instead of results."""


class CaptchaError(BotBlockedError):
    """The site answered with a CAPTCHA challenge instead of results.

    Unlike a one-off bot page, this is the site deciding the whole connection looks
    automated: retrying only adds to the evidence, so a run stops at the first one.
    """


class SearchTimeoutError(ScraperError):
    """Results never finished loading in time."""


class PaginationError(ScraperError):
    """A further page of results couldn't be loaded."""


class BaseScraper(ABC):
    """Common interface every site-specific scraper implements.

    A scraper owns its own browser lifecycle (enter/exit) so callers can
    reuse one browser instance across many searches via a `with` block.
    """

    name: str
    # Searches that returned only their first pages because a later one wouldn't load.
    truncated_searches: int = 0
    # Pages reached by clicking from inside the page because the button was covered.
    in_page_clicks: int = 0
    # Filled in while the most recent search runs, so a search that fails partway still
    # says how far it got. Per pass: {"main": {...}, "nonstop": {...}} plus totals.
    last_stats: Optional[dict] = None

    def __enter__(self) -> "BaseScraper":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass

    def reset(self) -> None:
        """Relaunch from scratch; called before retrying a failed search."""
        self.__exit__(None, None, None)
        self.__enter__()

    @abstractmethod
    def search(self, query: SearchQuery) -> List[FlightResult]:
        ...
