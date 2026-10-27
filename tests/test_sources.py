"""The Google Flights sources: one itinerary model, four ways of filling it.

Responses here follow each provider's documented shape (SerpApi's and SearchApi's docs,
FlightPowers' openapi.json, fli 0.9.0's models); none of these tests calls a real API.
"""

import io
import json
import sys
import types
import urllib.error
from datetime import date, datetime

import pytest

from flight_fare_scraper.config import build_query
from flight_fare_scraper.runner import run_queries
from flight_fare_scraper.scrapers import SCRAPERS, get_scraper_class
from flight_fare_scraper.scrapers.base import (BotBlockedError, ScraperError, SearchTimeoutError,
                                               SourceUnavailableError)
from flight_fare_scraper.scrapers.flightpowers import FlightPowersScraper, parse_description, parse_duration
from flight_fare_scraper.scrapers.google_flights import Itinerary, Leg, Segment, airports_for, to_result
from flight_fare_scraper.scrapers.serp_google import SearchApiScraper, SerpApiScraper
from tests.factories import make_result
from tests.test_runner import ScriptedScraper

QUERY = build_query("MIA", "TYO", "2026-11-27", "2026-12-13", nonstop="false")
NONSTOP = build_query("MIA", "MCO", "2026-11-27", "2026-11-29", nonstop="true")


def test_every_source_is_registered_by_name():
    assert set(SCRAPERS) == {"kayak", "serpapi", "searchapi", "flightpowers", "fli"}
    assert get_scraper_class("serpapi") is SerpApiScraper


def test_metro_codes_become_their_airports_and_airports_stay_themselves():
    assert airports_for("TYO") == ["HND", "NRT"]
    assert airports_for("bkk") == ["BKK"]  # an airport's own code, as on Kayak


# --- the shared converter ----------------------------------------------------------------

def _segment(start, end, depart, arrive, minutes, airline="ANA", cabin="Economy"):
    return Segment(airline=airline, flight_number="NH 1", from_airport=start, to_airport=end,
                   depart=datetime.fromisoformat(depart), arrive=datetime.fromisoformat(arrive),
                   duration_min=minutes, aircraft="Boeing 787", cabin=cabin)


def test_a_connection_fills_the_layover_fields():
    outbound = Leg(segments=[_segment("MIA", "JFK", "2026-11-27T08:00", "2026-11-27T10:30", 210),
                             _segment("LGA", "HND", "2026-11-27T12:00", "2026-11-28T15:00", 780, cabin="Premium")])
    inbound = Leg(segments=[_segment("HND", "MIA", "2026-12-13T18:00", "2026-12-13T19:00", 840)])
    row = to_result(QUERY, Itinerary(outbound, inbound, 1178, price_level="low"), "serpapi")
    assert (row.site, row.origin, row.depart_date, row.price) == ("serpapi", "MIA", "2026-11-27", 1178.0)
    assert row.outbound_stops == 1 and row.outbound_layover_airports == "JFK"
    assert row.outbound_layover_min == 90 and row.outbound_airport_change is True
    assert row.outbound_duration_min == 210 + 780 + 90
    assert row.outbound_depart == "2026-11-27T08:00:00" and row.outbound_arrive == "2026-11-28T15:00:00"
    assert (row.outbound_from, row.outbound_to) == ("MIA", "HND")
    assert row.return_stops == 0 and row.return_layover_min is None and row.return_airport_change is None
    assert row.cabin_class == "Economy,Premium" and row.price_prediction == "low"


def test_a_summary_leg_needs_no_segments():
    leg = Leg(airline="ANA", from_airport="MIA", to_airport="HND", stops=0, duration_min=900,
              depart=datetime(2026, 11, 27, 8, 0))
    row = to_result(QUERY, Itinerary(leg, leg, 900.0), "flightpowers")
    assert (row.outbound_airline, row.outbound_from, row.outbound_stops, row.outbound_duration_min) == \
        ("ANA", "MIA", 0, 900)


# --- SerpApi and SearchApi -----------------------------------------------------------------

def _serp_item(start, end, depart, arrive, price, token=None, layover=None, date_split=False):
    def end_point(code, moment):
        if date_split:  # SearchApi's shape
            day, clock = moment.split(" ")
            return {"id": code, "name": code, "date": day, "time": clock}
        return {"id": code, "name": code, "time": moment}
    flights = [{"departure_airport": end_point(start, depart), "arrival_airport": end_point(end, arrive),
                "duration": 600, "airplane": "Boeing 787", "airline": "ANA", "flight_number": "NH 9",
                "travel_class": "Economy"}]
    item = {"flights": flights, "layovers": [layover] if layover else [], "total_duration": 600, "price": price}
    if token:
        item["departure_token"] = token
    return item


