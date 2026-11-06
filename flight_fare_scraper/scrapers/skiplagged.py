"""Skiplagged: fares from the endpoints its own site searches with, no key.

Free, and robots.txt leaves its search open (only redirect and booking paths are
disallowed). It answered GitHub's runners 40 times in a row where Kayak and Google
refuse them (docs/backup-fare-sources.md). Its terms forbid automated access without
permission, as Kayak's do, so the run's load limits apply to it (`polite`).

How it prices differs from the other sources:
- Mostly as one-way tickets, so most round trips here are two tickets costing their sum.
  Each kept outbound is paired with the cheapest kept return, and each kept return with
  the cheapest outbound, so every flight it lists is stored with its cheapest complement.
- Some outbounds also carry a round-trip ticket's price (`min_round_trip_price`), and some
  are sold only that way. Which returns go with it takes another request -- the one its
  site sends when you pick that outbound. That's made only where the ticket could beat the
  two one-ways, for at most FFS_SKIPLAGGED_ROUND_TRIPS outbounds per search (default 3),
  cheapest first, at ~2 requests each. Its returns are stored at the ticket's price.
- Hidden-city fares are mixed in: tickets that fly on past the destination, cheap only if
  you get off at the connection, and unusable with a checked bag. They're dropped, by the
  site's own test (you'd ride fewer flights than the ticket has) and by checking that a
  leg leaves from and lands at the trip's own airports.

The main search is one request, metro codes included. Its answer is marked incomplete
(the site's page keeps polling), but its cheapest fares matched Google's within a few dollars.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from .. import redact
from ..models import SearchQuery
from .base import BotBlockedError, ScraperError
from .filters import leg_within_constraints
from .google_flights import Itinerary, Leg, Segment, _iso, airports_for, env_count
from .http_source import HttpSource

logger = logging.getLogger(__name__)

ENDPOINT = "https://skiplagged.com/api/search.php"
RETURNS_ENDPOINT = "https://skiplagged.com/api/search_lazy.php"  # an outbound's round-trip returns
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36")
ROUND_TRIPS_ENV = "FFS_SKIPLAGGED_ROUND_TRIPS"
RETURN_POLLS = 3  # the site's own page had the full list on its second ask
POLL_PAUSE_S = 3.0
ROUNDING_MARGIN = 1.0  # dollars; see round_trip_candidates


@dataclass
class Option:
    """One usable flight in one direction, with what it costs."""
    key: str
    leg: Leg
    one_way: Optional[float] = None  # dollars, as its own ticket
    round_trip: Optional[float] = None  # dollars, as a round-trip ticket with its cheapest return
    meta: str = ""  # the fare's `data` string, which the returns request sends back
    flight_data: str = ""  # the flight's `data` string, likewise


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


def _dollars(cents) -> Optional[float]:
    return cents / 100 if isinstance(cents, (int, float)) and not isinstance(cents, bool) else None


def usable_leg(query: SearchQuery, data: dict, key: Optional[str], outbound: bool,
               starts: Set[str], ends: Set[str]) -> Optional[Leg]:
    """The flight as a leg, or None if it's hidden-city or outside the query's filters."""
    flight = (data.get("flights") or {}).get(key)
    if not flight:
        return None
    leg = parse_leg(flight, data.get("airlines") or {})
    segments = leg.segments
    if not segments:
        return None
    ridden = flight.get("count")
    if isinstance(ridden, int) and ridden < len(segments):
        return None  # hidden-city: you'd get off before the ticket's last flight
    if segments[0].from_airport not in starts or segments[-1].to_airport not in ends:
        return None  # hidden-city by where it ends
    if not leg_within_constraints(query, outbound, len(segments) - 1, _iso(segments[0].depart),
                                  _iso(segments[-1].arrive)):
        return None
    return leg


def options(query: SearchQuery, data: dict, direction: str, starts: Set[str], ends: Set[str]) -> List[Option]:
    """One direction's usable flights, cheapest one-way first, one entry per flight."""
    kept: Dict[str, Option] = {}
    for item in (data.get("itineraries") or {}).get(direction) or []:
        key = item.get("flight")
        one_way, round_trip = _dollars(item.get("one_way_price")), _dollars(item.get("min_round_trip_price"))
        if one_way is None and round_trip is None:
            continue
        leg = usable_leg(query, data, key, direction == "outbound", starts, ends)
        if leg is None:
            continue
        found = kept.get(key)
        if found is None:
            kept[key] = Option(key, leg, one_way, round_trip, item.get("data") or "",
                               (data["flights"][key].get("data") or ""))
            continue
        if one_way is not None and (found.one_way is None or one_way < found.one_way):
            found.one_way = one_way
        if round_trip is not None and (found.round_trip is None or round_trip < found.round_trip):
            found.round_trip, found.meta = round_trip, item.get("data") or found.meta
    return sorted(kept.values(), key=lambda option: (option.one_way is None, option.one_way or 0))


