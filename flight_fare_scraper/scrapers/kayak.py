import logging
import math
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from .. import redact
from ..models import FlightResult, SearchQuery
from ..windows import kayak_landing, kayak_takeoff
from .base import BaseScraper, BotBlockedError, PaginationError, ScraperError, SearchTimeoutError

logger = logging.getLogger(__name__)

POLL_URL_RE = re.compile(r"/i/api/search/dynamic/flights/poll")
BOT_PAGE_MARKER = "/help/bots"
BASE_URL = "https://www.kayak.com"
SHOW_MORE_TEXT = "Show more results"
MAX_PAGES_PER_PASS = 20
DEFAULT_PAGE_SIZE = 50
POLL_INTERVAL_MS = 400
# Random pauses so requests don't arrive at a fixed machine rhythm.
PAGE_JITTER_S = (1.5, 4.0)  # before each "Show more results" click
PASS_JITTER_S = (5.0, 15.0)  # between a search's main and nonstop passes

# Identifies one bookable offer across separate page loads, where booking ids differ:
# (outbound leg id, return leg id, provider code, price).
OfferKey = Tuple[Optional[str], Optional[str], str, Optional[float]]
Offer = Tuple[OfferKey, FlightResult]


@dataclass
class _Pass:
    offers: List[Offer]
    filtered_count: int
    total_pages: int
    price_prediction: Optional[str]
    price_prediction_change: Optional[float]
    price_prediction_days: Optional[int]


def pages_to_fetch(filtered_count: int, page_size: int, max_pages: int = MAX_PAGES_PER_PASS) -> Tuple[int, int]:
    """Return (total pages, pages to fetch): every page, capped at `max_pages`."""
    total = math.ceil(filtered_count / page_size) if filtered_count > 0 else 0
    return total, min(total, max_pages)


def merge_offers(offer_lists: Sequence[Sequence[Offer]]) -> List[FlightResult]:
    """Union offers from several searches, keeping each offer's highest count from any one of them.

    Within one search, offers with the same legs, provider, and price (different fare
    bundles) are all kept; an offer that two searches both returned isn't double counted.
    """
    merged: List[FlightResult] = []
    kept: Counter = Counter()
    for offers in offer_lists:
        seen: Counter = Counter()
        for key, result in offers:
            seen[key] += 1
            if seen[key] > kept[key]:
                kept[key] += 1
                merged.append(result)
    return merged


def within_constraints(query: SearchQuery, result: FlightResult) -> bool:
    if query.nonstop_only and (result.outbound_stops or result.return_stops):
        return False
    checks = (
        (query.outbound_takeoff, query.depart_date, result.outbound_depart),
        (query.return_takeoff, query.return_date, result.return_depart),
        (query.outbound_landing, query.depart_date, result.outbound_arrive),
        (query.return_landing, query.return_date, result.return_arrive),
    )
    for window, leg_date, moment in checks:
        if window is None:
            continue
        if moment is None or not window.contains(leg_date, datetime.fromisoformat(moment)):
            return False
    return True


def _page_number(poll: dict) -> Optional[int]:
    """Which results page a poll response belongs to, or None if it isn't a results page.

    A search whose filters match nothing (e.g. stops=0 on a route with no nonstops)
    answers with polls that have no pageNumber and filteredCount 0. Treating those as
    an empty page 1 keeps a genuine "no results" from looking like a timeout.
    """
    number = poll.get("pageNumber")
    if isinstance(number, int):
        return number
    if poll.get("filteredCount") == 0 and not poll.get("results"):
        return 1
    return None


def _flag(flags: Optional[dict], name: str) -> Optional[bool]:
    # Kayak lists the flags that apply, sometimes with an explicit false; a key missing
    # from a present flags object is treated as false. No flags object at all is unknown.
    if flags is None:
        return None
    return bool(flags.get(name, False))


def _leg_pair(outbound: Optional[str], inbound: Optional[str]) -> str:
    # Kayak separates the outbound-leg and return-leg values of a filter with "__".
    return f"{outbound or ''}__{inbound or ''}"


def _bag_fee(bag: dict) -> Optional[float]:
    status = bag.get("status")
    if status == "INCLUDED":
        return 0.0
    if status == "FEE":
        return (bag.get("displayPrice") or {}).get("price")
    return None


