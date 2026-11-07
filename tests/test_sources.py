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
    assert set(SCRAPERS) == {"kayak", "serpapi", "searchapi", "flightpowers", "fli", "skiplagged", "opodo", "apify_google"}
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


# --- Skiplagged -----------------------------------------------------------------------------

def _sk_flight(*hops, airline="AA", day="2026-11-27"):
    """A Skiplagged flight from (from, to, depart HH:MM, arrive HH:MM) hops, at -05:00."""
    return {"segments": [{"airline": airline, "flight_number": 100 + n,
                          "departure": {"time": f"{day}T{dep}:00-05:00", "airport": start},
                          "arrival": {"time": f"{day}T{arr}:00-05:00", "airport": end},
                          "duration": 3600}
                         for n, (start, end, dep, arr) in enumerate(hops)]}


def _sk_answer():
    """Shaped like a real format=v3 answer: flights by key, priced per direction in cents."""
    return {
        "airlines": {"AA": {"name": "American Airlines"}},
        "info": {"from": {"airports": ["MIA"]}, "to": {"airports": ["MCO"]}},
        "flights": {
            "o-nonstop": _sk_flight(("MIA", "MCO", "08:00", "09:00")),
            "o-late": _sk_flight(("MIA", "MCO", "20:00", "21:00")),
            "o-hidden": _sk_flight(("MIA", "MCO", "06:00", "07:00"), ("MCO", "ATL", "08:00", "09:30")),
            "o-connect": _sk_flight(("MIA", "TPA", "06:00", "07:00"), ("TPA", "MCO", "08:00", "09:00")),
            "i-nonstop": _sk_flight(("MCO", "MIA", "18:00", "19:00"), day="2026-11-29"),
            "i-dear": _sk_flight(("MCO", "MIA", "12:00", "13:00"), day="2026-11-29"),
        },
        "itineraries": {
            "outbound": [{"flight": "o-hidden", "one_way_price": 3900},  # cheapest, but flies on to ATL
                         {"flight": "o-nonstop", "one_way_price": 8950},
                         {"flight": "o-late", "one_way_price": 9900},
                         {"flight": "o-connect", "one_way_price": 5000}],
            "inbound": [{"flight": "i-nonstop", "one_way_price": 7000},
                        {"flight": "i-dear", "one_way_price": 12000}],
        },
        "incomplete": True,
    }


def _skiplagged(monkeypatch, answer, returns=()):
    """A scraper whose search gets `answer`, and whose returns requests get `returns` in
    turn (an exception in the list is raised). Keeps the main search's params in `calls`
    and each returns request in `scraper.posts`."""
    from flight_fare_scraper.scrapers.skiplagged import SkiplaggedScraper
    calls, replies = [], list(returns)
    scraper = SkiplaggedScraper(sleep=lambda seconds: scraper.slept.append(seconds))
    scraper.posts, scraper.slept = [], []

    def respond(method, url, params=None, body=None, headers=None):
        if method == "GET":
            calls.append(params)
            return answer
        scraper.posts.append({"params": params, "body": body})
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(scraper, "request", respond)
    return scraper, calls


def test_skiplagged_drops_hidden_city_fares_and_prices_two_one_ways(monkeypatch):
    scraper, calls = _skiplagged(monkeypatch, _sk_answer())
    rows = scraper.search(NONSTOP)
    # The connection fails the nonstop filter and the hidden-city fare never counts.
    assert sorted((row.outbound_depart[11:16], row.return_depart[11:16], row.price) for row in rows) == [
        ("08:00", "12:00", 209.5), ("08:00", "18:00", 159.5), ("20:00", "18:00", 169.0)]
    assert {row.outbound_to for row in rows} == {"MCO"} and rows[0].outbound_airline == "American Airlines"
    assert rows[0].site == "skiplagged" and rows[0].outbound_arrive == "2026-11-27T09:00:00"  # local, no offset
    assert calls == [{"from": "MIA", "to": "MCO", "depart": "2026-11-27", "return": "2026-11-29",
                      "format": "v3", "counts[adults]": 1, "counts[children]": 0}]


