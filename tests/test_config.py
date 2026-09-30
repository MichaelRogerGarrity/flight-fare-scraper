from datetime import time

import pytest

from flight_fare_scraper.config import build_query, load_queries


def write_csv(tmp_path, text):
    path = tmp_path / "routes.csv"
    path.write_text(text)
    return str(path)


def test_loads_windows_and_applies_defaults(tmp_path):
    path = write_csv(
        tmp_path,
        "origin,destination,depart,return_date,nonstop,outbound_takeoff,return_takeoff,outbound_landing,return_landing,site\n"
        'mia,tyo,2026-11-27,2026-12-13,false,"0600,2359",,"+1@1200,+1@2359","1700,2359",\n'
        "MIA,BKK,2026-11-13,2026-11-22,,,,,,kayak\n",
    )
    tokyo, bangkok = load_queries(path)

    assert (tokyo.origin, tokyo.destination, tokyo.nonstop_only, tokyo.site) == ("MIA", "TYO", False, "kayak")
    assert tokyo.outbound_takeoff.start == time(6, 0)
    assert tokyo.return_takeoff is None
    assert (tokyo.outbound_landing.start_day, tokyo.outbound_landing.start) == (1, time(12, 0))
    assert tokyo.return_landing.start == time(17, 0)
    assert bangkok.nonstop_only is True
    assert bangkok.outbound_landing is None


def test_retired_takeoff_window_column_is_rejected_rather_than_silently_ignored(tmp_path):
    path = write_csv(tmp_path, 'origin,destination,depart,return_date,takeoff_window\nMIA,TYO,2026-11-27,2026-12-13,"1600,2300"\n')
    with pytest.raises(ValueError, match="takeoff_window"):
        load_queries(path)


def test_missing_required_column_is_rejected(tmp_path):
    path = write_csv(tmp_path, "origin,destination,depart\nMIA,TYO,2026-11-27\n")
    with pytest.raises(ValueError, match="return_date"):
        load_queries(path)


def test_row_errors_name_the_offending_line(tmp_path):
    path = write_csv(tmp_path, "origin,destination,depart,return_date,nonstop\nMIA,TYO,2026-11-27,2026-12-13,yes\n")
    with pytest.raises(ValueError, match="line 2.*nonstop"):
        load_queries(path)


def test_return_before_departure_is_rejected():
    with pytest.raises(ValueError, match="before depart"):
        build_query("MIA", "TYO", "2026-12-13", "2026-11-27")


def test_takeoff_window_cannot_use_a_day_offset():
    with pytest.raises(ValueError, match="day offset"):
        build_query("MIA", "TYO", "2026-11-27", "2026-12-13", outbound_takeoff="+1@0600,+1@1200")