def _cabins(leg_farings: Sequence[dict]) -> Optional[str]:
    cabins = {
        segment.get("cabinDisplay")
        for leg in leg_farings
        for segment in leg.get("segmentFarings") or []
        if segment.get("cabinDisplay")
    }
    return ",".join(sorted(cabins)) or None


class KayakScraper(BaseScraper):
    name = "kayak"

    def __init__(self, headless: bool = True, timeout_s: float = 45.0, max_pages: int = MAX_PAGES_PER_PASS,
                 page_jitter_s: Tuple[float, float] = PAGE_JITTER_S,
                 pass_jitter_s: Tuple[float, float] = PASS_JITTER_S):
        self.headless = headless
        self.timeout_s = timeout_s
        self.max_pages = max_pages
        self.page_jitter_s = page_jitter_s
        self.pass_jitter_s = pass_jitter_s
        self._stealth = Stealth()
        self._pw_cm = None
        self._browser = None

    def __enter__(self) -> "KayakScraper":
        self._pw_cm = self._stealth.use_sync(sync_playwright())
        playwright = self._pw_cm.__enter__()
        try:
            self._browser = playwright.chromium.launch(
                channel="chrome",
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except BaseException:
            self._pw_cm.__exit__(None, None, None)
            self._pw_cm = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        browser, pw_cm = self._browser, self._pw_cm
        self._browser = self._pw_cm = None
        try:
            if browser is not None:
                browser.close()
        except PlaywrightError:
            logger.debug("browser was already gone at shutdown", exc_info=True)
        finally:
            if pw_cm is not None:
                pw_cm.__exit__(exc_type, exc, tb)

    def build_url(self, query: SearchQuery, nonstop: bool = False) -> str:
        path = f"/flights/{query.origin}-{query.destination}/{query.depart_date.isoformat()}/{query.return_date.isoformat()}"
        filters = []
        if query.nonstop_only or nonstop:
            filters.append("stops=0")
        if query.outbound_takeoff or query.return_takeoff:
            filters.append("takeoff=" + _leg_pair(
                query.outbound_takeoff and kayak_takeoff(query.outbound_takeoff),
                query.return_takeoff and kayak_takeoff(query.return_takeoff),
            ))
        if query.outbound_landing or query.return_landing:
            # Kayak rewrites a single-day landing range to plain HHMM,HHMM, so results are
            # also checked against the windows after parsing (see within_constraints).
            filters.append("landing=" + _leg_pair(
                query.outbound_landing and kayak_landing(query.outbound_landing, query.depart_date),
                query.return_landing and kayak_landing(query.return_landing, query.return_date),
            ))
        params = "sort=price_a"
        if filters:
            params += "&fs=" + ";".join(filters)
        return f"{BASE_URL}{path}?{params}"

    def search(self, query: SearchQuery) -> List[FlightResult]:
        if self._browser is None:
            raise RuntimeError("KayakScraper must be used as a context manager (`with KayakScraper() as s:`)")
        if not self._browser.is_connected():
            raise ScraperError("browser process is no longer connected")

        main = self._run_pass(query, nonstop=False)
        passes = [main]
        if not query.nonstop_only and main.total_pages > self.max_pages:
            # Sorted by price, direct flights can sit beyond the page cap; a separate
            # nonstop-only search (with the same cap) finds them.
            logger.info(
                "%s: %d pages exceeds %d, so fetched the first %d plus a nonstop-only search",
                redact.label(query), main.total_pages, self.max_pages, self.max_pages,
            )
            time.sleep(random.uniform(*self.pass_jitter_s))
            passes.append(self._run_pass(query, nonstop=True))

        results = [
            replace(result, price_prediction=main.price_prediction,
                    price_prediction_change=main.price_prediction_change,
                    price_prediction_days=main.price_prediction_days)
            for result in merge_offers([p.offers for p in passes])
        ]
        kept = [result for result in results if within_constraints(query, result)]
        if len(kept) < len(results):
            logger.warning(
                "%s: dropped %d of %d rows outside the requested stops/time filters (Kayak returned them anyway)",
                redact.label(query), len(results) - len(kept), len(results),
            )
        return kept

    def _run_pass(self, query: SearchQuery, nonstop: bool) -> _Pass:
        label = redact.label(query) + (" [nonstop pass]" if nonstop else "")
        url = self.build_url(query, nonstop=nonstop)
        context = self._browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        try:
            page = context.new_page()
            polls: Dict[int, List[dict]] = {}

            def on_response(response):
                if response.request.method != "POST" or not POLL_URL_RE.search(response.url):
                    return
                try:
                    data = response.json()
                except Exception:
                    logger.debug("%s: unreadable poll response", label, exc_info=True)
                    return
                page_number = _page_number(data)
                if page_number is None:
                    logger.debug("%s: ignoring poll response that isn't a results page (keys %s)", label, sorted(data)[:12])
                    return
                polls.setdefault(page_number, []).append(data)

            page.on("response", on_response)
            logger.debug("%s: loading %s", label, url)
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_s * 1000)

            first = self._wait_for_page(page, polls, 1, label)
            filtered_count = first.get("filteredCount") or 0
            total_pages, fetch = pages_to_fetch(filtered_count, first.get("pageSize") or DEFAULT_PAGE_SIZE,
                                                self.max_pages)
            payloads = [first]
            for number in range(2, fetch + 1):
                page.wait_for_timeout(random.uniform(*self.page_jitter_s) * 1000)
                self._click_show_more(page, number, label)
                payloads.append(self._wait_for_page(page, polls, number, label))
                logger.debug("%s: loaded page %d of %d", label, number, fetch)
            logger.info("%s: %d results across %d page(s), fetched %d", label, filtered_count, total_pages, fetch)

            offers: List[Offer] = []
            booking_ids = set()
            for payload in payloads:
                for key, booking_id, result in self._parse(query, payload):
                    if booking_id is not None:
                        if booking_id in booking_ids:
                            continue
                        booking_ids.add(booking_id)
                    offers.append((key, result))

            return _Pass(offers, filtered_count, total_pages, *self._price_prediction(first))
        finally:
            try:
                context.close()
            except PlaywrightError:
                logger.debug("%s: browser context was already closed", label, exc_info=True)

    def _wait_for_page(self, page, polls: Dict[int, List[dict]], number: int, label: str) -> dict:
        deadline = time.monotonic() + self.timeout_s
        while True:
            self._raise_if_blocked(page, label)
            complete = [poll for poll in polls.get(number, []) if poll.get("status") == "complete"]
            if complete:
                return complete[-1]
            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(POLL_INTERVAL_MS)

        seen = polls.get(number, [])
        if seen:
            raise SearchTimeoutError(
                f"{label}: page {number} still '{seen[-1].get('status')}' after {self.timeout_s:.0f}s"
            )
        hint = " (possibly a bot block that didn't redirect)" if number == 1 else ""
        raise SearchTimeoutError(f"{label}: no results response for page {number} within {self.timeout_s:.0f}s{hint}")

    @staticmethod
    def _raise_if_blocked(page, label: str) -> None:
        if BOT_PAGE_MARKER in page.url:
            raise BotBlockedError(f"{label}: redirected to {page.url}")

    def _click_show_more(self, page, number: int, label: str) -> None:
        try:
            page.get_by_text(SHOW_MORE_TEXT, exact=False).first.click(timeout=self.timeout_s * 1000)
        except PlaywrightTimeoutError as error:
            self._raise_if_blocked(page, label)
            raise PaginationError(f"{label}: couldn't click '{SHOW_MORE_TEXT}' to load page {number}") from error

    def _leg_detail(self, leg: dict, segments_lookup: dict, airlines_lookup: dict):
        """Airline, codeshare operator, equipment, layover, and airport-change detail for one leg.

        `booking.providerCode` is who you'd book through, not the airline; the airline
        lives on each segment in the top-level `segments` lookup. `layover` sits on the
        leg's own segment references, not on those resolved segment records.
        """
        segment_refs = leg.get("segments") or []
        if not segment_refs:
            return None, None, None, None, None, None

        segments = [segments_lookup.get(ref.get("id")) or {} for ref in segment_refs]
        first = segments[0]
        code = first.get("airline")
        airline = (airlines_lookup.get(code) or {}).get("name", code)
        operated_by = first.get("operationalDisplay")
        if operated_by == airline:
            operated_by = None

        layover_airports = ",".join(s["destination"] for s in segments[:-1] if s.get("destination")) or None
        layover_min = sum(
            ref["layover"].get("duration", 0) for ref in segment_refs[:-1] if ref.get("layover")
        ) or None
        airport_change = any(
            arriving.get("destination") and departing.get("origin")
            and arriving["destination"] != departing["origin"]
            for arriving, departing in zip(segments, segments[1:])
        )
        return airline, operated_by, first.get("equipmentTypeName"), layover_airports, layover_min, airport_change

    @staticmethod
    def _price_prediction(data: dict) -> Tuple[Optional[str], Optional[float], Optional[int]]:
        prediction = (data.get("pricePredictionData") or {}).get("pricePrediction") or {}
        if not prediction:
            return None, None, None
        return (
            prediction.get("prediction"),
            (prediction.get("priceChange") or {}).get("price"),
            prediction.get("daysHorizon"),
        )

    def _parse(self, query: SearchQuery, data: dict) -> Iterator[Tuple[OfferKey, Optional[str], FlightResult]]:
        legs_lookup = data.get("legs") or {}
        airlines_lookup = data.get("airlines") or {}
        segments_lookup = data.get("segments") or {}
        providers_lookup = data.get("providers") or {}
        skipped = 0

        for item in data.get("results") or []:
            if item.get("type") != "core":
                continue
            for booking in item.get("bookingOptions") or []:
                leg_farings = booking.get("legFarings") or []
                if len(leg_farings) != 2:
                    skipped += 1
                    continue
                outbound_ref, return_ref = leg_farings
                outbound_leg = legs_lookup.get(outbound_ref.get("legId")) or {}
                return_leg = legs_lookup.get(return_ref.get("legId")) or {}

                (outbound_airline, outbound_operated_by, outbound_equipment, outbound_layover_airports,
                 outbound_layover_min, outbound_airport_change) = self._leg_detail(
                    outbound_leg, segments_lookup, airlines_lookup)
                (return_airline, return_operated_by, return_equipment, return_layover_airports,
                 return_layover_min, return_airport_change) = self._leg_detail(
                    return_leg, segments_lookup, airlines_lookup)

                price_info = booking.get("displayPrice") or {}
                provider_code = booking.get("providerCode") or ""
                fees = booking.get("fees") or {}
                carry_on = fees.get("carryOnBagData") or {}
                checked = fees.get("checkedBagData") or {}
                flags = booking.get("flags")
                booking_url = (booking.get("bookingUrl") or {}).get("url")

                result = FlightResult(
                    site=self.name,
                    origin=query.origin,
                    destination=query.destination,
                    depart_date=query.depart_date.isoformat(),
                    return_date=query.return_date.isoformat(),
                    price=price_info.get("price"),
                    currency=price_info.get("currency", "USD"),
                    booking_provider=(providers_lookup.get(provider_code) or {}).get("displayName", provider_code),
                    outbound_airline=outbound_airline,
                    outbound_operated_by=outbound_operated_by,
                    outbound_depart=outbound_leg.get("departure"),
                    outbound_arrive=outbound_leg.get("arrival"),
                    outbound_stops=max(len(outbound_leg.get("segments") or []) - 1, 0) if outbound_leg else None,
                    outbound_duration_min=outbound_leg.get("duration"),
                    outbound_layover_airports=outbound_layover_airports,
                    outbound_layover_min=outbound_layover_min,
                    outbound_airport_change=outbound_airport_change,
                    outbound_equipment=outbound_equipment,
                    return_airline=return_airline,
                    return_operated_by=return_operated_by,
                    return_depart=return_leg.get("departure"),
                    return_arrive=return_leg.get("arrival"),
                    return_stops=max(len(return_leg.get("segments") or []) - 1, 0) if return_leg else None,
                    return_duration_min=return_leg.get("duration"),
                    return_layover_airports=return_layover_airports,
                    return_layover_min=return_layover_min,
                    return_airport_change=return_airport_change,
                    return_equipment=return_equipment,
                    cabin_class=_cabins(leg_farings),
                    carry_on_status=carry_on.get("status"),
                    carry_on_fee=_bag_fee(carry_on),
                    checked_bag_status=checked.get("status"),
                    checked_bag_fee=_bag_fee(checked),
                    second_checked_bag_fee=_bag_fee(checked.get("secondBag") or {}),
                    free_cancellation=_flag(flags, "isFreeCancellation"),
                    virtual_interline=_flag(flags, "hasVirtualInterline"),
                    self_transfer_protection=_flag(flags, "isSelfTransferProtection"),
                    booking_url=urljoin(BASE_URL, booking_url) if booking_url else None,
                )
                key = (outbound_ref.get("legId"), return_ref.get("legId"), provider_code, result.price)
                yield key, booking.get("bookingId"), result

        if skipped:
            logger.warning("skipped %d booking option(s) that weren't a two-leg round trip", skipped)
