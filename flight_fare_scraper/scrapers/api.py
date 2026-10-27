"""The shared part of every source reached over an HTTP API rather than a browser.

A subclass names its key's environment variable and implements `itineraries` using
`request`; the search itself -- rows, filtering, stats -- is ItinerarySource's. This
class adds what every API needs alike: reading the key, making the request, and
turning HTTP failures into the errors the runner understands. An API whose key is
missing, rejected or out of quota raises SourceUnavailableError, and the runner skips
the rest of that source's searches for the run.

Keys come only from the environment and never appear in a log or an error message.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ..models import FlightResult, SearchQuery
from .base import ScraperError, SearchTimeoutError, SourceUnavailableError
from .google_flights import ItinerarySource

TIMEOUT_S = 90.0


class ApiScraper(ItinerarySource):
    # Paid APIs meter themselves; the run's hourly cap is for sites we read directly.
    polite = False
    key_env: str = ""  # environment variable holding the API key
    timeout_s: float = TIMEOUT_S

    def __enter__(self) -> "ApiScraper":
        self.api_key = os.environ.get(self.key_env, "").strip()
        return self

    def search(self, query: SearchQuery) -> List[FlightResult]:
        if not getattr(self, "api_key", ""):
            raise SourceUnavailableError(f"{self.name}: no API key ({self.key_env} is not set)")
        return super().search(query)

    def request(self, method: str, url: str, params: Optional[Dict[str, Any]] = None,
                body: Optional[dict] = None, headers: Optional[Dict[str, str]] = None) -> Any:
        """One API call, returning the decoded JSON. Counts toward this search's requests."""
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        sent = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            sent["Content-Type"] = "application/json"
        self.requests_made += 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=sent),
                                        timeout=self.timeout_s) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            detail = _error_detail(error)
            if error.code in (401, 403):
                raise SourceUnavailableError(f"{self.name}: key rejected (HTTP {error.code}){detail}") from None
            if error.code in (402, 429):
                raise SourceUnavailableError(f"{self.name}: quota or rate limit reached (HTTP {error.code}){detail}") from None
            raise ScraperError(f"{self.name}: HTTP {error.code}{detail}") from None
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            # Only the type: a URLError's text can include the address, and with it a key.
            raise SearchTimeoutError(f"{self.name}: request failed ({type(error).__name__})") from None
        try:
            return json.loads(payload)
        except ValueError:
            raise ScraperError(f"{self.name}: response was not JSON") from None


def _error_detail(error: urllib.error.HTTPError) -> str:
    """The API's own error message, briefly. APIs answer errors in JSON; nothing else is kept."""
    try:
        body = json.loads(error.read() or b"{}")
    except (ValueError, OSError):
        return ""
    message = body.get("error") if isinstance(body, dict) else None
    if isinstance(message, dict):
        message = message.get("message") or message.get("type")
    return f": {str(message)[:160]}" if message else ""
