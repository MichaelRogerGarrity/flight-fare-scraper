"""Opaque route labels for logs that may end up somewhere public.

The natural log line names the route -- "MIA-MCO 2026-10-30/2026-11-02". On a
public GitHub Actions run that log is world-readable, so when FFS_REDACT_LOGS is
set we log a stable digest instead.

Set FFS_REDACT_SALT too. Without a salt the input space (a few thousand airport
pairs times a year of date pairs) is small enough to brute-force from the digest,
so an unsalted id hides the route from a reader, not from an attacker.
"""

import hashlib
import os
import re

REDACT_ENV = "FFS_REDACT_LOGS"
SALT_ENV = "FFS_REDACT_SALT"
TRUTHY = ("1", "true", "yes", "on")


def enabled() -> bool:
    return os.environ.get(REDACT_ENV, "").strip().lower() in TRUTHY


def route_id(query) -> str:
    """A stable, salted id for one search. Same query -> same id across runs."""
    raw = "|".join(str(part) for part in (
        os.environ.get(SALT_ENV, ""),
        query.site, query.origin, query.destination,
        query.depart_date, query.return_date, query.nonstop_only,
    ))
    return "route-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def label(query) -> str:
    """The route label to log: opaque when redaction is on, readable otherwise."""
    return route_id(query) if enabled() else query.label


# Errors whose messages this project builds itself, from an already-redacted route
# label plus a page number and a poll status. Their detail is what tells a slow
# results page apart from a silent block -- "page 3 still 'first-phase'" versus
# "no results response for page 1" -- so it is worth keeping.
SAFE_DETAIL_TYPES = ("SearchTimeoutError", "PaginationError")
_URL = re.compile(r"https?://\S+")
_ROUTE = re.compile(r"\b[A-Z]{3}-[A-Z]{3}\b")


def error(text: str) -> str:
    """The exception type when redacting, plus the detail of errors known to be safe.

    Other messages carry whatever the site handed back -- a bot-block reports the URL
    it was redirected to, and a Playwright navigation error names the page it was
    loading -- and they end up on the console, which on a public CI run is
    world-readable. Even the safe types are scrubbed of URLs and route-shaped codes,
    so a later change to one of those messages can't leak through here.
    """
    if not enabled():
        return text
    kind = text.split(":", 1)[0].strip() or "error"
    if kind not in SAFE_DETAIL_TYPES:
        return kind
    return _ROUTE.sub("<route>", _URL.sub("<url>", text))
