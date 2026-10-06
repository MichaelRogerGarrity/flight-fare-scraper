import json
from datetime import date, timedelta

import pytest

from flight_fare_scraper import schedule

FRIDAY = date(2026, 10, 2)  # a Friday, used as the base weekend throughout

SPEC = {
    "tiers": [
        {"max_days_out": 30, "every_days": 1, "day_offsets": [-1, 0, 1]},
        {"max_days_out": 90, "every_days": 3, "day_offsets": [-1, 0, 1]},
        {"max_days_out": 365, "every_days": 7, "day_offsets": [0]},
    ],
    "routes": [{"origin": "mia", "destination": "mco", "nights": 2, "nonstop": True}],
}


def spec(**overrides):
    return schedule.parse_spec(json.dumps({**SPEC, **overrides}))


def test_parse_spec_normalizes_codes_and_json_booleans():
    parsed = spec()
    (route,) = parsed.routes
    assert (route.origin, route.destination) == ("MIA", "MCO")
    assert route.options["nonstop"] == "true"


def test_parse_spec_rejects_unknown_route_keys():
    bad = {**SPEC, "routes": [{**SPEC["routes"][0], "takeoff_window": "0600,1200"}]}
    with pytest.raises(ValueError, match="takeoff_window"):
        schedule.parse_spec(json.dumps(bad))


def test_parse_spec_rejects_malformed_input():
    with pytest.raises(ValueError, match="valid JSON"):
        schedule.parse_spec("{not json")
    with pytest.raises(ValueError, match="routes"):
        schedule.parse_spec(json.dumps({"tiers": SPEC["tiers"]}))


def test_base_weekends_are_fridays_inside_the_horizon():
    parsed = spec(horizon_days=30)
    weekends = schedule.base_weekends(parsed, date(2026, 9, 19))
    assert weekends[0] == date(2026, 9, 25)
    assert all(day.weekday() == schedule.FRIDAY for day in weekends)
    assert all((day - date(2026, 9, 19)).days <= 30 for day in weekends)


def test_nine_combinations_per_weekend_in_the_near_tier():
    # 20 days out -> daily tier, three departure offsets by three return offsets.
    # min_nights=0 keeps the same-day combination, so the full grid is visible.
    queries = schedule.due_today(spec(min_nights=0), FRIDAY - timedelta(days=20))
    for_this_weekend = [
        query for query in queries
        if abs((query.depart_date - FRIDAY).days) <= 1
    ]
    assert len(for_this_weekend) == 9
    assert {(query.depart_date - FRIDAY).days for query in for_this_weekend} == {-1, 0, 1}
    assert {(query.return_date - FRIDAY).days for query in for_this_weekend} == {1, 2, 3}


def test_far_tier_scans_only_the_base_weekend():
    parsed = spec()
    # The phase depends on the weekend, so find the day in the far tier when it is due
    # rather than assuming one.
    for days_out in range(180, 187):
        today = FRIDAY - timedelta(days=days_out)
        queries = [query for query in schedule.due_today(parsed, today) if query.depart_date == FRIDAY]
        if queries:
            break
    else:
        raise AssertionError("the weekend never came up across a full weekly interval")

    assert len(queries) == 1  # day_offsets is [0] in the far tier, so no slack days
    assert queries[0].return_date == FRIDAY + timedelta(days=2)


def test_a_weekend_comes_up_on_its_tier_interval():
    parsed = spec()
    due = [
        days_out for days_out in range(31, 91)
        if any(query.depart_date == FRIDAY for query in
               schedule.due_today(parsed, FRIDAY - timedelta(days=days_out)))
    ]
    # Every third day, never two days running. The phase depends on which weekend
    # this is, so assert the interval rather than the specific days.
    assert due, "the weekend never came up"
    assert {later - earlier for earlier, later in zip(due, due[1:])} == {3}


def test_tiers_spread_their_weekends_across_the_week():
    """Regression: base weekends are a whole number of weeks apart, so keying due-ness
    on days_out alone gave every weekend in a tier the same phase. The whole weekly
    tier landed on one weekday and the other six saw none of it."""
    parsed = spec()
    far_per_day = [
        len([query for query in schedule.due_today(parsed, date(2026, 9, 20) + timedelta(days=offset))
             if (query.depart_date - (date(2026, 9, 20) + timedelta(days=offset))).days > 90])
        for offset in range(7)
    ]
    assert all(count > 0 for count in far_per_day), f"a day saw no far-tier weekends: {far_per_day}"
    assert max(far_per_day) <= 3 * min(far_per_day), f"far tier is lumpy: {far_per_day}"


