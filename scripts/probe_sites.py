"""Does a site answer a plain headless browser with fares, or with a challenge -- and
which airlines does it sell?

Loads one search page per site on a fixed placeholder route (BOS-DEN, 8-15 Dec 2026:
United and Southwest fly it nonstop, Delta and American with a connection), unrelated
to anything this repo tracks, and prints counts only: the page's status, any challenge
wording, the JSON calls that carry prices, and how often each major airline's name
appears in the page and its JSON. No stealth patches -- this asks whether a site answers
an ordinary automated browser, not whether one can be disguised.

    python scripts/probe_sites.py priceline agoda
"""

import re
import sys
import time

from playwright.sync_api import sync_playwright

SITES = {
    "opodo": "https://www.opodo.com/travel/#results/type=R;from=BOS;to=DEN;dep=2026-12-08;ret=2026-12-15",
    "edreams": "https://www.edreams.com/travel/#results/type=R;from=BOS;to=DEN;dep=2026-12-08;ret=2026-12-15",
    "aviasales": "https://www.aviasales.com/search/BOS0812DEN15121",
    "trip.com": "https://us.trip.com/flights/showfarefirst?dcity=bos&acity=den&ddate=2026-12-08&rdate=2026-12-15"
                "&triptype=rt&class=y&quantity=1&nonstoponly=off&locale=en-US&curr=USD",
    "wego": "https://www.wego.com/flights/searches/cBOS-cDEN-2026-12-08:cDEN-cBOS-2026-12-15/economy/1a:0c:0i",
    "skyscanner": "https://www.skyscanner.com/transport/flights/bos/den/261208/261215/",
    "priceline": "https://www.priceline.com/m/fly/search/BOS-DEN-20261208/DEN-BOS-20261215/"
                 "?cabin-class=ECO&num-adults=1",
    "expedia": "https://www.expedia.com/Flights-Search?trip=roundtrip&leg1=from:BOS,to:DEN,departure:12/8/2026TANYT"
               "&leg2=from:DEN,to:BOS,departure:12/15/2026TANYT&passengers=adults:1&mode=search",
    "orbitz": "https://www.orbitz.com/Flights-Search?trip=roundtrip&leg1=from:BOS,to:DEN,departure:12/8/2026TANYT"
              "&leg2=from:DEN,to:BOS,departure:12/15/2026TANYT&passengers=adults:1&mode=search",
    "agoda": "https://www.agoda.com/flights/results?departureFrom=BOS&departureFromType=1&arrivalTo=DEN"
             "&arrivalToType=1&departDate=2026-12-08&returnDate=2026-12-15&searchType=2&cabinType=Economy&adults=1",
    "almosafer": "https://www.almosafer.com/en/flights/BOS-DEN/2026-12-08/2026-12-15/Economy/1Adult",
    "makemytrip": "https://www.makemytrip.com/flight/search?itinerary=BOS-DEN-08/12/2026_DEN-BOS-15/12/2026"
                  "&tripType=R&paxType=A-1_C-0_I-0&intl=true&cabinClass=E",
    "cleartrip": "https://www.cleartrip.com/flights/results?adults=1&childs=0&infants=0&class=Economy"
                 "&depart_date=08/12/2026&return_date=15/12/2026&from=BOS&to=DEN&intl=y",
    "traveloka": "https://www.traveloka.com/en-en/flight/fullsearch?ap=BOS.DEN&dt=08-12-2026.15-12-2026"
                 "&ps=1.0.0&sc=ECONOMY",
    "despegar": "https://www.despegar.com.ar/shop/flights/results/roundtrip/BOS/DEN/2026-12-08/2026-12-15/1/0/0",
    "google": "https://www.google.com/travel/flights?q=Flights%20from%20BOS%20to%20DEN%20on%202026-12-08"
              "%20through%202026-12-15&curr=USD&hl=en",
    "jetcost": "https://www.jetcost.com/en/flights/search?adults=1&children=0&infants=0&cabin_class=economy"
               "&trips[0][from_iata]=BOS&trips[0][to_iata]=DEN&trips[0][date]=2026-12-08"
               "&trips[1][from_iata]=DEN&trips[1][to_iata]=BOS&trips[1][date]=2026-12-15",
    "united": "https://www.united.com/en/us/fsr/choose-flights?f=BOS&t=DEN&d=2026-12-08&r=2026-12-15&px=1&taxng=1"
              "&newHP=True&clm=7&st=bestmatches&tqp=R",
    "american": "https://www.aa.com/booking/search?locale=en_US&pax=1&adult=1&type=RoundTrip&searchType=Revenue&cabin="
                "&carriers=ALL&slices=%5B%7B%22orig%22:%22BOS%22,%22origNearby%22:false,%22dest%22:%22DEN%22,"
                "%22destNearby%22:false,%22date%22:%222026-12-08%22%7D,%7B%22orig%22:%22DEN%22,%22origNearby%22:false,"
                "%22dest%22:%22BOS%22,%22destNearby%22:false,%22date%22:%222026-12-15%22%7D%5D",
    "delta": "https://www.delta.com/flightsearch/book-a-flight?tripType=ROUND_TRIP&fromCity=BOS&toCity=DEN"
             "&departureDate=12/08/2026&returnDate=12/15/2026&paxCount=1",
    "southwest": "https://www.southwest.com/air/booking/select-depart.html?adultPassengersCount=1&departureDate=2026-12-08"
                 "&destinationAirportCode=DEN&originationAirportCode=BOS&returnDate=2026-12-15&tripType=roundtrip&fareType=USD",
    "jetblue": "https://www.jetblue.com/booking/flights?from=BOS&to=DEN&depart=2026-12-08&return=2026-12-15"
               "&isMultiCity=false&noOfRoute=1&adults=1&children=0&infants=0&sharedMarket=false&roundTripFaresFlag=false&usePoints=false",
    "alaska": "https://www.alaskaair.com/search/results?A=1&O=BOS&D=DEN&OD=2026-12-08&DD=2026-12-15&RT=true",
    "frontier": "https://booking.flyfrontier.com/Flight/InternalSelect?o1=BOS&d1=DEN&dd1=Dec%2008,%202026"
                "&dd2=Dec%2015,%202026&ADT=1&mon=true",
    "cheapoair": "https://www.cheapoair.com/air/listing?&d1=BOS&r1=DEN&dt1=12/08/2026&d2=DEN&r2=BOS&dt2=12/15/2026"
                 "&tripType=ROUNDTRIP&cl=ECONOMY&ad=1&se=0&ch=0&infl=0&infs=0",
    "hopper": "https://hopper.com/flights/BOS-DEN/2026-12-08/2026-12-15",
    "studentuniverse": "https://www.studentuniverse.com/flights/search?origin=BOS&destination=DEN&departDate=2026-12-08"
                       "&returnDate=2026-12-15&adults=1&tripType=roundtrip",
    "flightcentre": "https://www.flightcentre.com/flights/search?origin=BOS&destination=DEN&departureDate=2026-12-08"
                    "&returnDate=2026-12-15&adults=1&tripType=return",
    "lastminute": "https://www.lastminute.com/flights/search?from=BOS&to=DEN&departureDate=2026-12-08&returnDate=2026-12-15&adults=1",
    "cheapflights": "https://www.cheapflights.com/flight-search/BOS-DEN/2026-12-08/2026-12-15",
    "kiwi": "https://www.kiwi.com/en/search/results/boston-massachusetts-united-states/denver-colorado-united-states/2026-12-08/2026-12-15",
}
CHALLENGE = re.compile(r"captcha|are you a (human|robot)|verify you are|press (&|and) hold|access denied|"
                       r"unusual traffic|just a moment|pardon our interruption|bot or not|request unsuccessful|blocked", re.I)
