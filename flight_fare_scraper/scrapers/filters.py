"""Filters every source applies to its results, whatever the site already filtered."""

from datetime import datetime

from ..models import FlightResult, SearchQuery


def within_constraints(query: SearchQuery, result: FlightResult) -> bool:
    """Whether a result honours the query's stops and time windows.

    Sites don't always: Kayak returns rows outside its own filters, and the APIs take
    hour-level windows at best, so each row is checked again here.
    """
    if query.nonstop_only and (result.outbound_stops or result.return_stops):
        return False
    checks = (
        (query.outbound_takeoff, query.depart_date, result.outbound_depart),
        (query.return_takeoff, query.return_date, result.return_depart),
        (query.outbound_landing, query.depart_date, result.outbound_arrive),
        (query.return_landing, query.return_date, result.return_arrive),
    )
    for window, leg_date, moment in checks:
        if window is None:
            continue
        if moment is None or not window.contains(leg_date, datetime.fromisoformat(moment)):
            return False
    return True
