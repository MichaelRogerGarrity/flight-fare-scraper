"""Does a site answer a plain headless browser with fares, or with a challenge?

Loads one search page per site on a fixed placeholder route (NYC-LAX, 10-17 Dec 2026),
unrelated to anything this repo tracks, and prints counts only: the page's status, any
challenge wording, and the JSON calls that carry prices. No stealth patches -- this asks
whether a site answers an ordinary automated browser, not whether one can be disguised.

    python scripts/probe_sites.py opodo aviasales
"""

import re
import sys
import time

from playwright.sync_api import sync_playwright

SITES = {
    "opodo": "https://www.opodo.com/travel/#results/type=R;from=JFK;to=LAX;dep=2026-12-10;ret=2026-12-17",
    "edreams": "https://www.edreams.com/travel/#results/type=R;from=JFK;to=LAX;dep=2026-12-10;ret=2026-12-17",
    "aviasales": "https://www.aviasales.com/search/NYC1012LAX17121",
    "trip.com": "https://us.trip.com/flights/showfarefirst?dcity=nyc&acity=lax&ddate=2026-12-10&rdate=2026-12-17"
                "&triptype=rt&class=y&quantity=1&nonstoponly=off&locale=en-US&curr=USD",
    "wego": "https://www.wego.com/flights/searches/cNYC-cLAX-2026-12-10:cLAX-cNYC-2026-12-17/economy/1a:0c:0i",
    "skyscanner": "https://www.skyscanner.com/transport/flights/nyc/lax/261210/261217/",
}
CHALLENGE = re.compile(r"captcha|are you a (human|robot)|verify you are|press (&|and) hold|access denied|"
                       r"unusual traffic|just a moment", re.I)
PRICE_FIELD = re.compile(r'"(price|amount|value|total)"\s*:\s*\{?\s*"?\d', re.I)
RESULT_COUNT = re.compile(r"\d[\d,]* of \d[\d,]* flights|\d[\d,]* (flights|results) found", re.I)
WAIT_S = 35


def probe(browser, name: str, url: str) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
    page = context.new_page()
    documents, calls = [], []

    def on_response(response):
        if response.request.resource_type == "document":
            documents.append(response.status)
        if "json" not in (response.headers.get("content-type") or ""):
            return
        try:
            body = response.text()
        except Exception:
            return
        calls.append((len(PRICE_FIELD.findall(body)), len(body) // 1024, response.url.split("?")[0][:90]))
    page.on("response", on_response)
    started = time.monotonic()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(WAIT_S * 1000)
        text = page.inner_text("body")
        title = page.title()
    except Exception as error:
        print(f"{name}: failed to load ({type(error).__name__})")
        context.close()
        return
    challenge = CHALLENGE.search(text) or CHALLENGE.search(title)
    count = RESULT_COUNT.search(text)
    priced = [call for call in calls if call[0]]
    print(f"{name}: pages HTTP {documents}  title {title[:40]!r}  "
          f"challenge: {challenge.group(0) if challenge else 'none'}  results: {count.group(0) if count else 'none shown'}  "
          f"{len(priced)} of {len(calls)} JSON responses carry prices  {time.monotonic() - started:.0f}s")
    for fields, kb, where in sorted(priced, reverse=True)[:3]:
        print(f"    {fields:5d} price fields  {kb:4d} KB  {where}")
    context.close()


if __name__ == "__main__":
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for name in sys.argv[1:] or SITES:
            probe(browser, name, SITES[name])
        browser.close()
