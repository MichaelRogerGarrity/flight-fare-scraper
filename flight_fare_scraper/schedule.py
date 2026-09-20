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
    {"max_days_out": 365, "every_days": 7, "day_offsets": [0]},
)
ROUTE_PASSTHROUGH = (
    "nonstop", "outbound_takeoff", "return_takeoff", "outbound_landing", "return_landing", "site",
)


@dataclass(frozen=True)
class Tier:
    max_days_out: int
    every_days: int
    day_offsets: Tuple[int, ...]


@dataclass(frozen=True)
class Route:
    origin: str
    destination: str
    nights: int
    options: Dict[str, str]


@dataclass(frozen=True)
class Spec:
    routes: Tuple[Route, ...]
    tiers: Tuple[Tier, ...]
    base_weekday: int = FRIDAY
    horizon_days: int = DEFAULT_HORIZON_DAYS
    min_days_out: int = 1


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
        unknown = sorted(set(entry) - {"origin", "destination", "nights"} - set(ROUTE_PASSTHROUGH))
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
        ))

    tiers = []
    for index, entry in enumerate(raw.get("tiers", DEFAULT_TIERS)):
        where = f"route spec: tiers[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        every = int(_require(entry, "every_days", where))
        if every < 1:
            raise ValueError(f"{where}: 'every_days' must be at least 1")
        offsets = entry.get("day_offsets", [0])
        if not isinstance(offsets, list) or not offsets:
            raise ValueError(f"{where}: 'day_offsets' must be a non-empty list")
        tiers.append(Tier(
            max_days_out=int(_require(entry, "max_days_out", where)),
            every_days=every,
            day_offsets=tuple(int(offset) for offset in offsets),
        ))
    if not tiers:
        raise ValueError("route spec: 'tiers' must not be empty")
    tiers.sort(key=lambda tier: tier.max_days_out)

    return Spec(
        routes=tuple(routes),
        tiers=tuple(tiers),
        base_weekday=int(raw.get("base_weekday", FRIDAY)),
        horizon_days=int(raw.get("horizon_days", DEFAULT_HORIZON_DAYS)),
        min_days_out=int(raw.get("min_days_out", 1)),
    )


def base_weekends(spec: Spec, today: date) -> List[date]:
    """Every base-weekday date from min_days_out to horizon_days out, inclusive."""
    first = today + timedelta(days=spec.min_days_out)
    first += timedelta(days=(spec.base_weekday - first.weekday()) % 7)
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


def due_today(spec: Spec, today: date) -> List[SearchQuery]:
    """The searches to run today. A weekend's turn comes up every tier.every_days."""
    queries = []
    for weekend in base_weekends(spec, today):
        days_out = (weekend - today).days
        tier = tier_for(spec, days_out)
        if tier is None or days_out % tier.every_days != 0:
            continue
        for route in spec.routes:
            for depart_offset in tier.day_offsets:
                depart = weekend + timedelta(days=depart_offset)
                if (depart - today).days < spec.min_days_out:
                    continue
                for return_offset in tier.day_offsets:
                    returns = weekend + timedelta(days=route.nights + return_offset)
                    if returns < depart:
                        continue
                    queries.append(build_query(
                        route.origin, route.destination,
                        depart.isoformat(), returns.isoformat(),
                        **route.options,
                    ))
    queries.sort(key=lambda query: (query.depart_date, query.return_date, query.origin, query.destination))
    return queries


def shard(queries: Sequence[SearchQuery], index: int, count: int) -> List[SearchQuery]:
    """Split a run across jobs. Interleaving keeps each shard's route mix even."""
    if count < 1 or not 0 <= index < count:
        raise ValueError(f"shard {index}/{count} is out of range")
    return [query for position, query in enumerate(queries) if position % count == index]
