"""Google Flights through Apify's "Google Flights API" actor (lergassy/google-flights-api).

Google Flights lists every US airline -- Delta, United, American, Southwest, JetBlue,
Alaska and the low-cost carriers -- with Basic Economy fares, which no site we can read
from GitHub's runners does (docs/backup-fare-sources.md). Google refuses those runners
directly; this actor fetches through its own proxies, so being refused is its problem,
and it bills per itinerary row: $0.20 per 1,000, with Apify's free plan including $5 of
usage a month.

One search is one synchronous actor run. It asks for the FFS_APIFY_OUTBOUNDS cheapest
outbound flights (default 5), each with up to 20 of its returns priced as the round
trip's total; every such pairing is stored. A route that allows stops but whose cheapest
outbounds are all connections gets a second, nonstop-only run, as Kayak gets a nonstop
pass. Its fallback to Kayak is turned off, so every row is Google's.

The actor documents its row fields but not the keys inside `legs`, `layovers` and
`returnOptions`, so those are read from several likely spellings and the summary fields
fill any gap. Check the first live run's rows before relying on the details.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from .. import redact
from ..models import SearchQuery
from .api import ApiScraper
from .base import ScraperError, SourceUnavailableError
from .google_flights import Itinerary, Leg, Segment, airports_for, env_count


def env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
        return value if value > 0 else default
    except ValueError:
        return default

logger = logging.getLogger(__name__)

ACTOR = "lergassy~google-flights-api"
ENDPOINT = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
LIMITS_URL = "https://api.apify.com/v2/users/me/limits"  # this cycle's usage and cap, and when it resets
OUTBOUNDS_ENV = "FFS_APIFY_OUTBOUNDS"
# The free plan stops at its monthly credit rather than charging, so the budget only
# spreads the credit over the month. Cost per search is a guess until the first runs
# measure it: 5 rows at $0.0002, with room for a nonstop rerun and billed returns.
COST_ENV, DEFAULT_COST_PER_SEARCH = "FFS_APIFY_COST_PER_SEARCH", 0.002
RESERVE_ENV, DEFAULT_RESERVE_USD = "FFS_APIFY_RESERVE_USD", 0.25
DAILY_SEARCHES_ENV, DEFAULT_DAILY_SEARCHES = "FFS_APIFY_DAILY_SEARCHES", 150  # also the cap if usage can't be read
RETURNS_PER_OUTBOUND = 20
NOT_FLIGHTS = {"search", "notice", "calendar_day", "error"}  # other kinds of row the actor can add


def _first(item: dict, *keys: str) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return None


def _moment(text: Any) -> Optional[datetime]:
    """Google's local time, "2027-03-15T15:05"; any offset is dropped, as for the other sources."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _airport(value: Any) -> Optional[str]:
    """An airport code, whether given as "DEN" or as {"code": "DEN", ...}."""
    if isinstance(value, dict):
        value = _first(value, "code", "iata", "id", "airport")
    return value.upper() if isinstance(value, str) and len(value) == 3 else None


def _segment(part: dict) -> Segment:
    return Segment(
        airline=_first(part, "airline", "airlineName", "carrier"),
        flight_number=_first(part, "flightNumber", "flight_number", "number"),
        from_airport=_airport(_first(part, "from", "departureAirport", "origin", "departure_airport")),
        to_airport=_airport(_first(part, "to", "arrivalAirport", "destination", "arrival_airport")),
        depart=_moment(_first(part, "departAt", "departureTime", "departure_time", "departure")),
        arrive=_moment(_first(part, "arriveAt", "arrivalTime", "arrival_time", "arrival")),
        duration_min=_int(_first(part, "durationMinutes", "duration")),
        aircraft=_first(part, "aircraft", "airplane"),
        operated_by=_first(part, "operatedBy", "operatingCarrier", "operated_by"),
    )


def parse_leg(item: dict) -> Leg:
    """One direction: its flights if the row lists them, with the row's summary fields."""
    segments = [_segment(part) for part in item.get("legs") or [] if isinstance(part, dict)]
    layovers = [layover for layover in item.get("layovers") or [] if isinstance(layover, dict)]
    waits = [_int(_first(layover, "durationMinutes", "duration")) for layover in layovers]
    airlines = item.get("airline")
    return Leg(
        segments=segments,
        airline=airlines if isinstance(airlines, str) else None,
        depart=_moment(item.get("departAt")), arrive=_moment(item.get("arriveAt")),
        duration_min=_int(item.get("totalDurationMinutes")),
        stops=_int(item.get("stops")),
        layover_airports=[code for code in (_airport(_first(layover, "airport", "code")) for layover in layovers)
                          if code] or None,
        layover_min=sum(waits) if layovers and None not in waits else None,
    )


def _ends(leg: Leg) -> tuple:
    if leg.segments:
        return leg.segments[0].from_airport, leg.segments[-1].to_airport
    return None, None


def _flight_rows(rows: Any) -> List[dict]:
    if not isinstance(rows, list):
        raise ScraperError("apify: answer was not a list of rows")
    flights = [row for row in rows if isinstance(row, dict) and row.get("type") not in NOT_FLIGHTS]
    if not flights:
        errors = [row for row in rows if isinstance(row, dict) and row.get("type") == "error"]
        if errors:
            raise ScraperError("apify: the actor reported an error for this search")
    return flights


