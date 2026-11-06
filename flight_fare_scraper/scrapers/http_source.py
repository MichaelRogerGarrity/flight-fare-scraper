"""The shared part of every source read over plain HTTP rather than a browser.

A subclass implements `itineraries` using `request`; the search itself -- rows,
filtering, stats -- is ItinerarySource's. This adds the request and turns its failures
into the errors the runner understands. A site read directly treats a refusal (403/429)
as the site refusing us, counted toward the breaker; the paid APIs (api.py) read the
same codes as a key or quota problem instead.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from .base import BotBlockedError, CaptchaError, ScraperError, SearchTimeoutError
from .google_flights import ItinerarySource

TIMEOUT_S = 90.0


class HttpSource(ItinerarySource):
    timeout_s: float = TIMEOUT_S
    default_headers: Dict[str, str] = {}  # sent with every request

    def request(self, method: str, url: str, params: Optional[Dict[str, Any]] = None,
                body: Optional[dict] = None, headers: Optional[Dict[str, str]] = None) -> Any:
        """One HTTP call, returning the decoded JSON. Counts toward this search's requests."""
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        sent = {"Accept": "application/json", **self.default_headers, **(headers or {})}
        if data is not None:
            sent["Content-Type"] = "application/json"
        self.requests_made += 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=sent),
                                        timeout=self.timeout_s) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise self.http_error(error.code, _error_detail(error)) from None
        except (TimeoutError, urllib.error.URLError, OSError) as error:
            # Only the type: a URLError's text can include the address, and with it a key.
            raise SearchTimeoutError(f"{self.name}: request failed ({type(error).__name__})") from None
        try:
            return json.loads(payload)
        except ValueError:
            # A site that stops trusting us answers with a challenge page instead of data.
            if b"captcha" in payload[:50000].lower():
                raise CaptchaError(f"{self.name}: answered with a CAPTCHA page") from None
            raise ScraperError(f"{self.name}: response was not JSON") from None

    def http_error(self, code: int, detail: str) -> ScraperError:
        """The error to raise for an HTTP error status."""
        if code in (403, 429):
            return BotBlockedError(f"{self.name}: refused (HTTP {code}){detail}")
        return ScraperError(f"{self.name}: HTTP {code}{detail}")


def _error_detail(error: urllib.error.HTTPError) -> str:
    """The service's own error message, briefly. APIs answer errors in JSON; nothing else is kept."""
    try:
        body = json.loads(error.read() or b"{}")
    except (ValueError, OSError):
        return ""
    message = body.get("error") if isinstance(body, dict) else None
    if isinstance(message, dict):
        message = message.get("message") or message.get("type")
    return f": {str(message)[:160]}" if message else ""
