"""Opodo: fares from the answer its own search page gets, read in a browser, no key.

The page asks one GraphQL call (`searchItinerary`) for the fares and draws them; this
loads the page in a plain headless browser and keeps that call's answer. It answered
GitHub's runners 20 times in a row on 2026-11-05 (docs/backup-fare-sources.md). Its
terms forbid automated access without permission, as Kayak's do, so the run's load
limits apply to it (`polite`).

What it gives:
- Real round-trip tickets, priced as one -- no one-way pairing, no hidden-city fares.
- Two prices per itinerary: the regular one, stored here, and a lower one for Opodo's
  paid Prime membership, which isn't.
- Prices in euros on opodo.com, whatever the route. They're stored in dollars at the
  European Central Bank's reference rate of the day, fetched once per run; if that
  fails, they're stored in euros with `currency` saying so.
- Whole city areas: a search from JFK also lists Newark and LaGuardia flights. Only
  legs that leave from and land at the query's own airports (a metro code's airports,
  or the code itself) are kept, as on Kayak.
- A cabin-bag rule per leg, stored as INCLUDED when both legs include one and UNKNOWN
  otherwise ("depends on the airline"). Nothing on checked bags.

A Cloudflare challenge in place of the fares is the site refusing this connection, and
stops the run like a Kayak CAPTCHA: on 2026-11-06 it began answering the home connection
that way while still answering GitHub's runners.
"""

import json
import logging
import re
import time
import urllib.request
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from ..models import SearchQuery
from .base import BaseScraper, BotBlockedError, CaptchaError, SearchTimeoutError
from .google_flights import Itinerary, ItinerarySource, Leg, Segment, airports_for

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.opodo.com/travel/#results/type=R;from={origin};to={destination};dep={depart};ret={back}"
FARES_PATH = "itinerary-selection-bff/service/graphql"
REGULAR = "MEMBER_PRICE_POLICY_UNDISCOUNTED"  # the price without the paid membership
ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FARES_TIMEOUT_S = 75.0  # the fares came 20-40 s after the page started loading
SETTLE_S = 5.0  # after the last fares answer, how long to wait for a later one
CHALLENGES_TO_GIVE_UP = 3
CHALLENGE_RE = re.compile(r"just a moment|captcha|verify you are human", re.I)
CABINS = {"TOURIST": "Economy", "PREMIUM_ECONOMY": "Premium Economy", "BUSINESS": "Business", "FIRST": "First"}


def ecb_usd_per_eur() -> float:
    """Dollars per euro: the European Central Bank's reference rate, published each working day."""
    with urllib.request.urlopen(ECB_RATES_URL, timeout=30) as response:
        text = response.read().decode("utf-8", "replace")
    found = re.search(r"currency=['\"]USD['\"]\s+rate=['\"]([\d.]+)['\"]", text)
    if not found:
        raise ValueError("no USD rate in the ECB's daily rates")
    return float(found.group(1))


def _moment(text: Optional[str]) -> Optional[datetime]:
    """A local time; Opodo gives the airport's offset, the other sources' rows don't."""
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _by_id(found: dict, name: str, inner: str) -> Dict[str, dict]:
    return {entry["id"]: entry[inner] for entry in found.get(name) or [] if entry.get("id") and entry.get(inner)}


def regular_price(item: dict) -> Optional[Tuple[float, str]]:
    for fee in item.get("fees") or []:
        price = fee.get("price") or {}
        if fee.get("type") == REGULAR and isinstance(price.get("amount"), (int, float)):
            return float(price["amount"]), price.get("currency") or "EUR"
    return None


class Answer:
    """One `searchItinerary` answer, with its lookup tables indexed."""

    def __init__(self, found: dict):
        self.itineraries = found.get("itineraries") or []
        self.segments = _by_id(found, "segments", "segment")
        self.sections = _by_id(found, "sections", "section")
        self.airports = {key: place.get("iata") for key, place in _by_id(found, "locations", "location").items()}
        self.carriers = {key: carrier.get("name") or key for key, carrier in _by_id(found, "carriers", "carrier").items()}

    def leg(self, segment_id: Optional[str]) -> Tuple[Optional[Leg], Optional[str]]:
        """A leg and its cabin-bag rule, or (None, None) if the answer doesn't describe it."""
        segment = self.segments.get(segment_id)
        if not segment:
            return None, None
        flights = []
        for section_id in segment.get("sections") or []:
            section = self.sections.get(section_id)
            if not section:
                return None, None
            marketing, operating = section.get("carrierId"), section.get("operatingCarrierId")
            flights.append(Segment(
                airline=self.carriers.get(marketing, marketing),
                flight_number=section.get("flightCode"),
                from_airport=self.airports.get(section.get("departureId")),
                to_airport=self.airports.get(section.get("destinationId")),
                depart=_moment(section.get("departureDate")), arrive=_moment(section.get("arrivalDate")),
                duration_min=section.get("duration") if isinstance(section.get("duration"), int) else None,
                aircraft=section.get("vehicleModel"),
                cabin=CABINS.get(section.get("cabinClass"), section.get("cabinClass")),
                operated_by=self.carriers.get(operating, operating) if operating and operating != marketing else None,
            ))
        if not flights:
            return None, None
        duration = segment.get("duration")
        return Leg(segments=flights, duration_min=duration if isinstance(duration, int) else None), \
            segment.get("baggageCondition")