Pairs = Dict[Tuple[str, str], Itinerary]


def pair_one_ways(outbound: List[Option], inbound: List[Option]) -> Pairs:
    """Every flight with the other direction's cheapest, priced as two one-way tickets."""
    outs = [o for o in outbound if o.one_way is not None]
    ins = [i for i in inbound if i.one_way is not None]
    if not outs or not ins:
        return {}
    pairs = [(o, ins[0]) for o in outs] + [(outs[0], i) for i in ins[1:]]
    return {(o.key, i.key): Itinerary(o.leg, i.leg, round(o.one_way + i.one_way, 2)) for o, i in pairs}


def round_trip_candidates(outbound: List[Option], inbound: List[Option], limit: int) -> List[Option]:
    """Outbounds whose round-trip ticket could beat their own two one-ways, cheapest first.

    Round-trip prices come in whole dollars, so one within a dollar of the two one-ways
    is usually just their sum rounded, not a cheaper ticket.
    """
    cheapest_return = min((i.one_way for i in inbound if i.one_way is not None), default=None)
    candidates = [o for o in outbound if o.round_trip is not None and o.meta and o.flight_data
                  and (o.one_way is None or cheapest_return is None
                       or o.round_trip < o.one_way + cheapest_return - ROUNDING_MARGIN)]
    return sorted(candidates, key=lambda o: o.round_trip)[:limit]


def round_trip_returns(query: SearchQuery, answer: dict, outbound: Option,
                       starts: Set[str], ends: Set[str]) -> Pairs:
    """An outbound's returns from the returns request, priced as round-trip tickets."""
    pairs: Pairs = {}
    for item in (answer.get("itineraries") or {}).get("outbound") or []:
        if item.get("flight") != outbound.key:
            continue
        for back in item.get("inbound") or []:
            price = _dollars(back.get("round_trip_price"))
            leg = usable_leg(query, answer, back.get("flight"), False, starts, ends) if price is not None else None
            if leg is not None:
                found = pairs.get((outbound.key, back["flight"]))
                if found is None or price < found.price:
                    pairs[(outbound.key, back["flight"])] = Itinerary(outbound.leg, leg, price)
    return pairs


def merge(into: Pairs, more: Pairs) -> None:
    """Add pairings, keeping the cheaper price where the same two flights appear twice."""
    for key, itinerary in more.items():
        if key not in into or itinerary.price < into[key].price:
            into[key] = itinerary


class SkiplaggedScraper(HttpSource):
    name = "skiplagged"
    polite = True  # read directly, like Kayak
    default_headers = {"User-Agent": USER_AGENT}

    def __init__(self, *args, sleep=time.sleep, **kwargs):
        super().__init__(*args, **kwargs)
        self.sleep = sleep

    def itineraries(self, query: SearchQuery) -> List[Itinerary]:
        data = self.request("GET", ENDPOINT, params={
            "from": query.origin, "to": query.destination,
            "depart": query.depart_date.isoformat(), "return": query.return_date.isoformat(),
            "format": "v3", "counts[adults]": 1, "counts[children]": 0,
        })
        if not isinstance(data, dict) or "itineraries" not in data:
            raise ScraperError("skiplagged: answer had no itineraries")
        origins, destinations = _ends(data, "from", query.origin), _ends(data, "to", query.destination)
        outbound = options(query, data, "outbound", origins, destinations)
        inbound = options(query, data, "inbound", destinations, origins)
        pairs = pair_one_ways(outbound, inbound)
        candidates = round_trip_candidates(outbound, inbound, env_count(ROUND_TRIPS_ENV, 3))
        tickets = 0
        for candidate in candidates:
            answer = self._returns(query, candidate)
            if answer:
                found = round_trip_returns(query, answer, candidate, destinations, origins)
                tickets += len(found)
                merge(pairs, found)
        if candidates:
            logger.info("%s: %d round-trip ticket(s) with their returns, from %d outbound(s) looked up",
                        redact.label(query), tickets, len(candidates))
        return list(pairs.values())

    def _returns(self, query: SearchQuery, outbound: Option) -> Optional[dict]:
        """Ask for an outbound's round-trip returns as the site does, until the list is
        complete. A failure costs only these returns, unless the site is refusing us."""
        answer = None
        try:
            for poll in range(RETURN_POLLS):
                if poll:
                    self.sleep(POLL_PAUSE_S)
                answer = self.request("POST", RETURNS_ENDPOINT,
                                      params={"return": query.return_date.isoformat(), "format": "v3", "poll": poll},
                                      body={"trip": outbound.flight_data, "tripMeta": outbound.meta})
                if not isinstance(answer, dict) or not answer.get("show_loading"):
                    break
        except BotBlockedError:
            raise
        except ScraperError as error:
            logger.warning("skiplagged: round-trip returns for one outbound failed: %s", error)
            return None
        return answer if isinstance(answer, dict) else None
