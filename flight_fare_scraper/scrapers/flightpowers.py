"""FlightPowers: Google Flights round trips in one request, already paired.

https://flightpowers.com/flights-api -- free on RapidAPI: 10 requests a month. Its
round-trip answer is a summary per itinerary: each leg's airline, stops, duration and
times ("8:35 PM on Thu, Oct 15"), with no flight numbers, connections or aircraft, so
those columns stay empty. It does carry Google's verdict on the price.

It takes single airports, so a metro code is searched airport by airport -- NYC to PAR
is six requests. Schema: https://flightpowers.com/openapi.json
"""

import re
from datetime import date, datetime, timedelta
from typing import List, Optional

from ..models import SearchQuery
from .api import ApiScraper
from .base import ScraperError
from .google_flights import Itinerary, Leg, airports_for

_DESCRIPTION = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)\s+on\s+\w{3},\s+(\w{3})\s+(\d{1,2})", re.IGNORECASE)
_DURATION = re.compile(r"(?:(\d+)\s*hr?s?)?\s*(?:(\d+)\s*min)?", re.IGNORECASE)


def parse_description(text: Optional[str], near: date) -> Optional[datetime]:
    """"8:35 PM on Thu, Oct 15" -> a datetime, taking the year that puts it nearest `near`."""
    match = _DESCRIPTION.search(text or "")
    if not match:
        return None
    clock, month, day = match.groups()
    candidates = []
    for year in (near.year - 1, near.year, near.year + 1):
        try:
            candidates.append(datetime.strptime(f"{clock.upper().replace(' ', '')} {month} {day} {year}",
                                                "%I:%M%p %b %d %Y"))
        except ValueError:
            continue
    return min(candidates, key=lambda moment: abs(moment.date() - near), default=None)


def parse_duration(text: Optional[str]) -> Optional[int]:
    """"13 hr 5 min" -> 785."""
    match = _DURATION.fullmatch((text or "").strip())
    if not match or not any(match.groups()):
        return None
    hours, minutes = (int(part) if part else 0 for part in match.groups())
    return hours * 60 + minutes


def _leg(item: dict, prefix: str, leg_date: date, start: Optional[str], end: Optional[str]) -> Leg:
    return Leg(airline=item.get(f"{prefix}_airline"), from_airport=start, to_airport=end,
               depart=parse_description(item.get(f"{prefix}_departure_description"), leg_date),
               arrive=parse_description(item.get(f"{prefix}_arrival_description"), leg_date + timedelta(days=1)),
               duration_min=parse_duration(item.get(f"{prefix}_duration")),
               stops=item.get(f"{prefix}_stops"))


class FlightPowersScraper(ApiScraper):
    name = "flightpowers"
    key_env = "FFS_FLIGHTPOWERS_KEY"  # the RapidAPI key
    endpoint = "https://api.flightpowers.com/v1/flights/roundtrip"

    def itineraries(self, query: SearchQuery) -> List[Itinerary]:
        found: List[Itinerary] = []
        for origin in airports_for(query.origin):
            for destination in airports_for(query.destination):
                body = {"from_airport": origin, "to_airport": destination,
                        "departure_date": query.depart_date.isoformat(), "return_date": query.return_date.isoformat(),
                        "currency": "USD", "seat_type": 1}
                if query.nonstop_only:
                    body.update(max_departure_stops=0, max_return_stops=0)
                answer = self.request("POST", self.endpoint, body=body, headers={"x-api-key": self.api_key})
                if isinstance(answer, dict) and answer.get("error"):
                    raise ScraperError(f"API error: {str(answer['error'].get('type', 'unknown'))[:80]}")
                for item in answer if isinstance(answer, list) else []:
                    price = item.get("total_price_as_number")
                    if price is None:
                        continue
                    start, end = item.get("from_airport", origin), item.get("to_airport", destination)
                    found.append(Itinerary(
                        _leg(item, "departure_flight", query.depart_date, start, end),
                        _leg(item, "return_flight", query.return_date, end, start),
                        float(price), price_level=item.get("price_range_in_relation_to_other_periods"),
                        booking_url=item.get("buy_link")))
        return found
