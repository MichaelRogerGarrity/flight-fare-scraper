"""A run of Opodo searches in a plain headless browser: does it keep answering?

Placeholder routes unrelated to anything this repo tracks; prints one line per search
(challenge or not, itineraries, cheapest regular price) and a total. The fares arrive as
the answer to Opodo's own `searchItinerary` GraphQL call, which the page makes.

    python scripts/opodo_check.py SEARCHES PAUSE_SECONDS
"""

import json
import sys
import time
from datetime import date, timedelta

from playwright.sync_api import sync_playwright

from probe_sites import CHALLENGE

ROUTES = [("JFK", "LAX"), ("BOS", "DEN"), ("ATL", "LAS"), ("SFO", "JFK"), ("SEA", "HNL"), ("MIA", "PHX")]
FARES_PATH = "itinerary-selection-bff/service/graphql"
REGULAR = "MEMBER_PRICE_POLICY_UNDISCOUNTED"  # the price without Opodo's paid membership
WAIT_S = 45


def search(context, origin, destination, depart, back):
    page = context.new_page()
    answers = []

    def on_response(response):
        if FARES_PATH in response.url:
            try:
                answers.append(json.loads(response.text()))
            except Exception:
                pass
    page.on("response", on_response)
    url = f"https://www.opodo.com/travel/#results/type=R;from={origin};to={destination};dep={depart};ret={back}"
    started = time.monotonic()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(WAIT_S * 1000)
        text, title = page.inner_text("body"), page.title()
    except Exception as error:
        page.close()
        return "FAILED", f"{type(error).__name__}", time.monotonic() - started
    page.close()
    challenge = CHALLENGE.search(text) or CHALLENGE.search(title)
    itineraries = []
    for answer in answers:
        found = (((answer or {}).get("data") or {}).get("searchItinerary") or {}).get("itineraries") or []
        if len(found) > len(itineraries):
            itineraries = found
    prices = [(fee["price"]["amount"], fee["price"]["currency"]) for item in itineraries
              for fee in item.get("fees") or [] if fee.get("type") == REGULAR]
    seconds = time.monotonic() - started
    if challenge:
        return "CHALLENGED", challenge.group(0), seconds
    if not itineraries:
        return "EMPTY", "no fares answer", seconds
    detail = f"{len(itineraries):4d} itineraries"
    if prices:
        amount, currency = min(prices)
        detail += f"  cheapest {currency} {amount:,.0f}"
    return "ok", detail, seconds


def main():
    searches, pause = int(sys.argv[1]), float(sys.argv[2])
    counts = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
        for i in range(searches):
            origin, destination = ROUTES[i % len(ROUTES)]
            depart = date.today() + timedelta(days=30 + 7 * (i // len(ROUTES)))
            outcome, detail, seconds = search(context, origin, destination, depart, depart + timedelta(days=7))
            counts[outcome] = counts.get(outcome, 0) + 1
            print(f"{i + 1:3d} {origin}-{destination} {depart}  {outcome:10s} {detail}  {seconds:4.0f}s", flush=True)
            if i < searches - 1:
                time.sleep(pause)
        browser.close()
    print("\n" + ", ".join(f"{n} {outcome}" for outcome, n in counts.items()) + f" of {searches}")


if __name__ == "__main__":
    main()
