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
