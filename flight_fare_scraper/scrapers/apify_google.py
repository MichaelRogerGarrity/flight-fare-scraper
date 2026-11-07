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
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from .. import redact
from ..models import SearchQuery
from .api import ApiScraper
from .base import ScraperError
from .google_flights import Itinerary, Leg, Segment, airports_for, env_count

logger = logging.getLogger(__name__)

ACTOR = "lergassy~google-flights-api"
ENDPOINT = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
OUTBOUNDS_ENV = "FFS_APIFY_OUTBOUNDS"
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


class ApifyGoogleScraper(ApiScraper):
    name = "apify_google"
    key_env = "FFS_APIFY_TOKEN"
    timeout_s = 300.0  # a synchronous run answers within Apify's 5-minute limit or not at all

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
        rows = self.request("POST", ENDPOINT, body=self.run_input(query, nonstop),
                            headers={"Authorization": f"Bearer {self.api_key}"})  # keeps the token out of the URL
        return _flight_rows(rows)

    def itineraries(self, query: SearchQuery) -> List[Itinerary]:
        rows = self._run(query, nonstop=query.nonstop_only)
        if not query.nonstop_only and rows and all(_int(row.get("stops")) for row in rows):
            logger.info("%s: cheapest outbounds all connect, so a nonstop-only run too", redact.label(query))
            rows += self._run(query, nonstop=True)
        return parse_rows(query, rows)
