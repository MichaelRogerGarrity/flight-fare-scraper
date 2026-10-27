"""SerpApi and SearchApi: Google Flights through a search-results API.

Both relay Google's own response in the same shape -- `best_flights` and
`other_flights`, each item a list of flight segments with its price -- and both answer
a round trip in two steps: the outbound flights, each priced with its cheapest return
and carrying a `departure_token`; then that token, for the returns paired with that
outbound. So one class does the work, and each provider only says how to ask.

A round trip costs 1 + FFS_GOOGLE_OUTBOUND_OPTIONS requests (default 2).
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..models import SearchQuery
from .api import ApiScraper
from .base import ScraperError
from .google_flights import Itinerary, Leg, Segment, airports_for, cheapest_first, outbound_options

_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p")


def _moment(airport: Optional[dict]) -> Optional[datetime]:
    """A segment end's local time: SerpApi gives "2026-03-03 10:10", SearchApi a date and a time."""
    if not airport:
        return None
    text = f"{airport['date']} {airport['time']}" if airport.get("date") else airport.get("time")
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(str(text).strip(), fmt)
        except (TypeError, ValueError):
            continue
    return None


def _int(value) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_item(item: dict) -> Tuple[Leg, Optional[float], Optional[str]]:
    """One `best_flights`/`other_flights` entry: the leg, its price, and its departure token."""
    segments = [
        Segment(
            airline=flight.get("airline"), flight_number=flight.get("flight_number"),
            from_airport=(flight.get("departure_airport") or {}).get("id"),
            to_airport=(flight.get("arrival_airport") or {}).get("id"),
            depart=_moment(flight.get("departure_airport")), arrive=_moment(flight.get("arrival_airport")),
            duration_min=_int(flight.get("duration")), aircraft=flight.get("airplane"),
            cabin=flight.get("travel_class"),
        )
        for flight in item.get("flights") or []
    ]
    layovers = item.get("layovers") or []
    durations = [_int(layover.get("duration")) for layover in layovers]
    leg = Leg(
        segments=segments,
        duration_min=_int(item.get("total_duration")),
        layover_airports=[layover["id"] for layover in layovers if layover.get("id")] or None,
        layover_min=sum(durations) if layovers and None not in durations else None,
    )
    price = item.get("price")
    return leg, float(price) if isinstance(price, (int, float)) else None, item.get("departure_token")


def _items(response: Any) -> List[dict]:
    if not isinstance(response, dict):
        raise ScraperError("response was not a JSON object")
    if response.get("error"):
        # SerpApi answers "no results" this way too; the message names no route.
        if "hasn't returned any results" in str(response["error"]):
            return []
        raise ScraperError(f"API error: {str(response['error'])[:160]}")
    return list(response.get("best_flights") or []) + list(response.get("other_flights") or [])


class GoogleSerpScraper(ApiScraper):
    endpoint: str = ""

    def params(self, query: SearchQuery, departure_token: Optional[str] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def headers(self) -> Dict[str, str]:
        return {}

    def _call(self, query: SearchQuery, departure_token: Optional[str] = None) -> dict:
        return self.request("GET", self.endpoint, params=self.params(query, departure_token), headers=self.headers())

    def itineraries(self, query: SearchQuery) -> List[Itinerary]:
        first = self._call(query)
        level = ((first.get("price_insights") or {}).get("price_level") if isinstance(first, dict) else None)
        outbounds = cheapest_first([parse_item(item) for item in _items(first)], lambda option: option[1])
        found: List[Itinerary] = []
        for leg, price, token in outbounds[:outbound_options()]:
            if not token:
                continue
            for back, total, _ in (parse_item(item) for item in _items(self._call(query, token))):
                total = total if total is not None else price
                if total is not None:
                    found.append(Itinerary(leg, back, total, price_level=level))
        return found

    @staticmethod
    def _route(query: SearchQuery) -> Dict[str, str]:
        return {"departure_id": ",".join(airports_for(query.origin)),
                "arrival_id": ",".join(airports_for(query.destination)),
                "outbound_date": query.depart_date.isoformat(), "return_date": query.return_date.isoformat(),
                "currency": "USD", "hl": "en", "gl": "us"}


class SerpApiScraper(GoogleSerpScraper):
    """https://serpapi.com/google-flights-api -- free plan: 250 searches a month."""

    name = "serpapi"
    key_env = "FFS_SERPAPI_KEY"
    endpoint = "https://serpapi.com/search"

    def params(self, query, departure_token=None):
        params = {"engine": "google_flights", **self._route(query), "type": 1,
                  "stops": 1 if query.nonstop_only else 0, "api_key": self.api_key}
        if departure_token:
            params["departure_token"] = departure_token
        return params


class SearchApiScraper(GoogleSerpScraper):
    """https://www.searchapi.io/docs/google-flights-api -- 100 free requests to try it."""

    name = "searchapi"
    key_env = "FFS_SEARCHAPI_KEY"
    endpoint = "https://www.searchapi.io/api/v1/search"

    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}  # keeps the key out of the URL

    def params(self, query, departure_token=None):
        params = {"engine": "google_flights", **self._route(query), "flight_type": "round_trip",
                  "stops": "nonstop" if query.nonstop_only else "any"}
        if departure_token:
            params["departure_token"] = departure_token
        return params