def test_skiplagged_keeps_connections_when_the_route_allows_them(monkeypatch):
    query = build_query("MIA", "MCO", "2026-11-27", "2026-11-29", nonstop="false")
    rows, _ = _skiplagged(monkeypatch, _sk_answer())
    connecting = sorted((row for row in rows.search(query) if row.outbound_stops), key=lambda row: row.price)
    # Now the cheapest outbound, so it's paired with both returns.
    assert [row.price for row in connecting] == [120.0, 170.0]
    assert connecting[0].outbound_layover_airports == "TPA" and connecting[0].outbound_layover_min == 60


def test_skiplagged_with_no_usable_flight_one_way_is_an_empty_answer(monkeypatch):
    answer = _sk_answer()
    answer["itineraries"]["inbound"] = []
    scraper, _ = _skiplagged(monkeypatch, answer)
    assert scraper.search(NONSTOP) == []


def test_skiplagged_trusts_its_own_hidden_city_mark(monkeypatch):
    answer = _sk_answer()
    answer["flights"]["o-late"]["count"] = 0  # rides fewer flights than the ticket has
    scraper, _ = _skiplagged(monkeypatch, answer)
    assert "20:00" not in {row.outbound_depart[11:16] for row in scraper.search(NONSTOP)}


def _with_round_trip_fares(answer):
    """Two outbounds sold only as round-trip tickets, and one whose ticket costs more
    than its own two one-ways."""
    answer["flights"]["o-rt"] = dict(_sk_flight(("MIA", "MCO", "10:00", "11:00")), data="flight-rt")
    answer["flights"]["o-rt2"] = dict(_sk_flight(("MIA", "MCO", "11:00", "12:00")), data="flight-rt2")
    answer["itineraries"]["outbound"] += [
        {"flight": "o-rt", "min_round_trip_price": 12000, "data": "meta-rt"},
        {"flight": "o-rt2", "min_round_trip_price": 14000, "data": "meta-rt2"}]
    answer["itineraries"]["outbound"][1].update(min_round_trip_price=99000, data="meta-dear")  # o-nonstop
    answer["flights"]["o-nonstop"]["data"] = "flight-nonstop"
    return answer


def _sk_returns(outbound, *backs, loading=False):
    """A returns answer: the outbound, its returns at round-trip prices, and their flights."""
    return {"airlines": {"AA": {"name": "American Airlines"}},
            "flights": {key: _sk_flight(hop, day="2026-11-29") for key, _price, hop in backs},
            "itineraries": {"outbound": [{"flight": outbound, "inbound": [
                {"flight": key, "round_trip_price": price, "data": "x"} for key, price, _hop in backs]}]},
            "show_loading": loading}


def test_skiplagged_prices_round_trip_tickets_with_their_returns(monkeypatch):
    returns = [_sk_returns("o-rt", ("r-1", 12000, ("MCO", "MIA", "17:00", "18:00")), loading=True),
               _sk_returns("o-rt", ("r-1", 12000, ("MCO", "MIA", "17:00", "18:00")),
                           ("r-hidden", 9000, ("MCO", "ATL", "15:00", "16:00"))),
               _sk_returns("o-rt2", ("r-2", 14000, ("MCO", "MIA", "19:00", "20:00")))]
    scraper, _ = _skiplagged(monkeypatch, _with_round_trip_fares(_sk_answer()), returns)
    rows = {(row.outbound_depart[11:16], row.return_depart[11:16]): row.price for row in scraper.search(NONSTOP)}
    assert rows[("10:00", "17:00")] == 120.0 and rows[("11:00", "19:00")] == 140.0
    assert ("10:00", "15:00") not in rows  # its hidden-city return
    assert rows[("08:00", "18:00")] == 159.5  # the dearer ticket was never asked about
    # The first outbound took two asks (the list was still loading), the second one.
    assert [post["body"] for post in scraper.posts] == [{"trip": "flight-rt", "tripMeta": "meta-rt"}] * 2 + [
        {"trip": "flight-rt2", "tripMeta": "meta-rt2"}]
    assert [post["params"]["poll"] for post in scraper.posts] == [0, 1, 0] and scraper.slept == [3.0]
    assert scraper.posts[0]["params"]["return"] == "2026-11-29"


