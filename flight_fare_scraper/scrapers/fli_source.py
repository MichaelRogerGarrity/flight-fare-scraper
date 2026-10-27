"""Google Flights read directly, through the open-source `fli` library.

Free, and the same data the paid APIs resell -- but it is this machine asking Google,
so the run's load limits apply to it like Kayak (`polite`), and a block here is a block
on this connection's Google access, not just on flight searches. Google refused
GitHub's runners within a few searches (docs/backup-fare-sources.md); from a home
connection it has not been tested at volume. Keep it to small numbers.

Optional: `pip install flights` (tested with 0.9.0). A round trip costs
1 + FFS_GOOGLE_OUTBOUND_OPTIONS requests to Google.
"""

from typing import List

from ..models import SearchQuery
from .base import BotBlockedError, ScraperError, SearchTimeoutError, SourceUnavailableError
from .google_flights import Itinerary, ItinerarySource, Leg, Segment, airports_for, outbound_options


def _leg(result) -> Leg:
    """A fli FlightResult (one direction) as a Leg."""
    segments = [
        Segment(
            airline=getattr(leg.airline, "value", None), flight_number=leg.flight_number,
            from_airport=leg.departure_airport.name, to_airport=leg.arrival_airport.name,
            depart=leg.departure_datetime, arrive=leg.arrival_datetime, duration_min=leg.duration,
            aircraft=leg.aircraft,
            operated_by=getattr(leg.operating_airline, "value", None)
            if leg.operating_airline and leg.operating_airline != leg.airline else None,
        )
        for leg in result.legs
    ]
    layovers = result.layovers or []
    return Leg(segments=segments, duration_min=result.duration, stops=result.stops,
               layover_airports=[layover.airport.name for layover in layovers] or None,
               layover_min=sum(layover.duration for layover in layovers) if layovers else None)


class FliScraper(ItinerarySource):
    name = "fli"
    polite = True  # this machine is the one asking Google

    def itineraries(self, query: SearchQuery) -> List[Itinerary]:
        try:
            from fli.models import (Airport, FlightSearchFilters, FlightSegment, MaxStops, PassengerInfo,
                                    SeatType, SortBy, TripType)
            from fli.search import SearchFlights
            from fli.search.exceptions import SearchClientError, SearchHTTPError
        except ImportError:
            raise SourceUnavailableError("fli: not installed (pip install flights)") from None
        try:
            origins = [[Airport[code], 0] for code in airports_for(query.origin)]
            destinations = [[Airport[code], 0] for code in airports_for(query.destination)]
        except KeyError as error:
            raise ScraperError(f"fli: unknown airport code {error}") from None
        filters = FlightSearchFilters(
            trip_type=TripType.ROUND_TRIP,
            passenger_info=PassengerInfo(adults=1),
            flight_segments=[
                FlightSegment(departure_airport=origins, arrival_airport=destinations,
                              travel_date=query.depart_date.isoformat()),
                FlightSegment(departure_airport=destinations, arrival_airport=origins,
                              travel_date=query.return_date.isoformat()),
            ],
            seat_type=SeatType.ECONOMY,
            stops=MaxStops.NON_STOP if query.nonstop_only else MaxStops.ANY,
            sort_by=SortBy.CHEAPEST,
        )
        options = outbound_options()
        self.requests_made += 1 + options
        try:
            combos = SearchFlights().search(filters, top_n=options)
        except SearchHTTPError as error:
            if error.status_code in (403, 429):
                raise BotBlockedError(f"fli: Google refused the search (HTTP {error.status_code})") from None
            raise ScraperError(f"fli: HTTP {error.status_code}") from None
        except SearchClientError as error:
            raise SearchTimeoutError(f"fli: {type(error).__name__}") from None
        if not combos:
            # Google answers a connection it has stopped serving with empty results, so an
            # empty answer counts as a failure: five in a row trip the breaker.
            raise SearchTimeoutError("fli: no results")
        found = []
        for combo in combos:
            outbound, inbound = combo[0], combo[-1]
            # The return's price is the round trip's: Google prices returns as totals.
            price = inbound.price if inbound.price is not None else outbound.price
            if price is not None and len(combo) == 2:
                found.append(Itinerary(_leg(outbound), _leg(inbound), float(price), currency=outbound.currency or "USD"))
        return found