def test_every_weekend_in_the_horizon_gets_scanned():
    parsed = spec()
    start = date(2026, 9, 20)
    scanned = {
        query.depart_date
        for offset in range(8)
        for query in schedule.due_today(parsed, start + timedelta(days=offset))
    }
    # Weekly is the slowest tier, so one week must cover every weekend in the horizon.
    for weekend in schedule.base_weekends(parsed, start + timedelta(days=7)):
        assert weekend in scanned, f"{weekend} was never scanned in a full week"


def test_min_days_out_excludes_departures_that_are_too_close():
    parsed = spec(min_days_out=2)
    today = FRIDAY - timedelta(days=2)
    departures = {query.depart_date for query in schedule.due_today(parsed, today)}
    assert FRIDAY - timedelta(days=1) not in departures  # only 1 day out
    assert FRIDAY in departures


def test_returns_never_precede_departure():
    parsed = spec(routes=[{"origin": "MIA", "destination": "MCO", "nights": 0, "nonstop": True}])
    queries = schedule.due_today(parsed, FRIDAY - timedelta(days=10))
    assert queries, "expected some searches"
    assert all(query.return_date >= query.depart_date for query in queries)


def test_shard_splits_every_query_exactly_once():
    queries = schedule.due_today(spec(), date(2026, 9, 19))
    shards = [schedule.shard(queries, index, 3) for index in range(3)]
    assert sum(len(part) for part in shards) == len(queries)
    assert sorted(id(query) for part in shards for query in part) == sorted(id(query) for query in queries)
    assert max(len(part) for part in shards) - min(len(part) for part in shards) <= 1


def test_shard_rejects_an_out_of_range_index():
    with pytest.raises(ValueError):
        schedule.shard([], 3, 3)


MIXED = {
    **SPEC,
    "routes": [
        {"origin": "MIA", "destination": "MCO", "nights": 2, "nonstop": True},
        {"origin": "MIA", "destination": "TPA", "nights": 2, "nonstop": True},
        {"origin": "MIA", "destination": "SIN", "nights": 9, "nonstop": False},
        {"origin": "MIA", "destination": "TYO", "nights": 16, "nonstop": False},
        {"origin": "FLL", "destination": "TYO", "nights": 16, "nonstop": False},
        {"origin": "PBI", "destination": "TYO", "nights": 16, "nonstop": False},
    ],
}


def test_every_shard_carries_an_even_share_of_every_route():
    """Regression: interleaving a date-sorted list repeats per date, so with six routes
    and three shards whole routes landed together -- one shard got the light routes and
    another the heavy ones, and the heavy runner was the one the site stopped answering."""
    queries = schedule.due_today(schedule.parse_spec(json.dumps(MIXED)), date(2026, 9, 30))
    routes = {(q.origin, q.destination) for q in queries}
    for count in (3, 5, 6):
        for route in routes:
            per_shard = [
                sum(1 for q in schedule.shard(queries, index, count) if (q.origin, q.destination) == route)
                for index in range(count)
            ]
            assert max(per_shard) - min(per_shard) <= 1, f"{route} split {per_shard} across {count}"


def test_a_shard_runs_its_searches_in_date_order():
    queries = schedule.due_today(schedule.parse_spec(json.dumps(MIXED)), date(2026, 9, 30))
    part = schedule.shard(queries, 1, 4)
    order = [queries.index(q) for q in part]
    assert order == sorted(order)


@pytest.mark.parametrize("total, limit, expected", [(0, 70, 1), (70, 70, 1), (71, 70, 2), (414, 70, 6), (468, 70, 7)])
def test_shards_needed_keeps_every_shard_under_the_limit(total, limit, expected):
    assert schedule.shards_needed(total, limit) == expected


def test_shards_needed_rejects_a_zero_limit():
    with pytest.raises(ValueError):
        schedule.shards_needed(10, 0)


def test_same_day_round_trips_are_not_searched_by_default():
    """A 2-night weekend with a day of slack at each end includes leaving a day late and
    returning a day early -- a same-day turnaround, not a trip anyone means."""
    queries = schedule.due_today(spec(), FRIDAY - timedelta(days=10))
    assert queries
    assert all((q.return_date - q.depart_date).days >= 1 for q in queries)
    near = [q for q in queries if abs((q.depart_date - FRIDAY).days) <= 1]
    assert len(near) == 8  # nine combinations less the same-day one


def test_min_nights_can_be_raised_or_lowered():
    stays = lambda parsed: {(q.return_date - q.depart_date).days
                            for q in schedule.due_today(parsed, FRIDAY - timedelta(days=10))
                            if abs((q.depart_date - FRIDAY).days) <= 1}
    assert stays(spec(min_nights=0)) == {0, 1, 2, 3, 4}
    assert stays(spec(min_nights=2)) == {2, 3, 4}
