import copy
import json
import logging
from pathlib import Path

import pytest

from flight_fare_scraper.config import build_query
from flight_fare_scraper.scrapers.kayak import (
    KayakScraper, _flag, _page_number, _Pass, merge_offers, pages_to_fetch, within_constraints,
)
from tests.factories import make_result

FIXTURE = Path(__file__).parent / "fixtures" / "kayak_poll_page.json"
QUERY = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false")


@pytest.fixture(scope="module")
def payload():
    # A real page-1 poll response for MIA-TYO 2026-11-27/2026-12-13, trimmed to 4 results.
    return json.loads(FIXTURE.read_text())


def parse(payload, query=QUERY):
    return list(KayakScraper()._parse(query, payload))


def results(payload):
    return [result for _, _, result in parse(payload)]


def test_each_round_trip_booking_option_becomes_one_row(payload):
    offers = parse(payload)
    assert len(offers) == 8
    assert len({booking_id for _, booking_id, _ in offers}) == 8


def test_self_transfer_itinerary_with_an_airport_change(payload):
    key, _, row = parse(payload)[0]

    assert key == (
        "FLLNRT1795820400000YP1361795939200000YP7312",
        "NRTMIA1797195600000ZG261797238800000AS6531797249600000AS143",
        "KIWIVI", 1020,
    )
    assert (row.booking_provider, row.price, row.cabin_class) == ("Kiwi.com", 1020, "Economy")
    assert (row.outbound_airline, row.outbound_operated_by, row.outbound_equipment) == (
        "AIR PREMIA", None, "Boeing 787-9 Dreamliner")
    assert (row.outbound_stops, row.outbound_layover_airports, row.outbound_layover_min) == (1, "ICN", 200)
    assert row.outbound_airport_change is False
    assert (row.outbound_depart, row.outbound_duration_min) == ("2026-11-27T23:35:00", 1305)
    # Lands at SFO, then the next flight leaves from SJC.
    assert (row.return_airline, row.return_stops, row.return_layover_airports) == ("ZIPAIR", 2, "SFO,SAN")
    assert row.return_layover_min == 1219 + 99
    assert row.return_airport_change is True
    assert (row.return_arrive, row.return_duration_min) == ("2026-12-14T21:00:00", 2255)
    assert (row.carry_on_status, row.carry_on_fee) == ("INCLUDED", 0.0)
    assert (row.checked_bag_status, row.checked_bag_fee, row.second_checked_bag_fee) == ("FEE", 176, None)
    # This booking's flags object has no isFreeCancellation key at all.
    assert (row.virtual_interline, row.self_transfer_protection, row.free_cancellation) == (True, True, False)
    assert row.booking_url == "https://www.kayak.com/book/flight?code=fixture-1"


def test_resellers_of_one_itinerary_are_separate_offers(payload):
    offers = parse(payload)[1:5]
    assert [row.booking_provider for _, _, row in offers] == ["Trip.com", "Expedia", "Orbitz", "Orbitz"]
    assert [row.price for _, _, row in offers] == [1156, 1283, 1283, 1283]
    assert len({key for key, _, _ in offers}) == 4  # two Orbitz storefronts, distinct provider codes
    assert {row.return_airline for _, _, row in offers} == {"Jeju Air"}


def test_bag_statuses_and_fees_stay_separate_from_the_fare(payload):
    budget_air, kiwi = results(payload)[5:7]
    assert (budget_air.price, budget_air.carry_on_status, budget_air.carry_on_fee) == (1034, "UNKNOWN", None)
    assert (budget_air.checked_bag_status, budget_air.checked_bag_fee) == ("INCLUDED", 0.0)
    assert (kiwi.price, kiwi.carry_on_status, kiwi.carry_on_fee) == (1104, "FEE", 104)
    assert (kiwi.checked_bag_status, kiwi.checked_bag_fee) == ("FEE", 211)
    assert (kiwi.return_layover_airports, kiwi.return_layover_min) == ("LAX,DEN", 295 + 47)


