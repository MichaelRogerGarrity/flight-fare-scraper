from abc import ABC, abstractmethod
from typing import List

from ..models import FlightResult, SearchQuery


class ScraperError(Exception):
    """A search failed in a way that's worth retrying."""


class BotBlockedError(ScraperError):
    """The site detected automation and served its bot page instead of results."""


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
