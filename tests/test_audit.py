from datetime import date

import duckdb
import pytest

from flight_fare_scraper import audit, db


@pytest.fixture
def con():
    connection = duckdb.connect()
    db.ensure_schema(connection)
    yield connection
    connection.close()


def insert(con, **overrides):
    row = {
        "snapshot_date": date(2026, 9, 24), "snapshot_datetime": "2026-09-24 08:00:00",
        "site": "kayak", "origin": "MIA", "destination": "TYO",
        "depart_date": date(2026, 11, 27), "return_date": date(2026, 12, 13),
        "price": 1200.0, "currency": "USD",
        "outbound_airline": "ANA", "return_airline": "ANA",
        "outbound_depart": "2026-11-27 10:00:00", "outbound_arrive": "2026-11-28 14:00:00",
        "outbound_duration_min": 840, "outbound_stops": 0,
        "return_depart": "2026-12-13 16:50:00", "return_arrive": "2026-12-13 15:40:00",
        "return_duration_min": 770, "return_stops": 0,
        "cabin_class": "Economy",
    }
    row.update(overrides)
    names = ", ".join(f'"{k}"' for k in row)
    placeholders = ", ".join("?" for _ in row)
    con.execute(f"INSERT INTO fares ({names}) VALUES ({placeholders})", list(row.values()))


def counts(con):
    return dict(audit.anomalies(con, "fares"))


def test_a_dateline_crossing_is_not_an_anomaly(con):
    """An eastbound transpacific leg can leave at 16:50 and land at 15:40 the same local day. Timestamps are
    local wall-clock, so arrive < depart is correct here, not corrupt."""
    insert(con)
    assert counts(con)["timestamps disagree with duration"] == 0


def test_a_duration_that_contradicts_the_timestamps_is_caught(con):
    # 770 -> 900 makes the implied offset 16h20m: no such timezone gap exists.
    insert(con, return_duration_min=900)
    assert counts(con)["timestamps disagree with duration"] == 1


def test_an_off_grid_offset_is_caught(con):
    # Shift arrival by 7 minutes so the implied offset is not a multiple of 15.
    insert(con, return_arrive="2026-12-13 15:47:00")
    assert counts(con)["timestamps disagree with duration"] == 1


def test_a_connection_without_a_layover_is_caught(con):
    insert(con, outbound_stops=1, outbound_layover_min=None)
    assert counts(con)["connection with no layover recorded"] == 1


def test_a_nonstop_claiming_a_layover_is_caught(con):
    insert(con, outbound_stops=0, outbound_layover_min=95)
    assert counts(con)["nonstop with a layover"] == 1


def test_implausible_prices_are_caught(con):
    insert(con, price=5.0)
    insert(con, price=99999.0)
    result = counts(con)
    assert result["price below $20"] == 1
    assert result["price above $20000"] == 1


def test_shape_measures_reseller_inflation(con):
    for _ in range(4):
        insert(con)  # same itinerary, four sellers
    insert(con, outbound_airline="United", price=1300.0)
    facts = audit.shape(con, "fares")
    assert facts["distinct_itineraries"] == 2
    assert facts["rows_per_itinerary"] == pytest.approx(2.5)


def test_null_rates_flag_a_column_that_went_empty(con):
    insert(con, cabin_class=None)
    rates = dict(audit.null_rates(con, "fares"))
    assert rates["cabin_class"] == 1.0
    assert rates["price"] == 0.0


def test_report_fails_on_an_empty_table(con):
    assert audit.report(con, "fares") is False


def test_a_fifteen_hour_clock_gap_is_real(con):
    """A city on winter time 15 hours behind its destination: an 11:00 departure taking
    780 minutes lands at 15:00 the next day local, a wall-clock gap of 1,680 minutes.
    The old 14-hour bound flagged nearly a million correct rows like this."""
    insert(con, outbound_depart="2026-12-04 11:00:00", outbound_arrive="2026-12-05 15:00:00",
           outbound_duration_min=780)
    assert counts(con)["timestamps disagree with duration"] == 0


def test_a_gap_no_two_time_zones_can_have_is_still_caught(con):
    # 27 hours apart: beyond UTC-12 to UTC+14, so the timestamps or duration are wrong.
    insert(con, outbound_depart="2026-12-04 11:00:00", outbound_arrive="2026-12-05 15:00:00",
           outbound_duration_min=1680 + 27 * 60)
    assert counts(con)["timestamps disagree with duration"] == 1


def _fill(con, spec_obj, snapshot, keep=1.0):
    """Insert one row for (a share of) the searches the spec expected on `snapshot`."""
    from flight_fare_scraper import schedule as _schedule
    due = _schedule.due_today(spec_obj, snapshot)
    for query in due[: max(1, int(len(due) * keep))]:
        insert(con, snapshot_date=snapshot, depart_date=query.depart_date, return_date=query.return_date,
               origin=query.origin, destination=query.destination,
               outbound_from="MIA", outbound_to="NRT", return_from="NRT", return_to="MIA")


def _spec():
    import json
    from flight_fare_scraper import schedule as _schedule
    return _schedule.parse_spec(json.dumps(
        {"routes": [{"origin": "MIA", "destination": "TYO", "nights": 16, "nonstop": False}]}))


def test_a_short_day_outside_the_rolling_window_no_longer_fails_the_audit(con):
    """A short day stays short forever. Checking all history failed every weekly audit
    after one bad day; a window matched to the audit's cadence checks each day once."""
    spec_obj = _spec()
    _fill(con, spec_obj, date(2026, 10, 1), keep=0.1)   # badly short, two weeks back
    _fill(con, spec_obj, date(2026, 10, 15))            # complete, latest
    days = [date(2026, 10, 1), date(2026, 10, 15)]
    assert audit.coverage_report(con, "fares", spec_obj, days) is False
    assert audit.coverage_report(con, "fares", spec_obj, days, coverage_days=7) is True


def test_a_short_day_inside_the_window_still_fails_it(con):
    spec_obj = _spec()
    _fill(con, spec_obj, date(2026, 10, 12), keep=0.1)  # short, three days back
    _fill(con, spec_obj, date(2026, 10, 15))
    days = [date(2026, 10, 12), date(2026, 10, 15)]
    assert audit.coverage_report(con, "fares", spec_obj, days, coverage_days=7) is False
