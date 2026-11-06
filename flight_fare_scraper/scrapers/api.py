"""The shared part of every source reached through a paid API.

A subclass names its key's environment variable and implements `itineraries` using
`request` (http_source.py). This class adds what every API needs alike: reading the
key, and reading HTTP failures as key or quota problems. An API whose key is missing,
rejected or out of quota raises SourceUnavailableError, and the runner skips the rest
of that source's searches for the run.

Keys come only from the environment and never appear in a log or an error message.
"""

import os
from typing import List

from ..models import FlightResult, SearchQuery
from .base import ScraperError, SourceUnavailableError
from .http_source import HttpSource


class ApiScraper(HttpSource):
    # Paid APIs meter themselves; the run's hourly cap is for sites we read directly.
    polite = False
    key_env: str = ""  # environment variable holding the API key

    def __enter__(self) -> "ApiScraper":
        self.api_key = os.environ.get(self.key_env, "").strip()
        return self

    def search(self, query: SearchQuery) -> List[FlightResult]:
        if not getattr(self, "api_key", ""):
            raise SourceUnavailableError(f"{self.name}: no API key ({self.key_env} is not set)")
        return super().search(query)

    def http_error(self, code: int, detail: str) -> ScraperError:
        if code in (401, 403):
            return SourceUnavailableError(f"{self.name}: key rejected (HTTP {code}){detail}")
        if code in (402, 429):
            return SourceUnavailableError(f"{self.name}: quota or rate limit reached (HTTP {code}){detail}")
        return ScraperError(f"{self.name}: HTTP {code}{detail}")
