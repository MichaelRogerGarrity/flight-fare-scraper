from datetime import date, datetime
from typing import List

import pandas as pd

from .models import SearchQuery
from .windows import parse_optional_window

REQUIRED_COLUMNS = ("origin", "destination", "depart", "return_date")
OPTIONAL_COLUMNS = (
    "nonstop", "outbound_takeoff", "return_takeoff", "outbound_landing", "return_landing", "site",
)


def parse_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%Y-%m-%d").date()


def parse_nonstop(text: str) -> bool:
    value = (text or "").strip().lower()
    if value in ("", "true"):
        return True
    if value == "false":
        return False
    raise ValueError(f"nonstop must be 'true', 'false', or blank (meaning true), got {text!r}")


def build_query(
    origin: str,
    destination: str,
    depart: str,
    return_date: str,
    nonstop: str = "",
    outbound_takeoff: str = "",
    return_takeoff: str = "",
    outbound_landing: str = "",
    return_landing: str = "",
    site: str = "",
) -> SearchQuery:
    query = SearchQuery(
        origin=origin.strip().upper(),
        destination=destination.strip().upper(),
        depart_date=parse_date(depart),
        return_date=parse_date(return_date),
        nonstop_only=parse_nonstop(nonstop),
        outbound_takeoff=parse_optional_window(outbound_takeoff, allow_day_offset=False),
        return_takeoff=parse_optional_window(return_takeoff, allow_day_offset=False),
        outbound_landing=parse_optional_window(outbound_landing, allow_day_offset=True),
        return_landing=parse_optional_window(return_landing, allow_day_offset=True),
        site=site.strip() or "kayak",
    )
    if not query.origin or not query.destination:
        raise ValueError("origin and destination are required")
    if query.return_date < query.depart_date:
        raise ValueError(f"return_date {query.return_date} is before depart {query.depart_date}")
    return query


def load_queries(path: str) -> List[SearchQuery]:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, comment="#")
    frame.columns = [column.strip() for column in frame.columns]

    # Unknown columns are an error rather than ignored, so a retired or misspelled
    # column (e.g. the old `takeoff_window`) can't silently drop a filter.
    unknown = sorted(set(frame.columns) - set(REQUIRED_COLUMNS) - set(OPTIONAL_COLUMNS))
    if unknown:
        raise ValueError(
            f"{path}: unknown column(s) {unknown}; allowed columns are {list(REQUIRED_COLUMNS + OPTIONAL_COLUMNS)}"
        )
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"{path}: missing required column(s) {missing}")

    queries = []
    for line, row in enumerate(frame.to_dict("records"), start=2):
        try:
            queries.append(build_query(**row))
        except ValueError as error:
            raise ValueError(f"{path} line {line}: {error}") from None
    return queries
