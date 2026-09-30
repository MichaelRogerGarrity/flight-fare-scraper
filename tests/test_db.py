from datetime import date, datetime, timezone

import duckdb
import pytest

from flight_fare_scraper import db
from tests.factories import make_result

EVENING = datetime(2026, 9, 13, 20, 0)


@pytest.fixture
def con(tmp_path):
    connection = db.connect(str(tmp_path / "fares.duckdb"))
    yield connection
    connection.close()


def fetch_rows(con, where=""):
    cursor = con.execute(f"SELECT * FROM fares {where}")
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def assert_row_matches(row, result):
    for name in db.RESULT_COLUMNS:
        expected = getattr(result, name)
        if expected is not None and db.COLUMN_TYPES[name] == "DATE":
            expected = date.fromisoformat(expected)
        elif expected is not None and db.COLUMN_TYPES[name] == "TIMESTAMP":
            expected = datetime.fromisoformat(expected)
        assert row[name] == expected, name


def index_oids(con):
    return dict(con.execute("SELECT index_name, index_oid FROM duckdb_indexes() WHERE table_name = 'fares'").fetchall())


def test_every_field_round_trips_into_its_own_column(con):
    result = make_result()
    db.insert_snapshot(con, [result], snapshot_at=EVENING)

    (row,) = fetch_rows(con)
    assert_row_matches(row, result)
    assert "booking_url" not in row


def test_rerunning_the_same_day_replaces_rather_than_duplicates(con):
    db.insert_snapshot(con, [make_result(price=1178.0), make_result(price=1200.0)], snapshot_at=EVENING)
    db.insert_snapshot(con, [make_result(price=999.0)], snapshot_at=EVENING.replace(hour=23))

    assert [row["price"] for row in fetch_rows(con)] == [999.0]


def test_same_route_from_another_site_is_not_deleted(con):
    db.insert_snapshot(con, [make_result(site="kayak")], snapshot_at=EVENING)
    db.insert_snapshot(con, [make_result(site="othersite")], snapshot_at=EVENING)

    assert sorted(row["site"] for row in fetch_rows(con)) == ["kayak", "othersite"]


def test_other_days_and_routes_accumulate(con):
    db.insert_snapshot(con, [make_result()], snapshot_at=EVENING)
    db.insert_snapshot(con, [make_result()], snapshot_at=EVENING.replace(day=14))
    db.insert_snapshot(con, [make_result(destination="BKK")], snapshot_at=EVENING.replace(day=14))

    assert len(fetch_rows(con)) == 3


def test_snapshot_uses_the_eastern_calendar_day(con):
    late_evening_eastern = datetime(2026, 9, 13, 1, 30, tzinfo=timezone.utc)
    db.insert_snapshot(con, [make_result()], snapshot_at=late_evening_eastern)

    (row,) = fetch_rows(con)
    assert row["snapshot_date"] == date(2026, 9, 12)
    assert row["snapshot_datetime"] == datetime(2026, 9, 12, 21, 30)


def test_failed_insert_leaves_the_days_earlier_rows_intact(con):
    db.insert_snapshot(con, [make_result()], snapshot_at=EVENING)

    with pytest.raises(duckdb.Error):
        db.insert_snapshot(con, [make_result(outbound_depart="not-a-timestamp")], snapshot_at=EVENING)

    assert len(fetch_rows(con)) == 1


def test_empty_snapshot_touches_nothing(con):
    db.insert_snapshot(con, [make_result()], snapshot_at=EVENING)
    assert db.insert_snapshot(con, [], snapshot_at=EVENING) == 0
    assert len(fetch_rows(con)) == 1


def test_reconnecting_to_a_current_schema_rebuilds_no_indexes(con):
    before = index_oids(con)
    db.ensure_schema(con)
    assert index_oids(con) == before


def test_only_a_stale_index_definition_is_rebuilt(con):
    con.execute("DROP INDEX idx_route_window")
    con.execute("CREATE INDEX idx_route_window ON fares(origin, destination)")
    untouched = {name: oid for name, oid in index_oids(con).items() if name != "idx_route_window"}

    db.ensure_schema(con)

    assert db._current_indexes(con) == db.INDEXES
    assert {name: oid for name, oid in index_oids(con).items() if name != "idx_route_window"} == untouched


def test_migrates_the_previous_schema_in_place(tmp_path):
    path = str(tmp_path / "legacy.duckdb")
    legacy = duckdb.connect(path)
    legacy.execute(
        "CREATE TABLE fares (snapshot_date DATE, snapshot_datetime TIMESTAMP, site VARCHAR, origin VARCHAR, "
        "destination VARCHAR, depart_date DATE, return_date DATE, price DOUBLE, currency VARCHAR, "
        "booking_provider VARCHAR, outbound_airline VARCHAR, outbound_operated_by VARCHAR, "
        "outbound_depart TIMESTAMP, outbound_arrive TIMESTAMP, outbound_stops INTEGER, outbound_duration_min INTEGER, "
        "return_airline VARCHAR, return_operated_by VARCHAR, return_depart TIMESTAMP, return_arrive TIMESTAMP, "
        "return_stops INTEGER, return_duration_min INTEGER, booking_url VARCHAR)"
    )
    for column in ["outbound_layover_airports VARCHAR", "outbound_layover_min INTEGER", "outbound_equipment VARCHAR",
                   "return_layover_airports VARCHAR", "return_layover_min INTEGER", "return_equipment VARCHAR",
                   "cabin_class VARCHAR", "carry_on_included BOOLEAN", "checked_bag_fee DOUBLE",
                   "free_cancellation BOOLEAN", "is_split_ticket BOOLEAN", "price_prediction VARCHAR",
                   "price_prediction_change DOUBLE"]:
        legacy.execute(f"ALTER TABLE fares ADD COLUMN {column}")
    legacy.execute("CREATE INDEX idx_route_window ON fares(origin, destination, depart_date, return_date)")
    legacy.execute(
        "INSERT INTO fares (snapshot_date, snapshot_datetime, site, origin, destination, depart_date, return_date, "
        "price, booking_url, carry_on_included) VALUES "
        "('2026-09-11', '2026-09-11 18:02:12', 'kayak', 'SEA', 'MCO', '2026-11-21', '2026-11-28', 377, 'https://old', true)"
    )
    legacy.close()

    con = db.connect(path)
    try:
        assert sorted(row["column_name"] for row in [
            {"column_name": name} for (name,) in con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE table_name = 'fares'").fetchall()
        ]) == sorted(db.COLUMNS)
        assert db._current_indexes(con) == db.INDEXES
        assert con.execute("SELECT origin, price FROM fares").fetchall() == [("SEA", 377.0)]

        result = make_result()
        db.insert_snapshot(con, [result], snapshot_at=EVENING)
        (row,) = fetch_rows(con, "WHERE origin = 'MIA'")
        assert_row_matches(row, result)
    finally:
        con.close()
