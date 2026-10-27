"""Google Flights data, whichever way it arrives.

SerpApi, SearchApi and FlightPowers all resell Google Flights results, and `fli` reads
them directly; their responses differ only in shape. Each source parses its own into
the Itinerary below, and `to_result` turns that into the FlightResult every other part
of the project already stores -- so layover, stop and duration rules live in one place.

What Google gives differs from Kayak: ~30 curated itineraries per search rather than a
thousand seller listings, no per-seller prices, and no bag fees. Fields it doesn't have
are left empty; `site` tells the two apart when reading the history.
"""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from .. import redact
from ..models import FlightResult, SearchQuery
from .base import BaseScraper
from .filters import within_constraints

logger = logging.getLogger(__name__)

# Metropolitan codes Kayak accepts as one search, as the airports Google needs listed.
# Only codes that aren't also an airport's own code: BKK or SHA mean the airport, as on Kayak.
METRO_AIRPORTS: Dict[str, Sequence[str]] = {
    "BJS": ("PEK", "PKX"), "BUE": ("EZE", "AEP"), "CHI": ("ORD", "MDW"), "DTT": ("DTW",),
    "LON": ("LHR", "LGW", "STN", "LTN", "LCY", "SEN"), "MIL": ("MXP", "LIN", "BGY"),
    "MOW": ("SVO", "DME", "VKO"), "NYC": ("JFK", "LGA", "EWR"), "OSA": ("KIX", "ITM"),
    "PAR": ("CDG", "ORY"), "RIO": ("GIG", "SDU"), "ROM": ("FCO", "CIA"), "SAO": ("GRU", "CGH", "VCP"),
    "SEL": ("ICN", "GMP"), "STO": ("ARN", "BMA"), "TYO": ("HND", "NRT"), "WAS": ("IAD", "DCA", "BWI"),
    "YTO": ("YYZ", "YTZ"),
}


def airports_for(code: str) -> List[str]:
    """The airports to ask Google for: a metro code's airports, or the code itself."""
    return list(METRO_AIRPORTS.get(code.upper(), (code.upper(),)))


@dataclass
class Segment:
    """One flight, take-off to landing. Times are local to each airport."""
    airline: Optional[str] = None
    flight_number: Optional[str] = None
    from_airport: Optional[str] = None
    to_airport: Optional[str] = None
    depart: Optional[datetime] = None
    arrive: Optional[datetime] = None
    duration_min: Optional[int] = None
    aircraft: Optional[str] = None
    cabin: Optional[str] = None
    operated_by: Optional[str] = None


@dataclass
class Leg:
    """One direction of the trip. Sources that give only a summary leave `segments`
    empty and fill the summary fields; the summary wins when both are there."""
    segments: List[Segment] = field(default_factory=list)
    airline: Optional[str] = None
    from_airport: Optional[str] = None
    to_airport: Optional[str] = None
    depart: Optional[datetime] = None
    arrive: Optional[datetime] = None
    duration_min: Optional[int] = None
    stops: Optional[int] = None
    layover_airports: Optional[List[str]] = None
    layover_min: Optional[int] = None


@dataclass
class Itinerary:
    outbound: Leg
    inbound: Leg
    price: float  # the whole round trip, one adult
    currency: str = "USD"
    price_level: Optional[str] = None  # Google's own call: "low", "typical" or "high"
    booking_url: Optional[str] = None


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat(timespec="seconds") if moment else None


