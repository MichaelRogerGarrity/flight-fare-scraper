"""Filters every source applies to its results, whatever the site already filtered."""

from datetime import datetime
from typing import Optional

from ..models import FlightResult, SearchQuery


def within_constraints(query: SearchQuery, result: FlightResult) -> bool:
    """Whether a result honours the query's stops and time windows.

    Sites don't always: Kayak returns rows outside its own filters, and the APIs take
    hour-level windows at best, so each row is checked again here.
    """
    return (leg_within_constraints(query, True, result.outbound_stops, result.outbound_depart, result.outbound_arrive)
            and leg_within_constraints(query, False, result.return_stops, result.return_depart, result.return_arrive))


def leg_within_constraints(query: SearchQuery, outbound: bool, stops: Optional[int],
                           depart: Optional[str], arrive: Optional[str]) -> bool:
    """The same check for one direction, for sources that pair the legs themselves."""
    if query.nonstop_only and stops:
        return False
    if outbound:
        leg_date, takeoff, landing = query.depart_date, query.outbound_takeoff, query.outbound_landing
    else:
        leg_date, takeoff, landing = query.return_date, query.return_takeoff, query.return_landing
    for window, moment in ((takeoff, depart), (landing, arrive)):
        if window is None:
            continue
        if moment is None or not window.contains(leg_date, datetime.fromisoformat(moment)):
            return False
    return True