def test_skiplagged_round_trip_lookups_are_capped(monkeypatch):
    monkeypatch.setenv("FFS_SKIPLAGGED_ROUND_TRIPS", "1")
    returns = [_sk_returns("o-rt", ("r-1", 12000, ("MCO", "MIA", "17:00", "18:00")))]
    scraper, _ = _skiplagged(monkeypatch, _with_round_trip_fares(_sk_answer()), returns)
    scraper.search(NONSTOP)
    assert [post["body"]["trip"] for post in scraper.posts] == ["flight-rt"]  # the cheaper ticket first
    monkeypatch.setenv("FFS_SKIPLAGGED_ROUND_TRIPS", "0")
    scraper, _ = _skiplagged(monkeypatch, _with_round_trip_fares(_sk_answer()))
    assert len(scraper.search(NONSTOP)) == 3 and scraper.posts == []  # only the one-way pairings


def test_a_round_trip_price_that_is_only_the_one_ways_rounded_is_not_looked_up(monkeypatch):
    answer = _sk_answer()
    answer["itineraries"]["outbound"][1].update(min_round_trip_price=15900, data="meta")  # 89.50 + 70 = 159.50
    answer["flights"]["o-nonstop"]["data"] = "flight"
    scraper, _ = _skiplagged(monkeypatch, answer)
    scraper.search(NONSTOP)
    assert scraper.posts == []


def test_a_failed_round_trip_lookup_costs_only_its_returns(monkeypatch):
    returns = [ScraperError("skiplagged: HTTP 500"),
               _sk_returns("o-rt2", ("r-2", 14000, ("MCO", "MIA", "19:00", "20:00")))]
    scraper, _ = _skiplagged(monkeypatch, _with_round_trip_fares(_sk_answer()), returns)
    rows = scraper.search(NONSTOP)
    assert {row.outbound_depart[11:16] for row in rows} == {"08:00", "20:00", "11:00"}
    blocked, _ = _skiplagged(monkeypatch, _with_round_trip_fares(_sk_answer()),
                             [BotBlockedError("skiplagged: refused (HTTP 403)")])
    with pytest.raises(BotBlockedError):
        blocked.search(NONSTOP)


def test_skiplagged_challenges_and_refusals_count_as_blocks(monkeypatch):
    from flight_fare_scraper.scrapers.base import CaptchaError
    from flight_fare_scraper.scrapers.skiplagged import SkiplaggedScraper

    class Page:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self.body
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *_a, **_k: Page(b"<html>Please complete the CAPTCHA</html>"))
    with pytest.raises(CaptchaError):
        SkiplaggedScraper().search(NONSTOP)

    def refuse(*_args, **_kwargs):
        raise _http_error(403, b"")
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(BotBlockedError):
        SkiplaggedScraper().search(NONSTOP)
    assert SkiplaggedScraper.polite


# --- Opodo ----------------------------------------------------------------------------------