def parse_rows(query: SearchQuery, rows: Iterable[dict]) -> List[Itinerary]:
    """Every outbound paired with each of its returns, between the query's own airports."""
    origins, destinations = set(airports_for(query.origin)), set(airports_for(query.destination))
    found = []
    for row in rows:
        outbound = parse_leg(row)
        out_from, out_to = _ends(outbound)
        if (out_from and out_from not in origins) or (out_to and out_to not in destinations):
            continue
        for option in row.get("returnOptions") or []:
            if not isinstance(option, dict):
                continue
            total = _first(option, "totalPrice", "price", "roundTripPrice")
            if not isinstance(total, (int, float)):
                continue
            inbound = parse_leg(option)
            back_from, back_to = _ends(inbound)
            if (back_from and back_from not in destinations) or (back_to and back_to not in origins):
                continue
            found.append(Itinerary(outbound, inbound, float(total), currency=row.get("currency") or "USD",
                                   price_level=row.get("priceLevel")))
    return found


def daily_allowance(limits: dict, now: datetime, cost_per_search: float, reserve_usd: float) -> Optional[int]:
    """Searches today can afford: the month's credit left, less a reserve, spread evenly
    over the days left in the usage cycle. None if Apify's answer can't be read."""
    data = limits.get("data") if isinstance(limits, dict) else None
    try:
        cap = float(data["limits"]["maxMonthlyUsageUsd"])
        used = float(data["current"]["monthlyUsageUsd"])
        ends = datetime.fromisoformat(str(data["monthlyUsageCycle"]["endAt"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    days_left = max(1, -(-(ends.replace(tzinfo=None) - now).total_seconds() // 86400))
    return max(0, int((cap - used - reserve_usd) / days_left / cost_per_search))


class ApifyGoogleScraper(ApiScraper):
    name = "apify_google"
    key_env = "FFS_APIFY_TOKEN"
    timeout_s = 300.0  # a synchronous run answers within Apify's 5-minute limit or not at all

    def __enter__(self) -> "ApifyGoogleScraper":
        super().__enter__()
        self.allowance: Optional[int] = None  # searches left today; worked out at the first search
        self.used_at_start: Optional[float] = None
        self.searched = 0
        return self

    def _auth(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}  # keeps the token out of the URL

    def _budget(self) -> int:
        """Today's share of the month's credit, in searches, read from Apify once a run."""
        fallback = env_count(DAILY_SEARCHES_ENV, DEFAULT_DAILY_SEARCHES)
        try:
            limits = self.request("GET", LIMITS_URL, headers=self._auth())
        except SourceUnavailableError:
            raise
        except ScraperError as error:
            logger.warning("apify: couldn't read the account's usage (%s); allowing %d searches", error, fallback)
            return fallback
        cost = env_float(COST_ENV, DEFAULT_COST_PER_SEARCH)
        allowance = daily_allowance(limits, datetime.now(timezone.utc).replace(tzinfo=None), cost, env_float(RESERVE_ENV, DEFAULT_RESERVE_USD))
        if allowance is None:
            logger.warning("apify: the account's usage was unreadable; allowing %d searches", fallback)
            return fallback
        data = limits["data"]
        self.used_at_start = float(data["current"]["monthlyUsageUsd"])
        logger.info("apify: $%.2f of $%.2f used this cycle; today's share is %d searches at ~$%.4f each",
                    self.used_at_start, float(data["limits"]["maxMonthlyUsageUsd"]), allowance, cost)
        return min(allowance, fallback) if os.environ.get(DAILY_SEARCHES_ENV) else allowance

    def search(self, query: SearchQuery):
        if not getattr(self, "api_key", ""):
            return super().search(query)  # raises: no key
        if self.allowance is None:
            self.allowance = self._budget()
        if self.searched >= self.allowance:
            raise SourceUnavailableError(f"apify: today's share of the free credit is used ({self.searched} searches)")
        self.searched += 1
        return super().search(query)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.searched and self.used_at_start is not None:
            try:
                used = float(self.request("GET", LIMITS_URL, headers=self._auth())["data"]["current"]["monthlyUsageUsd"])
                logger.info("apify: this run used ~$%.4f for %d searches (~$%.5f each; Apify's figure can lag)",
                            used - self.used_at_start, self.searched, (used - self.used_at_start) / self.searched)
            except (ScraperError, KeyError, TypeError, ValueError):
                pass
        super().__exit__(exc_type, exc, tb)

    def run_input(self, query: SearchQuery, nonstop: bool) -> Dict[str, Any]:
        outbounds = env_count(OUTBOUNDS_ENV, 5, minimum=1)
        return {
            "origin": query.origin, "destination": query.destination,
            "departureDate": query.depart_date.isoformat(), "returnDate": query.return_date.isoformat(),
            "adults": 1, "cabinClass": "economy", "currency": "USD", "market": "US", "language": "en",
            "maxStops": "0" if nonstop else "any", "sortBy": "price", "maxResults": outbounds,
            "resolveReturnFlights": True, "maxReturnResolutions": outbounds,
            "maxReturnOptions": RETURNS_PER_OUTBOUND,
            "kayakWhenGoogleEmpty": False, "fillMissingPricesFromKayak": False, "resolveBookingOptions": False,
            "includeSearchSummary": False, "includeRunSummary": False,
        }

    def _run(self, query: SearchQuery, nonstop: bool) -> List[dict]:
        return _flight_rows(self.request("POST", ENDPOINT, body=self.run_input(query, nonstop), headers=self._auth()))

    def itineraries(self, query: SearchQuery) -> List[Itinerary]:
        rows = self._run(query, nonstop=query.nonstop_only)
        if not query.nonstop_only and rows and all(_int(row.get("stops")) for row in rows):
            logger.info("%s: cheapest outbounds all connect, so a nonstop-only run too", redact.label(query))
            rows += self._run(query, nonstop=True)
        return parse_rows(query, rows)