class Recorder:
    """Stands in for ApiScraper.request: answers by departure token, and keeps each call."""

    def __init__(self, first, returns):
        self.first, self.returns, self.calls = first, returns, []

    def __call__(self, method, url, params=None, body=None, headers=None):
        self.calls.append({"method": method, "url": url, "params": params, "body": body, "headers": headers})
        token = (params or {}).get("departure_token")
        return self.returns[token] if token else self.first


def _serp(cls, monkeypatch, recorder):
    monkeypatch.setenv(cls.key_env, "test-key")
    scraper = cls().__enter__()
    monkeypatch.setattr(scraper, "request", recorder)
    return scraper


def test_serpapi_pairs_the_cheapest_outbound_with_its_returns(monkeypatch):
    first = {"best_flights": [_serp_item("MIA", "HND", "2026-11-27 08:00", "2026-11-28 15:00", 1400, "t-dear"),
                              _serp_item("MIA", "NRT", "2026-11-27 09:00", "2026-11-28 16:00", 1100, "t-cheap")],
             "price_insights": {"price_level": "typical"}}
    returns = {"t-cheap": {"other_flights": [
        _serp_item("NRT", "MIA", "2026-12-13 17:00", "2026-12-13 16:00", 1100),
        _serp_item("HND", "MIA", "2026-12-13 20:00", "2026-12-13 19:00", 1250)]}}
    recorder = Recorder(first, returns)
    rows = _serp(SerpApiScraper, monkeypatch, recorder).search(QUERY)
    assert sorted(row.price for row in rows) == [1100.0, 1250.0]  # the returns' prices are round-trip totals
    assert {row.outbound_to for row in rows} == {"NRT"} and {row.price_prediction for row in rows} == {"typical"}
    first_call = recorder.calls[0]["params"]
    assert first_call["arrival_id"] == "HND,NRT" and first_call["stops"] == 0 and first_call["type"] == 1
    assert first_call["api_key"] == "test-key" and len(recorder.calls) == 2


def test_more_outbound_options_cost_more_requests(monkeypatch):
    monkeypatch.setenv("FFS_GOOGLE_OUTBOUND_OPTIONS", "2")
    first = {"best_flights": [_serp_item("MIA", "HND", "2026-11-27 08:00", "2026-11-28 15:00", 1400, "a"),
                              _serp_item("MIA", "NRT", "2026-11-27 09:00", "2026-11-28 16:00", 1100, "b")]}
    back = {"best_flights": [_serp_item("HND", "MIA", "2026-12-13 17:00", "2026-12-13 16:00", 1500)]}
    recorder = Recorder(first, {"a": back, "b": back})
    rows = _serp(SerpApiScraper, monkeypatch, recorder).search(QUERY)
    assert len(recorder.calls) == 3 and len(rows) == 2


def test_searchapi_sends_its_key_as_a_header_and_reads_split_dates(monkeypatch):
    first = {"best_flights": [_serp_item("MIA", "MCO", "2026-11-27 08:00", "2026-11-27 09:00", 180, "t",
                                         date_split=True)]}
    returns = {"t": {"best_flights": [_serp_item("MCO", "MIA", "2026-11-29 18:00", "2026-11-29 19:00", 190,
                                                 date_split=True)]}}
    recorder = Recorder(first, returns)
    rows = _serp(SearchApiScraper, monkeypatch, recorder).search(NONSTOP)
    assert len(rows) == 1 and rows[0].outbound_depart == "2026-11-27T08:00:00"
    call = recorder.calls[0]
    assert call["headers"] == {"Authorization": "Bearer test-key"} and "api_key" not in call["params"]
    assert call["params"]["stops"] == "nonstop" and call["params"]["flight_type"] == "round_trip"


def test_results_outside_the_querys_filters_are_dropped(monkeypatch):
    stop = {"duration": 60, "id": "ATL", "name": "ATL"}
    first = {"best_flights": [_serp_item("MIA", "MCO", "2026-11-27 08:00", "2026-11-27 09:00", 180, "t")]}
    returns = {"t": {"best_flights": [_serp_item("MCO", "MIA", "2026-11-29 18:00", "2026-11-29 19:00", 190,
                                                 layover=stop)]}}
    first["best_flights"][0]["flights"].append(first["best_flights"][0]["flights"][0])  # a connection
    scraper = _serp(SerpApiScraper, monkeypatch, Recorder(first, returns))
    assert scraper.search(NONSTOP) == []
    assert scraper.last_stats["rows_dropped_by_filters"] == 1


