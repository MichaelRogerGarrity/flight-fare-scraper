"""Turn a route spec into the searches that are due today.

The spec lists routes by trip shape (origin, destination, nights) rather than by
date, and this module expands it over a rolling horizon of base weekends. It is
stateless: whether a weekend is due is a function of how far out it is, so a
missed run just means that weekend waits for its next turn.

The spec itself is not in this repo -- it names real airports. It is supplied at
run time from the FFS_ROUTES secret. See examples/routes.spec.example.json.
"""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import build_query
from .models import SearchQuery

FRIDAY = 4  # date.weekday(): Monday is 0
DEFAULT_HORIZON_DAYS = 365
DEFAULT_TIERS = (
    {"max_days_out": 30, "every_days": 1, "day_offsets": [-1, 0, 1]},
    {"max_days_out": 90, "every_days": 3, "day_offsets": [-1, 0, 1]},
    {"max_days_out": 365, "every_days": 8, "day_offsets": [0]},
)
ROUTE_PASSTHROUGH = (
    "nonstop", "outbound_takeoff", "return_takeoff", "outbound_landing", "return_landing", "site",
)


@dataclass(frozen=True)
class Tier:
    max_days_out: int
    every_days: int
    day_offsets: Tuple[int, ...]
    # Date combinations searched per visit, the base trip always among them and the
    # rest rotating; None searches every combination every visit.
    combos_per_visit: Optional[int] = None


@dataclass(frozen=True)
class Route:
    origin: str
    destination: str
    nights: int
    options: Dict[str, str]
    # The weekday trips on this route are built around; None means the spec's default.
    base_weekday: Optional[int] = None


@dataclass(frozen=True)
class Spec:
    routes: Tuple[Route, ...]
    tiers: Tuple[Tier, ...]
    base_weekday: int = FRIDAY
    horizon_days: int = DEFAULT_HORIZON_DAYS
    min_days_out: int = 1
    # Shortest stay searched. The day offsets apply to both ends of a trip, so a
    # 2-night weekend with +/-1 day of slack also produces a same-day turnaround
    # (leave a day late, return a day early) -- about a tenth of a short route's
    # searches, spent on trips nobody takes as a weekend away.
    min_nights: int = 1


def _require(mapping: Dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ValueError(f"{where}: missing required key {key!r}")
    return mapping[key]


def parse_spec(text: str) -> Spec:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"route spec is not valid JSON: {error}") from None
    if not isinstance(raw, dict):
        raise ValueError("route spec must be a JSON object")

    routes = []
    raw_routes = _require(raw, "routes", "route spec")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise ValueError("route spec: 'routes' must be a non-empty list")
    for index, entry in enumerate(raw_routes):
        where = f"route spec: routes[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        unknown = sorted(set(entry) - {"origin", "destination", "nights", "base_weekday"}
                         - set(ROUTE_PASSTHROUGH))
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {unknown}")
        nights = _require(entry, "nights", where)
        if not isinstance(nights, int) or nights < 0:
            raise ValueError(f"{where}: 'nights' must be a non-negative integer, got {nights!r}")
        options = {}
        for key in ROUTE_PASSTHROUGH:
            value = entry.get(key, "")
            if isinstance(value, bool):  # JSON true/false -> the CSV spelling build_query expects
                value = "true" if value else "false"
            options[key] = str(value)
        routes.append(Route(
            origin=str(_require(entry, "origin", where)).strip().upper(),
            destination=str(_require(entry, "destination", where)).strip().upper(),
            nights=nights,
            options=options,
            base_weekday=_weekday(entry["base_weekday"], where) if "base_weekday" in entry else None,
        ))

    return Spec(
        routes=tuple(routes),
        tiers=parse_tiers(raw.get("tiers", DEFAULT_TIERS), "route spec: tiers"),
        base_weekday=_weekday(raw.get("base_weekday", FRIDAY), "route spec"),
        horizon_days=int(raw.get("horizon_days", DEFAULT_HORIZON_DAYS)),
        min_days_out=int(raw.get("min_days_out", 1)),
        min_nights=int(raw.get("min_nights", 1)),
    )


