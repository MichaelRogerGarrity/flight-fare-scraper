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
    """ANA leaves Tokyo 16:50 and lands in DC 15:40 the same local day. Timestamps are
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