def test_operated_by_reflects_the_first_segment_of_each_leg(payload):
    row = results(payload)[7]
    # The Air Canada Express - Jazz codeshare is the third return segment, so it isn't reported.
    assert (row.return_airline, row.return_operated_by) == ("WestJet", None)
    assert (row.return_layover_airports, row.return_layover_min, row.return_airport_change) == ("YYC,YYZ", 210, False)
    assert row.free_cancellation is True


def test_cabin_class_covers_every_segment_not_just_the_first(payload):
    mixed = copy.deepcopy(payload)
    mixed["results"][0]["bookingOptions"][0]["legFarings"][1]["segmentFarings"][1]["cabinDisplay"] = "First"
    assert results(mixed)[0].cabin_class == "Economy,First"


def test_booking_options_that_are_not_round_trips_are_skipped_loudly(payload, caplog):
    one_leg = copy.deepcopy(payload)
    booking = one_leg["results"][0]["bookingOptions"][0]
    booking["legFarings"] = booking["legFarings"][:1]

    with caplog.at_level(logging.WARNING, logger="flight_fare_scraper"):
        assert len(results(one_leg)) == 7
    assert "weren't a two-leg round trip" in caplog.text


def test_flags_missing_from_a_present_flags_object_are_false():
    assert _flag({"hasVirtualInterline": True}, "hasVirtualInterline") is True
    assert _flag({"isSelfTransferProtection": False}, "isSelfTransferProtection") is False
    assert _flag({"hasVirtualInterline": True}, "isFreeCancellation") is False
    assert _flag(None, "isFreeCancellation") is None


def test_empty_filtered_search_is_an_empty_page_one_not_a_timeout():
    # The shape Kayak returned live for MIA-BKK with stops=0: no nonstops exist on that route.
    empty = {"status": "complete", "filteredCount": 0, "totalCount": 2167, "results": []}
    assert _page_number(empty) == 1
    assert _page_number({"pageNumber": 3, "filteredCount": 939, "results": [{}]}) == 3
    assert _page_number({"status": "complete", "filteredCount": 12, "results": [{}]}) is None
    assert pages_to_fetch(empty["filteredCount"], 50) == (0, 0)


def test_price_prediction_includes_its_horizon(payload):
    assert KayakScraper._price_prediction(payload) == ("WAIT", 170.0, 15)
    assert KayakScraper._price_prediction({}) == (None, None, None)


@pytest.mark.parametrize("filtered_count, expected", [
    (946, (19, 19)),
    (1000, (20, 20)),
    (1001, (21, 20)),
    (1632, (33, 20)),
    (10, (1, 1)),
    (0, (0, 0)),
])
def test_pages_to_fetch_fetches_every_page_up_to_the_cap(filtered_count, expected):
    assert pages_to_fetch(filtered_count, 50, 20) == expected


def test_merge_keeps_duplicates_within_a_search_but_not_across_searches():
    bundle_a, bundle_b, other, nonstop_copy, nonstop_only = (make_result(price=p) for p in (1, 1, 2, 1, 3))
    main = [(("o", "r", "EXP", 1), bundle_a), (("o", "r", "EXP", 1), bundle_b), (("o2", "r", "EXP", 2), other)]
    nonstop = [(("o", "r", "EXP", 1), nonstop_copy), (("o3", "r3", "EXP", 3), nonstop_only)]

    merged = merge_offers([main, nonstop])

    assert [id(row) for row in merged] == [id(bundle_a), id(bundle_b), id(other), id(nonstop_only)]


def test_build_url_encodes_every_filter_in_the_verified_format():
    scraper = KayakScraper()
    base = "https://www.kayak.com/flights/MIA-TYO/2026-11-27/2026-12-13?sort=price_a"
    assert scraper.build_url(QUERY) == base
    assert scraper.build_url(QUERY, nonstop=True) == base + "&fs=stops=0"

    windows = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false",
                          outbound_takeoff="0600,2359", return_takeoff="0600,2359",
                          outbound_landing="+1@1200,+1@2359", return_landing="1700,2359")
    assert scraper.build_url(windows) == (
        base + "&fs=takeoff=0600,2359__0600,2359;landing=1128@1200,1128@2359__1213@1700,1213@2359"
    )

    return_only = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false", return_landing="1700,2359")
    assert scraper.build_url(return_only) == base + "&fs=landing=__1213@1700,1213@2359"