def _minutes(earlier: Optional[datetime], later: Optional[datetime]) -> Optional[int]:
    # Both ends of a connection are at the same airport (or two in one city), so local
    # times share a time zone and can be subtracted.
    if earlier is None or later is None:
        return None
    return int((later - earlier).total_seconds() // 60)


def _leg_fields(leg: Leg) -> dict:
    segments = leg.segments
    first = segments[0] if segments else Segment()
    last = segments[-1] if segments else Segment()
    connections = list(zip(segments, segments[1:]))
    stops = leg.stops if leg.stops is not None else (len(segments) - 1 if segments else None)
    if leg.layover_airports is not None:
        layover_airports = leg.layover_airports
    else:
        layover_airports = [a.to_airport for a, _ in connections if a.to_airport]
    layover_min = leg.layover_min
    if layover_min is None and connections:
        gaps = [_minutes(a.arrive, b.depart) for a, b in connections]
        layover_min = sum(gaps) if all(gap is not None for gap in gaps) else None
    duration = leg.duration_min
    if duration is None and segments and all(s.duration_min for s in segments):
        duration = sum(s.duration_min for s in segments) + (layover_min or 0)
    cabins = [s.cabin for s in segments if s.cabin]
    connecting = bool(stops)  # these fields are only set when the leg has a connection
    return {
        "airline": leg.airline or first.airline,
        "operated_by": first.operated_by,
        "depart": _iso(leg.depart or first.depart),
        "arrive": _iso(leg.arrive or last.arrive),
        "from": leg.from_airport or first.from_airport,
        "to": leg.to_airport or last.to_airport,
        "stops": stops,
        "duration_min": duration,
        "layover_airports": ",".join(layover_airports) if connecting and layover_airports else None,
        "layover_min": layover_min if connecting else None,
        "airport_change": (any(a.to_airport != b.from_airport for a, b in connections)
                           if connecting and connections else None),
        "equipment": first.aircraft,
        "cabins": cabins,
    }


def to_result(query: SearchQuery, itinerary: Itinerary, site: str) -> FlightResult:
    out, back = _leg_fields(itinerary.outbound), _leg_fields(itinerary.inbound)
    cabins = list(dict.fromkeys(out.pop("cabins") + back.pop("cabins")))
    fields = {"site": site, "origin": query.origin, "destination": query.destination,
              "depart_date": query.depart_date.isoformat(), "return_date": query.return_date.isoformat(),
              "price": float(itinerary.price), "currency": itinerary.currency,
              # Google lists itineraries, not sellers; who sells each fare takes another request.
              "booking_provider": "",
              "cabin_class": ",".join(cabins) or None,
              "price_prediction": itinerary.price_level, "booking_url": itinerary.booking_url}
    for prefix, values in (("outbound", out), ("return", back)):
        for name, value in values.items():
            fields[f"{prefix}_{name}"] = value
    return FlightResult(**fields)


def cheapest_first(items: Sequence, price) -> list:
    """Items sorted by price, unpriced ones last."""
    return sorted(items, key=lambda item: (price(item) is None, price(item) or 0))


def outbound_options() -> int:
    """How many outbound flights to pair with returns, for sources that fetch the
    returns separately. Google prices each outbound with its cheapest return, so 1 finds
    the cheapest round trip; each extra one costs a request but adds pairings."""
    try:
        return max(1, int(os.environ.get(OUTBOUND_OPTIONS_ENV, "1")))
    except ValueError:
        return 1


OUTBOUND_OPTIONS_ENV = "FFS_GOOGLE_OUTBOUND_OPTIONS"


class ItinerarySource(BaseScraper):
    """A source that yields Google Flights itineraries. Implement `itineraries`; this
    turns them into rows, filters them as the Kayak scraper does, and fills the stats
    each search record keeps -- the same way for every source."""

    def __init__(self, headless: bool = True, max_pages: Optional[int] = None, **_options):
        # The runner builds every source alike; a browser's options mean nothing here.
        self.requests_made = 0
        self.last_stats = None

    def reset(self) -> None:
        pass  # nothing to relaunch between attempts

    def itineraries(self, query: SearchQuery) -> List[Itinerary]:
        raise NotImplementedError

    def search(self, query: SearchQuery) -> List[FlightResult]:
        started, before = time.monotonic(), self.requests_made
        stats = {"ignored_polls": 0, "unreadable_polls": 0, "truncated": False}
        self.last_stats = {"main": stats}
        try:
            results = [to_result(query, itinerary, self.name) for itinerary in self.itineraries(query)]
        finally:
            stats["pages_fetched"] = self.requests_made - before
            stats["first_page_seconds"] = round(time.monotonic() - started, 1)
        kept = [result for result in results if within_constraints(query, result)]
        stats["results_reported"] = len(results)
        self.last_stats.update(rows_kept=len(kept), rows_dropped_by_filters=len(results) - len(kept))
        logger.info("%s: %d itineraries from %s in %d request(s)", redact.label(query), len(results), self.name,
                    stats["pages_fetched"])
        return kept