def parse_answer(query: SearchQuery, found: dict, usd_per_eur: Optional[float]) -> List[Itinerary]:
    """The answer's round trips between the query's own airports, one per pair of legs."""
    answer = Answer(found)
    origins, destinations = set(airports_for(query.origin)), set(airports_for(query.destination))
    kept: Dict[Tuple[str, str], Itinerary] = {}
    for item in answer.itineraries:
        price = regular_price(item)
        legs = item.get("legs") or []
        if price is None or len(legs) != 2:
            continue
        keys = (legs[0].get("segmentId"), legs[1].get("segmentId"))
        (outbound, out_bags), (inbound, in_bags) = answer.leg(keys[0]), answer.leg(keys[1])
        if outbound is None or inbound is None:
            continue
        if (outbound.segments[0].from_airport not in origins or outbound.segments[-1].to_airport not in destinations
                or inbound.segments[0].from_airport not in destinations or inbound.segments[-1].to_airport not in origins):
            continue  # another airport in the same city
        amount, currency = price
        if currency == "EUR" and usd_per_eur:
            amount, currency = round(amount * usd_per_eur, 2), "USD"
        found_already = kept.get(keys)
        if found_already is not None and found_already.price <= amount:
            continue
        cabin_bags = "INCLUDED" if out_bags == in_bags == "CABIN_INCLUDED" else "UNKNOWN"
        kept[keys] = Itinerary(outbound, inbound, amount, currency, extra={
            "booking_provider": "Opodo", "carry_on_status": cabin_bags,
            "carry_on_fee": 0.0 if cabin_bags == "INCLUDED" else None,
        })
    return list(kept.values())


class OpodoScraper(ItinerarySource):
    name = "opodo"
    polite = True  # read directly, like Kayak

    def __init__(self, headless: bool = True, max_pages: Optional[int] = None,
                 fares_timeout_s: float = FARES_TIMEOUT_S, settle_s: float = SETTLE_S,
                 usd_rate: Callable[[], float] = ecb_usd_per_eur, **options):
        super().__init__(headless, max_pages, **options)
        self.headless = headless
        self.fares_timeout_s = fares_timeout_s
        self.settle_s = settle_s
        self._usd_rate = usd_rate
        self._usd_per_eur: Optional[float] = None
        self._rate_failed = False
        self._playwright = self._browser = self._context = None

    def __enter__(self) -> "OpodoScraper":
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            # One context for the run, as in the 20-search check: the site sees one visitor.
            self._context = self._browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = self._playwright = self._context = None
        try:
            if browser is not None:
                browser.close()
        except PlaywrightError:
            logger.debug("browser was already gone at shutdown", exc_info=True)
        finally:
            if playwright is not None:
                playwright.stop()

    def reset(self) -> None:
        BaseScraper.reset(self)  # a fresh browser, unlike the HTTP sources

    def itineraries(self, query: SearchQuery) -> List[Itinerary]:
        found = self.fares_answer(query)
        return parse_answer(query, found, self.usd_per_eur())

    def usd_per_eur(self) -> Optional[float]:
        """The day's rate, fetched once; None (prices kept in euros) if it can't be had."""
        if self._usd_per_eur is None and not self._rate_failed:
            try:
                self._usd_per_eur = self._usd_rate()
            except Exception as error:
                self._rate_failed = True
                logger.warning("opodo: no EUR-USD rate (%s); storing prices in EUR", type(error).__name__)
        return self._usd_per_eur

    def fares_answer(self, query: SearchQuery) -> dict:
        """Load the search page and return the fullest fares answer it got."""
        if self._context is None:
            raise RuntimeError("OpodoScraper must be used as a context manager (`with OpodoScraper() as s:`)")
        answers: List[dict] = []
        refusals: List[Tuple[int, bool]] = []  # (status, whether it was a challenge page)
        last_answer = [0.0]

        def on_response(response):
            if FARES_PATH not in response.url or response.request.method != "POST":
                return
            try:
                body = response.text()
            except Exception:
                return
            if response.status >= 400:
                refusals.append((response.status, bool(CHALLENGE_RE.search(body[:5000]))))
                return
            try:
                found = (json.loads(body).get("data") or {}).get("searchItinerary")
            except (ValueError, AttributeError):
                return
            if isinstance(found, dict):
                answers.append(found)
                last_answer[0] = time.monotonic()

        url = SEARCH_URL.format(origin=query.origin, destination=query.destination,
                                depart=query.depart_date.isoformat(), back=query.return_date.isoformat())
        page = self._context.new_page()
        page.on("response", on_response)
        self.requests_made += 1
        try:
            started = time.monotonic()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            while time.monotonic() - started < self.fares_timeout_s:
                page.wait_for_timeout(1000)
                if any(a.get("itineraries") for a in answers) and time.monotonic() - last_answer[0] >= self.settle_s:
                    break
                if not answers and sum(challenge for _, challenge in refusals) >= CHALLENGES_TO_GIVE_UP:
                    break  # the page keeps retrying, and every retry is challenged too
            title = page.title()
        except PlaywrightError as error:
            raise SearchTimeoutError(f"opodo: page failed to load ({type(error).__name__})") from None
        finally:
            page.close()
        if answers:
            return max(answers, key=lambda a: len(a.get("itineraries") or []))
        if any(challenge for _, challenge in refusals) or CHALLENGE_RE.search(title):
            raise CaptchaError("opodo: answered with a Cloudflare challenge instead of fares")
        if refusals:
            raise BotBlockedError(f"opodo: fares refused (HTTP {refusals[-1][0]})")
        raise SearchTimeoutError(f"opodo: no fares within {self.fares_timeout_s:.0f}s")