def test_rows_outside_requested_filters_are_rejected():
    sunday_night = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false", return_landing="1700,2359")
    assert within_constraints(sunday_night, make_result(return_arrive="2026-12-13T18:29:00"))
    assert not within_constraints(sunday_night, make_result(return_arrive="2026-12-13T16:59:00"))
    assert not within_constraints(sunday_night, make_result(return_arrive="2026-12-14T18:29:00"))

    nonstop = build_query("MIA", "TYO", "2026-11-27", "2026-12-13")
    assert not within_constraints(nonstop, make_result(outbound_stops=0, return_stops=1))
    assert within_constraints(nonstop, make_result(outbound_stops=0, return_stops=0))


class _ConnectedBrowser:
    def is_connected(self):
        return True


def scripted_scraper(passes):
    scraper = KayakScraper(max_pages=20, pass_jitter_s=(0, 0))
    scraper._browser = _ConnectedBrowser()
    calls = []

    def run_pass(query, nonstop):
        calls.append(nonstop)
        return passes[nonstop]

    scraper._run_pass = run_pass
    return scraper, calls


def test_a_nonstop_pass_runs_only_when_results_exceed_the_page_cap():
    cheap, direct = make_result(price=900.0), make_result(price=1500.0, outbound_stops=0, return_stops=0)
    shallow = _Pass([(("a", "b", "X", 900.0), cheap)], 946, 19, "WAIT", 170.0, 15)
    deep = _Pass([(("a", "b", "X", 900.0), cheap)], 1200, 24, "BUY", 0.0, 7)
    nonstop_pass = _Pass([(("c", "d", "Y", 1500.0), direct)], 10, 1, "UNDECIDED", 0.0, 0)

    scraper, calls = scripted_scraper({False: shallow, True: nonstop_pass})
    assert [row.price for row in scraper.search(QUERY)] == [900.0]
    assert calls == [False]

    scraper, calls = scripted_scraper({False: deep, True: nonstop_pass})
    rows = scraper.search(QUERY)
    assert calls == [False, True]
    assert [row.price for row in rows] == [900.0, 1500.0]
    # The forecast describes the whole search, so the main pass's call applies to every row.
    assert {(row.price_prediction, row.price_prediction_days) for row in rows} == {("BUY", 7)}

    nonstop_only = build_query("MIA", "TYO", "2026-11-27", "2026-12-13")
    scraper, calls = scripted_scraper({False: _Pass([], 1200, 24, None, None, None), True: nonstop_pass})
    scraper.search(nonstop_only)
    assert calls == [False]


def test_a_route_with_no_nonstops_still_succeeds_when_it_needs_a_nonstop_pass():
    cheap = make_result(price=747.0)
    deep = _Pass([(("a", "b", "X", 747.0), cheap)], 1632, 33, "WAIT", 50.0, 10)
    no_nonstops = _Pass([], 0, 0, None, None, None)

    scraper, calls = scripted_scraper({False: deep, True: no_nonstops})

    assert [row.price for row in scraper.search(QUERY)] == [747.0]
    assert calls == [False, True]


def test_the_nonstop_pass_starts_after_a_random_pause(monkeypatch):
    pauses = []
    monkeypatch.setattr("flight_fare_scraper.scrapers.kayak.time.sleep", pauses.append)
    deep = _Pass([], 1200, 24, None, None, None)
    scraper, calls = scripted_scraper({False: deep, True: _Pass([], 0, 0, None, None, None)})
    scraper.pass_jitter_s = (5.0, 15.0)

    scraper.search(QUERY)

    assert calls == [False, True]
    assert len(pauses) == 1 and 5.0 <= pauses[0] <= 15.0