PRICE_FIELD = re.compile(r'"(price|amount|value|total)"\s*:\s*\{?\s*"?\d', re.I)
AIRLINES = ("Delta", "United", "Southwest", "American", "JetBlue", "Frontier", "Spirit", "Alaska")
WAIT_S = 35


def probe(browser, name: str, url: str) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
    page = context.new_page()
    documents, calls, bodies = [], [], []

    def on_response(response):
        if response.request.resource_type == "document":
            documents.append(response.status)
        if "json" not in (response.headers.get("content-type") or ""):
            return
        try:
            body = response.text()
        except Exception:
            return
        bodies.append(body)
        calls.append((len(PRICE_FIELD.findall(body)), len(body) // 1024, response.url.split("?")[0][:90]))
    page.on("response", on_response)
    started = time.monotonic()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(WAIT_S * 1000)
        text = page.inner_text("body")
        title = page.title()
    except Exception as error:
        print(f"{name}: failed to load ({type(error).__name__})  pages HTTP {documents}")
        context.close()
        return
    challenge = CHALLENGE.search(text) or CHALLENGE.search(title)
    priced = [call for call in calls if call[0]]
    everything = text + "".join(bodies)
    airlines = "  ".join(f"{airline} {len(re.findall(airline, everything))}" for airline in AIRLINES)
    print(f"{name}: pages HTTP {documents}  title {title[:40]!r}  "
          f"challenge: {challenge.group(0) if challenge else 'none'}  "
          f"{len(priced)} of {len(calls)} JSON responses carry prices  {time.monotonic() - started:.0f}s\n"
          f"    airlines named: {airlines}")
    for fields, kb, where in sorted(priced, reverse=True)[:3]:
        print(f"    {fields:5d} price fields  {kb:4d} KB  {where}")
    context.close()


if __name__ == "__main__":
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for name in sys.argv[1:] or SITES:
            probe(browser, name, SITES[name])
        browser.close()