def test_no_results_is_an_empty_answer_not_an_error(monkeypatch):
    first = {"error": "Google Flights hasn't returned any results for this query."}
    assert _serp(SerpApiScraper, monkeypatch, Recorder(first, {})).search(QUERY) == []


def test_a_missing_key_makes_the_source_unavailable(monkeypatch):
    monkeypatch.delenv(SerpApiScraper.key_env, raising=False)
    with pytest.raises(SourceUnavailableError, match="FFS_SERPAPI_KEY"):
        SerpApiScraper().__enter__().search(QUERY)


# --- HTTP errors ----------------------------------------------------------------------------

def _http_error(code, body=b'{"error": "Your account has run out of searches."}'):
    return urllib.error.HTTPError("https://serpapi.com/search?api_key=secret", code, "x", {}, io.BytesIO(body))


@pytest.mark.parametrize("raised, expected", [
    (_http_error(401), SourceUnavailableError),
    (_http_error(429), SourceUnavailableError),
    (_http_error(500), ScraperError),
    (urllib.error.URLError("https://serpapi.com/search?api_key=secret unreachable"), SearchTimeoutError),
])
def test_http_failures_become_the_runners_errors_without_the_key(monkeypatch, raised, expected):
    def fail(*_args, **_kwargs):
        raise raised
    monkeypatch.setattr("urllib.request.urlopen", fail)
    monkeypatch.setenv(SerpApiScraper.key_env, "secret")
    with pytest.raises(expected) as caught:
        SerpApiScraper().__enter__().search(QUERY)
    assert "secret" not in str(caught.value)


# --- FlightPowers -----------------------------------------------------------------------------

def test_flightpowers_descriptions_and_durations_parse():
    assert parse_description("8:35 PM on Fri, Nov 27", date(2026, 11, 27)) == datetime(2026, 11, 27, 20, 35)
    assert parse_description("6:10 AM on Sat, Jan 2", date(2026, 12, 31)) == datetime(2027, 1, 2, 6, 10)
    assert parse_description("whenever", date(2026, 11, 27)) is None
    assert (parse_duration("13 hr 5 min"), parse_duration("1 hr"), parse_duration("45 min")) == (785, 60, 45)


def _fp_item(start, end):
    return {"from_airport": start, "to_airport": end, "total_price_as_number": 1234.0,
            "price_range_in_relation_to_other_periods": "high", "buy_link": "https://example.com/x",
            "departure_flight_airline": "ANA", "departure_flight_stops": 0, "departure_flight_duration": "13 hr",
            "departure_flight_departure_description": "11:00 AM on Fri, Nov 27",
            "departure_flight_arrival_description": "3:00 PM on Sat, Nov 28",
            "return_flight_airline": "JAL", "return_flight_stops": 1, "return_flight_duration": "15 hr 30 min",
            "return_flight_departure_description": "5:00 PM on Sun, Dec 13",
            "return_flight_arrival_description": "3:30 PM on Sun, Dec 13"}


def test_flightpowers_searches_a_metro_airport_by_airport(monkeypatch):
    calls = []

    def answer(method, url, params=None, body=None, headers=None):
        calls.append(body)
        return [_fp_item(body["from_airport"], body["to_airport"])]
    monkeypatch.setenv(FlightPowersScraper.key_env, "k")
    scraper = FlightPowersScraper().__enter__()
    monkeypatch.setattr(scraper, "request", answer)
    rows = scraper.search(QUERY)
    assert [call["to_airport"] for call in calls] == ["HND", "NRT"]
    assert len(rows) == 2 and rows[0].outbound_airline == "ANA" and rows[0].return_stops == 1
    assert rows[0].return_from == rows[0].outbound_to and rows[0].price_prediction == "high"
    assert rows[0].outbound_arrive == "2026-11-28T15:00:00"


# --- fli ------------------------------------------------------------------------------------