def _op_answer():
    """Shaped like a real searchItinerary answer: itineraries point at segments (one per
    leg), which point at sections (one per flight), with places and carriers by id."""
    places = {"1": "MIA", "2": "MCO", "3": "FLL", "4": "TPA"}
    sections = {  # id: (from, to, depart, arrive, carrier, operator)
        "s1": ("1", "2", "2026-11-27T08:00:00-05:00", "2026-11-27T09:00:00-05:00", "AA", "AA"),
        "s2": ("2", "1", "2026-11-29T18:00:00-05:00", "2026-11-29T19:00:00-05:00", "AA", "YX"),
        "s3": ("3", "2", "2026-11-27T07:00:00-05:00", "2026-11-27T08:00:00-05:00", "B6", "B6"),
        "s4": ("1", "4", "2026-11-27T06:00:00-05:00", "2026-11-27T07:00:00-05:00", "AA", "AA"),
        "s5": ("4", "2", "2026-11-27T08:00:00-05:00", "2026-11-27T09:00:00-05:00", "AA", "AA"),
    }
    segments = {"g-out": ["s1"], "g-back": ["s2"], "g-fll": ["s3"], "g-connect": ["s4", "s5"]}

    def itinerary(out, back, regular, member):
        return {"legs": [{"segmentKey": "0", "segmentId": out}, {"segmentKey": "0", "segmentId": back}],
                "fees": [{"type": "MEMBER_PRICE_POLICY_UNDISCOUNTED", "price": {"amount": regular, "currency": "EUR"}},
                         {"type": "MEMBER_PRICE_POLICY_DISCOUNTED", "price": {"amount": member, "currency": "EUR"}}]}
    return {
        "itineraries": [itinerary("g-out", "g-back", 200.0, 180.0), itinerary("g-out", "g-back", 190.0, 170.0),
                        itinerary("g-fll", "g-back", 150.0, 140.0), itinerary("g-connect", "g-back", 120.0, 110.0)],
        "segments": [{"id": key, "segment": {"id": key, "sections": ids, "duration": 60 if len(ids) == 1 else 180,
                                             "baggageCondition": "BASED_ON_AIRLINE" if key == "g-back"
                                             else "CABIN_INCLUDED"}}
                     for key, ids in segments.items()],
        "sections": [{"id": key, "section": {"id": key, "departureId": start, "destinationId": end,
                                             "departureDate": dep, "arrivalDate": arr, "duration": 60,
                                             "carrierId": carrier, "operatingCarrierId": operator,
                                             "flightCode": f"{carrier}100", "cabinClass": "TOURIST",
                                             "vehicleModel": "738"}}
                     for key, (start, end, dep, arr, carrier, operator) in sections.items()],
        "locations": [{"id": key, "location": {"id": key, "iata": iata}} for key, iata in places.items()],
        "carriers": [{"id": code, "carrier": {"id": code, "name": name}}
                     for code, name in (("AA", "American Airlines"), ("YX", "Republic Airways"), ("B6", "JetBlue"))],
    }


class _OpPage:
    """A browser page that answers the fares call with the given (status, body) replies."""

    def __init__(self, replies, title="Opodo"):
        self.replies, self.page_title, self.handlers, self.urls = replies, title, [], []

    def on(self, event, handler):
        self.handlers.append(handler)

    def goto(self, url, **_):
        self.urls.append(url)
        for status, body in self.replies:
            response = types.SimpleNamespace(url="https://www.opodo.com/itinerary-selection-bff/service/graphql",
                                             status=status, request=types.SimpleNamespace(method="POST"),
                                             text=lambda body=body: body)
            for handler in self.handlers:
                handler(response)

    def wait_for_timeout(self, _ms):
        pass

    def title(self):
        return self.page_title

    def close(self):
        pass


def _opodo(replies, title="Opodo", rate=lambda: 1.1):
    from flight_fare_scraper.scrapers.opodo import OpodoScraper
    scraper = OpodoScraper(fares_timeout_s=0.05, settle_s=0, usd_rate=rate)
    page = _OpPage(replies, title)
    scraper._context = types.SimpleNamespace(new_page=lambda: page)
    return scraper, page


def _fares(found):
    return 200, json.dumps({"data": {"searchItinerary": found}})


def test_opodo_keeps_the_regular_price_in_dollars_between_the_querys_own_airports():
    scraper, page = _opodo([_fares({"itineraries": []}), _fares(_op_answer())])
    rows = scraper.search(NONSTOP)
    # One row per pair of legs, at its cheaper regular price; the Fort Lauderdale
    # departure is another airport in the city and the connection isn't nonstop.
    assert [(row.price, row.currency) for row in rows] == [(209.0, "USD")]
    row = rows[0]
    assert (row.site, row.booking_provider, row.cabin_class) == ("opodo", "Opodo", "Economy")
    assert row.outbound_airline == "American Airlines" and row.outbound_operated_by is None
    assert row.return_operated_by == "Republic Airways"  # a codeshare
    assert row.outbound_depart == "2026-11-27T08:00:00" and row.return_to == "MIA"  # local, no offset
    assert row.outbound_equipment == "738" and row.outbound_duration_min == 60
    assert (row.carry_on_status, row.carry_on_fee) == ("UNKNOWN", None)  # the return's depends on the airline
    assert page.urls == ["https://www.opodo.com/travel/#results/type=R;from=MIA;to=MCO;"
                         "dep=2026-11-27;ret=2026-11-29"]
    assert scraper.last_stats["main"]["pages_fetched"] == 1


