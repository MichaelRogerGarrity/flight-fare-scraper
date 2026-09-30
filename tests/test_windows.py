from datetime import date, datetime, time

import pytest

from flight_fare_scraper.windows import kayak_landing, kayak_takeoff, parse_optional_window, parse_window


def test_same_day_window_is_inclusive_and_date_bound():
    window = parse_window("1700,2359", allow_day_offset=True)
    sunday = date(2026, 12, 13)
    assert window.contains(sunday, datetime(2026, 12, 13, 17, 0))
    assert window.contains(sunday, datetime(2026, 12, 13, 23, 59))
    assert not window.contains(sunday, datetime(2026, 12, 13, 16, 59))
    assert not window.contains(sunday, datetime(2026, 12, 14, 18, 0))


def test_day_offset_places_the_window_after_departure():
    window = parse_window("+1@1200,+1@2359", allow_day_offset=True)
    assert (window.start_day, window.start) == (1, time(12, 0))
    assert window.contains(date(2026, 11, 27), datetime(2026, 11, 28, 14, 5))
    assert not window.contains(date(2026, 11, 27), datetime(2026, 11, 27, 14, 5))


def test_takeoff_windows_reject_day_offsets():
    with pytest.raises(ValueError, match="day offset"):
        parse_window("+1@0600,+1@1200", allow_day_offset=False)


@pytest.mark.parametrize("text", ["1700", "2400,2359", "17:00,23:59", "2359,1700", "+1@0100,0200"])
def test_malformed_windows_are_rejected(text):
    with pytest.raises(ValueError):
        parse_window(text, allow_day_offset=True)


def test_blank_window_means_no_filter():
    assert parse_optional_window("   ", allow_day_offset=True) is None


def test_encodings_match_the_url_formats_observed_on_kayak():
    assert kayak_takeoff(parse_window("0600,2359", allow_day_offset=False)) == "0600,2359"
    landing = parse_window("+1@1200,+1@2359", allow_day_offset=True)
    assert kayak_landing(landing, date(2026, 11, 27)) == "1128@1200,1128@2359"


def test_landing_encoding_crosses_the_year_boundary():
    landing = parse_window("+1@0000,+1@0600", allow_day_offset=True)
    assert kayak_landing(landing, date(2026, 12, 31)) == "0101@0000,0101@0600"
