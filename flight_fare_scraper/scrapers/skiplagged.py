"""Skiplagged: each direction's fares from the endpoint its own site searches, no key.

Free, and robots.txt leaves its search open (only redirect and booking paths are
disallowed). It answered GitHub's runners 40 times in a row where Kayak and Google
refuse them (docs/backup-fare-sources.md). Its terms forbid automated access without
permission, as Kayak's do, so the run's load limits apply to it (`polite`).

Two things differ from the other sources:
- It prices each direction as a one-way ticket, so a round trip here is two tickets and
  costs their sum. Kayak and Google price a single round-trip ticket, which can differ.
- Hidden-city fares are mixed in: tickets that fly on past the destination, cheap only if
  you get off at the connection, and unusable with a checked bag. They're dropped: a leg
  must leave from one of its start's airports and land at one of its end's.

Each kept outbound is paired with the cheapest kept return, and each kept return with
the cheapest outbound, so every flight it lists is stored with its cheapest complement.
One request per search, metro codes included. The answer is marked incomplete (the
site's page keeps polling), but its cheapest fares matched Google's within a few dollars.
"""

from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from ..models import SearchQuery
from .base import ScraperError
from .filters import leg_within_constraints
from .google_flights import Itinerary, Leg, Segment, _iso, airports_for
from .http_source import HttpSource

ENDPOINT = "https://skiplagged.com/api/search.php"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")

# (price in dollars, flight key, leg), cheapest first
Option = Tuple[float, str, Leg]


def _moment(end: Optional[dict]) -> Optional[datetime]:
    """A segment end's local time. Skiplagged gives an offset; the other sources' rows don't."""
    try:
        return datetime.fromisoformat(end["time"]).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError):
        return None


def parse_leg(flight: dict, airlines: Dict[str, dict]) -> Leg:
    segments = []
    for part in flight.get("segments") or []:
        code = part.get("airline")
        seconds = part.get("duration")
        segments.append(Segment(
            airline=(airlines.get(code) or {}).get("name", code),
            flight_number=f"{code} {part['flight_number']}" if part.get("flight_number") else None,
            from_airport=(part.get("departure") or {}).get("airport"),
            to_airport=(part.get("arrival") or {}).get("airport"),
            depart=_moment(part.get("departure")), arrive=_moment(part.get("arrival")),
            duration_min=seconds // 60 if isinstance(seconds, int) else None,
        ))
    return Leg(segments=segments)


def _ends(data: dict, side: str, fallback: str) -> Set[str]:
    """The airports Skiplagged searched for one end of the trip, as it reports them."""
    listed = ((data.get("info") or {}).get(side) or {}).get("airports")
    return set(listed or airports_for(fallback))


def options(query: SearchQuery, data: dict, direction: str, starts: Set[str], ends: Set[str]) -> List[Option]:
    """One direction's usable flights: not hidden-city, within the query's filters, the
    cheapest price per flight."""
    flights, airlines = data.get("flights") or {}, data.get("airlines") or {}
    kept: Dict[str, Option] = {}
    for item in (data.get("itineraries") or {}).get(direction) or []:
        key, cents = item.get("flight"), item.get("one_way_price")
        if key not in flights or not isinstance(cents, (int, float)):
            continue
        leg = parse_leg(flights[key], airlines)
        if not leg.segments or leg.segments[0].from_airport not in starts or leg.segments[-1].to_airport not in ends:
            continue  # hidden-city: it flies on past where this trip ends
        if not leg_within_constraints(query, direction == "outbound", len(leg.segments) - 1,
                                      _iso(leg.segments[0].depart), _iso(leg.segments[-1].arrive)):
            continue
        if key not in kept or cents / 100 < kept[key][0]:
            kept[key] = (cents / 100, key, leg)
    return sorted(kept.values(), key=lambda option: option[0])


def pair(outbound: List[Option], inbound: List[Option]) -> List[Itinerary]:
    """Every flight with the other direction's cheapest, priced as two one-way tickets."""
    if not outbound or not inbound:
        return []
    # Flights are unique per direction, so only the cheapest-with-cheapest pair repeats.
    pairs = [(o, inbound[0]) for o in outbound] + [(outbound[0], i) for i in inbound[1:]]
    return [Itinerary(o[2], i[2], round(o[0] + i[0], 2)) for o, i in pairs]


class SkiplaggedScraper(HttpSource):
    name = "skiplagged"
    polite = True  # read directly, like Kayak
    default_headers = {"User-Agent": USER_AGENT}

    def itineraries(self, query: SearchQuery) -> List[Itinerary]:
        data = self.request("GET", ENDPOINT, params={
            "from": query.origin, "to": query.destination,
            "depart": query.depart_date.isoformat(), "return": query.return_date.isoformat(),
            "format": "v3", "counts[adults]": 1, "counts[children]": 0,
        })
        if not isinstance(data, dict) or "itineraries" not in data:
            raise ScraperError("skiplagged: answer had no itineraries")
        origins, destinations = _ends(data, "from", query.origin), _ends(data, "to", query.destination)
        return pair(options(query, data, "outbound", origins, destinations),
                    options(query, data, "inbound", destinations, origins))