def parse_tiers(entries: Sequence[Any], where_list: str) -> Tuple[Tier, ...]:
    """A list of tier objects, validated and sorted nearest first."""
    if not isinstance(entries, (list, tuple)):
        raise ValueError(f"{where_list} must be a list")
    tiers = []
    for index, entry in enumerate(entries):
        where = f"{where_list}[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        every = int(_require(entry, "every_days", where))
        if every < 1:
            raise ValueError(f"{where}: 'every_days' must be at least 1")
        if every % 7 == 0:
            # A whole number of weeks would check each window on the same weekday every
            # time, so any effect of the day a fare is searched would hide in the data.
            raise ValueError(f"{where}: 'every_days' must not be a multiple of 7, got {every} "
                             f"-- it would check each window on the same weekday every time")
        combos = entry.get("combos_per_visit")
        if combos is not None and (not isinstance(combos, int) or isinstance(combos, bool) or combos < 1):
            raise ValueError(f"{where}: 'combos_per_visit' must be a positive integer, got {combos!r}")
        offsets = entry.get("day_offsets", [0])
        if not isinstance(offsets, list) or not offsets:
            raise ValueError(f"{where}: 'day_offsets' must be a non-empty list")
        tiers.append(Tier(
            max_days_out=int(_require(entry, "max_days_out", where)),
            every_days=every,
            day_offsets=tuple(int(offset) for offset in offsets),
            combos_per_visit=combos,
        ))
    if not tiers:
        raise ValueError(f"{where_list} must not be empty")
    tiers.sort(key=lambda tier: tier.max_days_out)
    return tuple(tiers)


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _weekday(value, where: str) -> int:
    """A weekday as 0-6 (Monday is 0) or a name: "Tuesday", "tue"."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6:
        return value
    if isinstance(value, str):
        name = value.strip().lower()
        for index, day in enumerate(WEEKDAYS):
            if len(name) >= 3 and day.startswith(name):
                return index
    raise ValueError(f"{where}: base_weekday must be 0-6 or a weekday name, got {value!r}")


def base_weekends(spec: Spec, today: date, weekday: Optional[int] = None) -> List[date]:
    """Every base-weekday date from min_days_out to horizon_days out, inclusive.

    `weekday` overrides the spec's default, for a route built around another day.
    """
    weekday = spec.base_weekday if weekday is None else weekday
    first = today + timedelta(days=spec.min_days_out)
    first += timedelta(days=(weekday - first.weekday()) % 7)
    last = today + timedelta(days=spec.horizon_days)
    out = []
    while first <= last:
        out.append(first)
        first += timedelta(days=7)
    return out


def tier_for(spec: Spec, days_out: int) -> Optional[Tier]:
    for tier in spec.tiers:  # sorted by max_days_out
        if days_out <= tier.max_days_out:
            return tier
    return None


def _visit_key(weekend: date, days_out: int) -> int:
    """A per-weekend counter that falls by one each day, offset so that consecutive
    weekends sit in consecutive slots of any interval.

    Weekends are seven days apart, so days_out alone puts them all in one slot of a
    weekly interval, and adding the week number (the first fix) still collapsed every
    even interval: consecutive weekends then differ by 8, which is 0 mod 2 and only
    reaches every other slot of 6 or 12. Subtracting six per week makes consecutive
    weekends differ by exactly one, so they fill every slot of any interval evenly.
    """
    return days_out - 6 * (weekend.toordinal() // 7)


def _is_due(weekend: date, days_out: int, tier: Tier) -> bool:
    """Whether this weekend's turn comes up today. Each weekend comes up exactly every
    tier.every_days days, and a tier's weekends spread evenly across the interval."""
    return _visit_key(weekend, days_out) % tier.every_days == 0


def _combos_this_visit(combos: List[Tuple[int, int]], weekend: date, days_out: int,
                       tier: Tier) -> List[Tuple[int, int]]:
    """Which (depart offset, return offset) pairs to search on this visit.

    With combos_per_visit set, the base trip (0, 0) is searched every visit and the
    others take turns, so each weekend is still priced every visit while the full grid
    is covered every few visits. That cut the grid's cost -- most of the daily volume --
    by roughly a third without checking any weekend less often.
    """
    if not tier.combos_per_visit or len(combos) <= tier.combos_per_visit:
        return combos
    base = [(0, 0)] if (0, 0) in combos else []
    others = [combo for combo in combos if combo != (0, 0)]
    turns = tier.combos_per_visit - len(base)
    visit = -_visit_key(weekend, days_out) // tier.every_days  # rises by one each visit
    start = (visit * turns) % len(others)
    return base + [others[(start + step) % len(others)] for step in range(turns)]


def due_today(spec: Spec, today: date) -> List[SearchQuery]:
    """The searches to run today. A weekend's turn comes up every tier.every_days."""
    queries = []
    for route in spec.routes:
        for weekend in base_weekends(spec, today, route.base_weekday):
            days_out = (weekend - today).days
            tier = tier_for(spec, days_out)
            if tier is None or not _is_due(weekend, days_out, tier):
                continue
            combos = [
                (depart_offset, return_offset)
                for depart_offset in tier.day_offsets
                for return_offset in tier.day_offsets
                if ((weekend + timedelta(days=depart_offset)) - today).days >= spec.min_days_out
                and route.nights + return_offset - depart_offset >= spec.min_nights
            ]
            for depart_offset, return_offset in _combos_this_visit(combos, weekend, days_out, tier):
                depart = weekend + timedelta(days=depart_offset)
                returns = weekend + timedelta(days=route.nights + return_offset)
                queries.append(build_query(
                    route.origin, route.destination,
                    depart.isoformat(), returns.isoformat(),
                    **route.options,
                ))
    queries.sort(key=lambda query: (query.depart_date, query.return_date, query.origin, query.destination))
    return queries


def shard(queries: Sequence[SearchQuery], index: int, count: int) -> List[SearchQuery]:
    """Split a run across jobs so every shard carries an equal share of every route.

    Interleaving the date-sorted list looked balanced but wasn't: with six routes and
    three shards, the pattern repeats per date and hands whole routes to one shard, so
    one got the two light domestic routes and another the heavy long-haul ones. Load
    per runner is what matters -- the site stops answering a runner after enough
    requests -- so each route is dealt out round-robin on its own. The selected
    searches still run in date order.
    """
    if count < 1 or not 0 <= index < count:
        raise ValueError(f"shard {index}/{count} is out of range")
    by_route = sorted(
        range(len(queries)),
        key=lambda i: (queries[i].site, queries[i].origin, queries[i].destination,
                       queries[i].depart_date, queries[i].return_date),
    )
    chosen = {i for position, i in enumerate(by_route) if position % count == index}
    return [query for i, query in enumerate(queries) if i in chosen]


def shards_needed(total: int, max_per_shard: int) -> int:
    """How many sequential jobs keep each one under max_per_shard searches."""
    if max_per_shard < 1:
        raise ValueError(f"max_per_shard must be at least 1, got {max_per_shard}")
    return max(1, -(-total // max_per_shard))