def _fake_fli(monkeypatch, combos=None, error=None):
    """Just enough of fli 0.9.0 for the adapter: enums, filter classes and SearchFlights."""
    import enum

    class Airport(enum.Enum):
        MIA = "Miami"
        HND = "Haneda"
        NRT = "Narita"

    class Airline(enum.Enum):
        NH = "ANA"

    class SearchClientError(Exception):
        pass

    class SearchHTTPError(SearchClientError):
        def __init__(self, message, status_code=None):
            super().__init__(message)
            self.status_code = status_code

    class Record:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class SearchFlights:
        def search(self, filters, top_n):
            if error:
                raise error(SearchHTTPError, SearchClientError)
            return combos

    models = types.ModuleType("fli.models")
    for name in ("FlightSearchFilters", "FlightSegment", "PassengerInfo"):
        setattr(models, name, Record)
    models.Airport, models.Airline = Airport, Airline
    models.MaxStops = enum.Enum("MaxStops", "ANY NON_STOP")
    models.SeatType = enum.Enum("SeatType", "ECONOMY")
    models.SortBy = enum.Enum("SortBy", "CHEAPEST")
    models.TripType = enum.Enum("TripType", "ROUND_TRIP")
    search = types.ModuleType("fli.search")
    search.SearchFlights = SearchFlights
    exceptions = types.ModuleType("fli.search.exceptions")
    exceptions.SearchClientError, exceptions.SearchHTTPError = SearchClientError, SearchHTTPError
    for name, module in (("fli", types.ModuleType("fli")), ("fli.models", models), ("fli.search", search),
                         ("fli.search.exceptions", exceptions)):
        monkeypatch.setitem(sys.modules, name, module)
    return Airport, Airline, Record


def _fli_direction(Airport, Airline, Record, start, end, depart, arrive, price):
    leg = Record(airline=Airline.NH, flight_number="1", departure_airport=Airport[start], arrival_airport=Airport[end],
                 departure_datetime=datetime.fromisoformat(depart), arrival_datetime=datetime.fromisoformat(arrive),
                 duration=780, aircraft="Boeing 787", operating_airline=None)
    return Record(legs=[leg], price=price, currency="USD", duration=780, stops=0, layovers=None)


def test_fli_round_trips_are_priced_by_their_return(monkeypatch):
    Airport, Airline, Record = _fake_fli(monkeypatch)
    outbound = _fli_direction(Airport, Airline, Record, "MIA", "HND", "2026-11-27T11:00", "2026-11-28T15:00", 999)
    back = _fli_direction(Airport, Airline, Record, "HND", "MIA", "2026-12-13T17:00", "2026-12-13T15:00", 1180)
    _fake_fli(monkeypatch, combos=[(outbound, back)])
    from flight_fare_scraper.scrapers.fli_source import FliScraper
    rows = FliScraper().search(QUERY)
    assert len(rows) == 1 and rows[0].price == 1180.0 and rows[0].outbound_airline == "ANA"
    assert rows[0].site == "fli" and FliScraper.polite


def test_fli_refusals_and_silence_count_against_the_breaker(monkeypatch):
    from flight_fare_scraper.scrapers.fli_source import FliScraper
    _fake_fli(monkeypatch, error=lambda http, _client: http("blocked", status_code=429))
    with pytest.raises(BotBlockedError):
        FliScraper().search(QUERY)
    _fake_fli(monkeypatch, combos=None)
    with pytest.raises(SearchTimeoutError):
        FliScraper().search(QUERY)


def test_fli_not_installed_makes_the_source_unavailable(monkeypatch):
    from flight_fare_scraper.scrapers.fli_source import FliScraper
    monkeypatch.setitem(sys.modules, "fli.models", None)
    with pytest.raises(SourceUnavailableError, match="pip install flights"):
        FliScraper().search(QUERY)


# --- in the runner --------------------------------------------------------------------------

def test_an_unavailable_source_skips_its_own_searches_and_not_the_others():
    api_queries = [build_query("MIA", "TYO", f"2026-12-0{d}", f"2026-12-1{d}", nonstop="false", site="serpapi")
                   for d in (1, 2, 3)]
    kayak_query = build_query("MIA", "MCO", "2026-12-04", "2026-12-06")
    api = ScriptedScraper({api_queries[0].label: [SourceUnavailableError("serpapi: no API key")]})
    api.polite = False
    kayak = ScriptedScraper({kayak_query.label: [[make_result()]]})
    sleeps = []
    report = run_queries(api_queries + [kayak_query], delays=(15, 60), sleep=sleeps.append, search_jitter_s=None,
                         scraper_for=lambda site: lambda headless: api if site == "serpapi" else kayak,
                         breaker_after=5)
    assert api.calls == [api_queries[0].label]  # tried once, not retried
    assert [q.label for q in report.skipped] == [q.label for q in api_queries[1:]]
    assert report.succeeded == [kayak_query] and report.stopped == ""
    assert sleeps == []


def test_the_hourly_cap_leaves_metered_apis_alone():
    queries = [build_query("MIA", "TYO", f"2026-12-0{d}", f"2026-12-1{d}", nonstop="false", site="serpapi")
               for d in (1, 2, 3)]
    api = ScriptedScraper({q.label: [[make_result()]] for q in queries})
    api.polite = False
    sleeps = []
    run_queries(queries, sleep=sleeps.append, search_jitter_s=None, min_gap_s=240,
                scraper_for=lambda site: lambda headless: api)
    assert sleeps == []