def test_opodo_keeps_connections_when_the_route_allows_them():
    query = build_query("MIA", "MCO", "2026-11-27", "2026-11-29", nonstop="false")
    scraper, _ = _opodo([_fares(_op_answer())])
    connecting = [row for row in scraper.search(query) if row.outbound_stops]
    assert [(row.price, row.outbound_layover_airports, row.outbound_duration_min) for row in connecting] == [
        (132.0, "TPA", 180)]


def test_opodo_keeps_euros_when_there_is_no_rate():
    def no_rate():
        raise OSError("offline")
    scraper, _ = _opodo([_fares(_op_answer())], rate=no_rate)
    assert [(row.price, row.currency) for row in scraper.search(NONSTOP)] == [(190.0, "EUR")]


def test_opodo_both_cabin_bags_included():
    answer = _op_answer()
    for segment in answer["segments"]:
        segment["segment"]["baggageCondition"] = "CABIN_INCLUDED"
    scraper, _ = _opodo([_fares(answer)])
    assert [(row.carry_on_status, row.carry_on_fee) for row in scraper.search(NONSTOP)] == [("INCLUDED", 0.0)]


def test_opodo_challenges_and_silence():
    from flight_fare_scraper.scrapers.base import CaptchaError
    challenge = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"
    scraper, _ = _opodo([(403, challenge), (403, challenge)])
    with pytest.raises(CaptchaError):
        scraper.search(NONSTOP)
    scraper, _ = _opodo([(429, "")])
    with pytest.raises(BotBlockedError):
        scraper.search(NONSTOP)
    scraper, _ = _opodo([])
    with pytest.raises(SearchTimeoutError):
        scraper.search(NONSTOP)
    scraper, _ = _opodo([_fares({"itineraries": []})])
    assert scraper.search(NONSTOP) == []  # answered, with no flights


# --- Apify (Google Flights) ------------------------------------------------------------------

def _ap_row(dep, arr, price, stops=0, returns=(), frm="MIA", to="MCO", day="2026-11-27", airline="Delta"):
    """A flight row as the actor documents it, with legs and return options."""
    legs = [{"flightNumber": "DL 100", "airline": airline, "from": frm, "to": to,
             "departAt": f"{day}T{dep}", "arriveAt": f"{day}T{arr}", "durationMinutes": 60, "aircraft": "A321"}]
    if stops:
        legs = [dict(legs[0], to="ATL", arriveAt=f"{day}T07:00"),
                dict(legs[0], **{"from": "ATL", "departAt": f"{day}T08:00", "flightNumber": "DL 200"})]
    return {"type": "flight", "price": price, "currency": "USD", "priceLevel": "typical", "airline": airline,
            "airlineCodes": ["DL"], "stops": stops, "departAt": f"{day}T{dep}", "arriveAt": f"{day}T{arr}",
            "totalDurationMinutes": 60 if not stops else 180, "legs": legs,
            "layovers": [{"airport": "ATL", "durationMinutes": 60}] if stops else [],
            "returnOptions": list(returns)}


def _ap_return(dep, arr, total, frm="MCO", to="MIA"):
    row = _ap_row(dep, arr, None, frm=frm, to=to, day="2026-11-29")
    del row["returnOptions"]
    return dict(row, totalPrice=total)


def _apify(monkeypatch, *answers, token="tok"):
    from flight_fare_scraper.scrapers.apify_google import ApifyGoogleScraper
    monkeypatch.setenv("FFS_APIFY_TOKEN", token)
    scraper = ApifyGoogleScraper().__enter__()
    scraper.sent, replies = [], list(answers)

    def respond(method, url, params=None, body=None, headers=None):
        scraper.sent.append({"method": method, "url": url, "params": params, "body": body, "headers": headers})
        return replies.pop(0)
    monkeypatch.setattr(scraper, "request", respond)
    return scraper


def test_apify_stores_each_outbound_with_its_returns_at_the_round_trip_total(monkeypatch):
    answer = [{"type": "notice", "message": "run totals"},
              _ap_row("08:00", "09:00", 150, returns=[_ap_return("18:00", "19:00", 150), _ap_return("12:00", "13:00", 171)]),
              _ap_row("08:30", "09:30", 160, frm="FLL", returns=[_ap_return("18:00", "19:00", 160)])]
    scraper = _apify(monkeypatch, answer)
    rows = scraper.search(NONSTOP)
    # The Fort Lauderdale departure is another airport, not the query's.
    assert sorted((row.return_depart[11:16], row.price) for row in rows) == [("12:00", 171.0), ("18:00", 150.0)]
    row = rows[0]
    assert (row.site, row.currency, row.price_prediction) == ("apify_google", "USD", "typical")
    assert row.outbound_airline == "Delta" and row.outbound_from == "MIA" and row.return_to == "MIA"
    assert row.outbound_depart == "2026-11-27T08:00:00" and row.outbound_equipment == "A321"
    sent = scraper.sent[0]
    assert sent["method"] == "POST" and "lergassy~google-flights-api" in sent["url"] and sent["params"] is None
    assert sent["headers"] == {"Authorization": "Bearer tok"}  # never in the URL
    body = sent["body"]
    assert (body["origin"], body["destination"], body["departureDate"], body["returnDate"]) == (
        "MIA", "MCO", "2026-11-27", "2026-11-29")
    assert body["maxStops"] == "0" and body["resolveReturnFlights"] is True and body["kayakWhenGoogleEmpty"] is False
    assert body["maxResults"] == body["maxReturnResolutions"] == 5


def test_apify_asks_again_for_nonstops_when_the_cheapest_all_connect(monkeypatch):
    query = build_query("MIA", "MCO", "2026-11-27", "2026-11-29", nonstop="false")
    connecting = [_ap_row("06:00", "09:00", 120, stops=1, returns=[_ap_return("18:00", "19:00", 120)])]
    nonstop = [_ap_row("08:00", "09:00", 150, returns=[_ap_return("18:00", "19:00", 150)])]
    scraper = _apify(monkeypatch, connecting, nonstop)
    rows = scraper.search(query)
    assert [s["body"]["maxStops"] for s in scraper.sent] == ["any", "0"]
    assert sorted((row.price, row.outbound_stops, row.outbound_layover_airports) for row in rows) == [
        (120.0, 1, "ATL"), (150.0, 0, None)]


def test_apify_without_a_token_or_credit_is_unavailable(monkeypatch):
    from flight_fare_scraper.scrapers.apify_google import ApifyGoogleScraper
    monkeypatch.delenv("FFS_APIFY_TOKEN", raising=False)
    with ApifyGoogleScraper() as scraper:
        with pytest.raises(SourceUnavailableError, match="FFS_APIFY_TOKEN"):
            scraper.search(NONSTOP)
    assert ApifyGoogleScraper().http_error(402, "").__class__ is SourceUnavailableError
    assert not ApifyGoogleScraper.polite


def test_apify_summary_fields_fill_in_when_a_row_has_no_legs(monkeypatch):
    row = _ap_row("08:00", "09:00", 150, returns=[_ap_return("18:00", "19:00", 150)])
    row.pop("legs")
    row["returnOptions"][0].pop("legs")
    rows = _apify(monkeypatch, [row]).search(NONSTOP)
    assert [(r.outbound_airline, r.outbound_depart, r.outbound_stops, r.return_arrive) for r in rows] == [
        ("Delta", "2026-11-27T08:00:00", 0, "2026-11-29T19:00:00")]


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
