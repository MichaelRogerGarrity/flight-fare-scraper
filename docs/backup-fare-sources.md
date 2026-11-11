# Backup fare sources

Notes for the case where Kayak stays closed to the scheduled runners. Researched
2026-10-25, after Kayak began refusing GitHub-hosted runners across the board on
2026-10-23 (see "The circuit breaker" in the README). **This is a contingency plan,
not a migration:** Kayak data is richer for our purposes, so switch only if access
doesn't come back within a reasonable time.

**Update 2026-10-27:** the free options are built as sources -- `serpapi`, `searchapi`,
`flightpowers` and `fli` (README, "Sources") -- and not yet run against the live
services. GitHub's runners were still answered with a CAPTCHA on 2026-10-27.

**Update 2026-11-05:** from GitHub's runners, Kayak still answers with a CAPTCHA; `fli`
answered 3 of 4 small searches (1 request each at `top_n=1`, one empty); Skiplagged
answered 4 of 4. On the two routes and dates both searched, Skiplagged's cheapest
round trip (two one-ways, hidden-city fares excluded) was within $2-6 of Google's.
A longer Skiplagged run the same evening answered **40 of 40** (one request every 2
minutes over ~80 minutes, departures 30-72 days out), 3-9 s each, with no sign of
slowing. One answer (a holiday-week departure) had no non-hidden-city outbound option,
so it gave no round trip. Google refused the runners after a few dozen requests on
2026-10-26; Skiplagged showed nothing like that here.

## Short version

1. **[SerpApi's Google Flights API](https://serpapi.com/google-flights-api)** is the
   backup. Paid and managed, so blocking is their problem, and it also exposes Google's
   price insights, which nothing else here does.
2. **Google Flights through [`fli`](https://github.com/punitarani/fli)** has good data --
   its cheapest fare matched Kayak's to the dollar -- but **Google refuses GitHub's
   runners within a few searches** (tested 2026-10-26, below). It is only usable from a
   residential connection, and running it daily from one risks getting that address
   challenged across all of google.com.
3. Everything else is either closed to us, dead, or measures something different.

## Options

| source | access | cost at ~250 searches/day | data | main risk |
| --- | --- | --- | --- | --- |
| **Google Flights via `fli`** | open source (MIT), `pip install flights` (0.9.0 tested) | free | ~30 curated round-trip itineraries per search; per-leg airline, flight number, aircraft, times, legroom; includes Southwest | **refused from GitHub's runners** after 3-9 searches; undocumented API can change |
| **SerpApi Google Flights** | API key | ~$150/month: 250 searches/month free, then $25/1k ... $150/15k; a round trip takes 2 calls (outbound list, then returns via `departure_token`) | same Google data, plus `price_insights`: lowest price, price level, typical range, price history | cost; 50 searches/hour cap on the free plan |
| **Duffel** | account; live mode needs approval | ~$40/month: $0.005 per search beyond a 1500:1 search-to-book ratio, and we book nothing | airline-direct (NDC/GDS) fares, no online-agency prices | approval; no low-cost carriers like Southwest; fares not comparable to metasearch |
| **Travelpayouts (Aviasales) data API** | free affiliate token | free | cached prices from other people's searches, 2-7 days old | sparse for exact dates; not live |
| Momondo | website | - | - | **not a hedge**: managed by Kayak, same parent, likely the same defenses |
| Amadeus Self-Service | - | - | - | **gone**: decommissioned 17 July 2026; enterprise access only |
| Kiwi.com Tequila | - | - | - | **closed**: invitation-only since May 2024 |
| Skyscanner Travel API | - | - | - | **ineligible**: partners only, businesses with >100k monthly traffic |

## The `fli` test

One round trip on a domestic nonstop route, searched with Kayak and with `fli` the same
afternoon from a residential connection:

- 33 seconds, 30 round-trip itineraries, HTTP-only (no browser)
- cheapest fare identical to Kayak's
- includes Southwest, which many aggregators omit
- one price per itinerary: no per-reseller listings

### From GitHub's runners (`.github/workflows/backup-source-check.yml`)

Twelve round trips on placeholder routes, `top_n=5` (6 requests each), fresh runner each:

| spacing | succeeded | then |
| --- | --- | --- |
| 30 s | 9 (~54 requests in ~5 min) | instant empty responses for the rest |
| 2 min | 3 (~18 requests) | instant empty responses for the remaining 20 minutes |

Slower spacing did worse, so this is not a rate limit that pacing could work under: Google
stops answering GitHub's runner addresses after a few dozen requests, and the second
runner got less than the first, suggesting the range as a whole was flagged. The same
pattern as Kayak's block of the runners on 2026-10-23.

### Usage limits

`fli` imposes none beyond its own throttle (10 requests a second, with a round trip's
follow-up requests sent in parallel). A round trip costs `1 + top_n` requests -- `top_n`
outbound options are each expanded into their returns, capped at 10 -- so ~250 round trips
a day is ~1,500 requests at the default, ~500 at `top_n=1`. Google publishes no limit for
the endpoint (it is undocumented); robots.txt disallows `/travel/flights/search` though not
the `/_/FlightsFrontendUi/` endpoint `fli` calls.

## Other sites to scrape (explored 2026-10-27)

Checked from a residential connection: each site's robots.txt, whether a plain request
meets a bot challenge, and its terms. Nearly every site's terms forbid automated access
without written permission -- Kayak's included ("use automated tools on our Site without
our written permission") -- so the terms don't separate the candidates. What does is
whether robots.txt or a bot check stands in the way, and we don't get past either.

| site | robots.txt on search | bot check | verdict |
| --- | --- | --- | --- |
| **Skiplagged** | allowed (only redirect/booking paths disallowed) | none on one test request | **the one open option**, see below |
| Kiwi.com | can't tell: even robots.txt is served a "Client Challenge" page | yes, on every request | out |
| ITA Matrix | none (single-page app) | the search API (`alkalimatrix-pa.googleapis.com`) carries Google BotGuard challenges | out |
| Skyscanner | `/g/*`, `/trip/api/` disallowed | PerimeterX | out |
| Expedia group (Orbitz, Travelocity, Hotwire ...) | `/Flights-Search`, `/Flight-SearchResults` disallowed | HUMAN/PerimeterX | out |
| Trip.com | `/restapi/soa2/*` (its search API) and `*searchresults*` disallowed | - | out |
| Aviasales site | `/search` disallowed | - | out (its data API, Travelpayouts, is allowed -- table above) |
| Priceline, Momondo, Cheapflights | - | - | **not a hedge**: Booking Holdings, Kayak's family; Cheapflights runs on Kayak's own platform |
| `fast-flights` | - | - | a second library for the same Google endpoint `fli` uses; same block risk, no new data |

### Skiplagged

One round trip on a placeholder route (NYC area to LA, 7 nights) through the endpoint its
own site calls, `https://skiplagged.com/api/search.php?from=..&to=..&depart=..&return=..&format=v3`:

- HTTP 200, plain JSON, ~280 KB, 5.6 s; no browser, cookie or token needed
- 107 outbound and 154 return options, each with full segments: airline, flight number,
  airports, times with offsets, durations
- **one-way pricing**: each direction is priced on its own (in cents), and the round trip
  is the two added together (`min_round_trip_price` gives the cheapest pairing). Kayak and
  Google price the round trip as one ticket, which can differ
- **hidden-city fares are mixed in**: 13 of 107 outbound and 16 of 154 returns flew on
  past the destination (e.g. NYC-CLT-LA-SMF, getting off in LA), and those were the
  cheapest on both sides. They have to be filtered out (last segment's arrival isn't the
  destination) or the minimum is a fare you can't use with a checked bag
- `incomplete: true` -- the site polls for more; one request may be a partial answer
- nothing on bags, cabin or seller; no price prediction

Its terms forbid "any robot, spider, scraper or other automated means" without permission,
the same as Kayak's.

**From GitHub's runners** (`.github/workflows/skiplagged-check.yml`, 2026-10-27): 6 of 6
round trips answered, one request each, a minute apart, 3-6 s and 180-370 KB per answer,
39-156 non-hidden-city options per direction. Every answer was `incomplete: true`, so
whether a single request gets the cheapest fare is still open -- comparing with Kayak or
Google on the same dates would settle it. Six searches don't show how far it holds; the
Google test refused runners only after a few dozen requests.

## More sites, checked in a browser (2026-11-05)

A plain headless Chromium with no stealth patches (`scripts/probe_sites.py`,
`.github/workflows/site-probe.yml`), one search page each on a placeholder route, from
home and from GitHub's runners:

| site | answer |
| --- | --- |
| **Opodo** | fares, no challenge, from home and the runners -- see below |
| eDreams, Wego | Cloudflare challenge |
| Trip.com | HTTP 432 |
| Skyscanner | PerimeterX |
| Aviasales | AWS WAF; no results |
| Booking.com flights | 202 challenge |
| Gotogate, Mytrip, CheapOair, FlightHub, JustFly | 403 |

### Opodo

The search page (`https://www.opodo.com/travel/#results/type=R;from=..;to=..;dep=..;ret=..`)
gets its fares from one call it makes itself: `POST /itinerary-selection-bff/service/graphql`,
operation `searchItinerary`. Its answer:

- **real round-trip tickets**, priced as one: no one-way stitching, no hidden-city fares
- 300-800 itineraries per search on domestic routes
- two prices per itinerary: the regular one (`MEMBER_PRICE_POLICY_UNDISCOUNTED`) and a
  lower one for Opodo's paid Prime membership (`..._DISCOUNTED`, e.g. EUR 307 against
  EUR 355). The regular price is the comparable figure
- **prices in EUR** on opodo.com, with no currency in the request; a source would convert
- per segment: flight number, marketing and operating carrier, aircraft, cabin, terminals,
  times with offsets; a cabin-bag rule per leg (`CABIN_INCLUDED` or `BASED_ON_AIRLINE`);
  seats left per itinerary
- ~45 s per search in a browser

**From GitHub's runners** (`.github/workflows/opodo-check.yml`, `scripts/opodo_check.py`):
**20 of 20** round trips answered, one browser, one search every ~3.8 minutes over ~75
minutes, six routes, departures 31-52 days out. 35-811 itineraries each (the 35 was a
late-December date); cheapest regular fares EUR 282-830. No challenge and no slowing.

**2026-11-06:** Opodo began answering the home connection with a Cloudflare challenge
(HTTP 403, "Just a moment...") on every fares call, while a runner the same hour got 2 of
2 searches as before. So it's the home address that is flagged, not automation in
general. Built as the `opodo` source (README, "Sources"), for runner workflows.

**Its biggest gap: Delta, United and Southwest aren't sold.** The JFK-LAX answer listed
only American, Alaska, Frontier and JetBlue; ATL-LAS (a Delta hub) had Frontier as its
only nonstop, and BOS-DEN no nonstop at all, though both routes have several nonstop
airlines. Each search's page gets one complete answer (logged on a runner, 2026-11-06),
so this is Opodo's inventory, not a partial read. Its cheapest fare is the cheapest among
the airlines it sells, which on many US routes is not the market's cheapest -- compare
with Kayak before trusting it on a route.

## Which sites sell every airline (2026-11-06)

Opodo lacks Delta, United and Southwest, so the search went on, from GitHub's runners
only (to keep the home connection out of it), on BOS-DEN 8-15 Dec 2026, a route United
and Southwest fly nonstop and Delta and American with connections (`scripts/probe_sites.py`).

| site | from a runner | airlines it sells |
| --- | --- | --- |
| **Almosafer** (Saudi agency, Amadeus) | fares, no challenge -- but a guest token, so only through its page | Delta, United, American, Southwest, Alaska; **no JetBlue, Frontier or Spirit** |
| **Agoda** | fares, no challenge | Southwest, JetBlue, American, Alaska, Frontier, Sun Country; **no Delta or United** |
| Frontier's own site | fares, no challenge | Frontier only |
| Google Flights | page loads; its fares call comes back empty | -- |
| Priceline | PerimeterX "Press & Hold" | -- |
| Expedia, Orbitz | "Bot or Not?" (HTTP 429) | -- |
| American, Delta | Access Denied (403, 444) | -- |
| JetBlue, Alaska, Hopper | challenge pages | -- |
| Southwest | its search page answers a deep link with an error | -- |
| CheapOair | drops the deep link to its home page; sells no Delta anyway | -- |
| StudentUniverse, lastminute.com, JetCost | Cloudflare challenge | -- |
| Kiwi | 403 | -- |
| Traveloka | "confirm you are human" | -- |
| MakeMyTrip | wouldn't load | -- |

The cheapest round trip by outbound airline on six placeholder routes, from Almosafer,
Agoda and Skiplagged the same evening, showed what coverage alone hides:
- Almosafer's American and United fares ran well above the others' where all three
  had them (DFW-SFO: $414 against $262), apparently because Basic Economy isn't sold
  through Amadeus. Its Southwest and Delta fares were in line.
- Skiplagged's single request was incomplete on most routes (ATL-LAS: only Frontier;
  another: nothing).
- Agoda's first page of 20 results isn't enough to find each airline's cheapest.

**Conclusion: no free site we can read sells every airline at its lowest fare.** The two
that do are Kayak and Google Flights, and both refuse GitHub's runners. Southwest's fares
have been on both since 2024. So the all-airline source for runners is Google Flights
through a service that fetches it for us: Apify's Google Flights API actor
($0.20 per 1,000 rows, $5 free a month), built as `apify_google` (README, "Sources"),
not yet run live. SerpApi and SearchApi (`serpapi`, `searchapi`) are the same data at a
higher price.

## Connectors for AI assistants (MCP servers, 2026-11-11)

Travel sites now publish MCP servers -- their own doors for AI assistants -- which the
earlier surveys missed. Found through Claude's connector directory and the official MCP
registry (registry.modelcontextprotocol.io, 193 flight-related entries), and called from
the home connection with a plain MCP client, BOS-DEN 8-15 Dec 2026 and five more
placeholder routes:

| connector | answers without a login | what it gives |
| --- | --- | --- |
| Expedia (`expedia.com/mcp`, in Claude's directory) | no: a bare 403 to anything but, apparently, AI platforms' own servers | -- |
| SlickTrip | lists its tools, but searching needs a SlickTrip login | -- |
| LetsFG, Ignav, FlightPowers, Alice | need an account or key | -- |
| ToolOracle | needs a registered key (500 free units a month) | -- |
| lastminute.com | Cloudflare challenge | -- |
| eDreams | "Request blocked" | (Opodo's group: same airline gaps) |
| **wondel.ai Google Flights** | yes, free, no stated quota | SerpApi's Google Flights answer as-is -- every airline, Southwest included, with price insights and returns. **But its legal notice forbids querying "in bulk or systematically" and building "price databases from the results"**: personal lookups only |
| **Skiplagged** (`mcp.skiplagged.com`) | yes | up to 100 rows, hidden-city fares can be switched off -- but every outbound is paired with one cheapest one-way return, mostly separate tickets on another airline, all labelled "standard"; JFK-LAX came back priced $0; no Southwest |
| **Kiwi.com** (`mcp.kiwi.com`, official) | yes, fast (1-7 s) | real round trips with bags, but 15 results, and **no Delta, United, American or Southwest at all** (asked for each by code: none) |
| **Octotrip** | yes, free, 1 request/s | Aviasales' live search: 6 curated results of 2,600-5,000, mostly from small resale agencies; no Delta seen |
| Pointify | yes, but its terms bar automated use beyond its paid API tiers | Duffel, Google Flights and award seats |

So the connectors change nothing for a free, every-airline, automated source: the one
with full Google data forbids building a price history from it, and the rest are missing
the big four airlines or pair fares oddly. Expedia's and SlickTrip's remain useful inside
the Claude app for looking a fare up by hand.

## If we switch

- **Add it beside Kayak, not instead of it.** It fits the existing `BaseScraper`
  interface as a second site. `site` is already part of the dedup key, so both can run and
  store side by side, and the history stays comparable within each.
- **What carries over:** cheapest fare, cheapest nonstop, cheapest with each leg under a
  duration limit, airline, times, stops, layovers.
- **What doesn't:** anything weighted by resellers. Google returns ~30 curated
  itineraries, not 1,000+ seller listings, so counts and averages across rows mean
  something different. `MIN(price)` is the comparable figure.
- **Where it runs:** not from GitHub's runners (above). Through SerpApi, the runners only
  call SerpApi, which does the fetching.
- **Search budget:** a SerpApi round trip is 2 calls (outbound list, then returns via
  `departure_token`); the same worst-day discipline (`cli budget`) applies, and the circuit
  breaker and deadline carry over unchanged.

## Sources

- [punitarani/fli on GitHub](https://github.com/punitarani/fli)
- [SerpApi Google Flights API](https://serpapi.com/google-flights-api) and [pricing](https://serpapi.com/pricing)
- [Duffel pricing](https://duffel.com/pricing)
- [Travelpayouts: requirements for Aviasales data API access](https://support.travelpayouts.com/hc/en-us/articles/203956083-Requirements-for-Aviasales-data-API-access)
- [momondo: About us](https://www.momondo.com/about) (managed by Kayak, part of Booking Holdings)
- [Amadeus Self-Service shutdown](https://ignav.com/docs/amadeus-self-service-shutdown)
- [Kiwi.com Tequila API status](https://www.vervotech.com/hub/integrations/kiwi-com-api/)
- [Skiplagged terms](https://skiplagged.com/terms) and [Kayak terms of use](https://www.kayak.com/terms-of-use)
- [fast-flights](https://github.com/AWeirdDev/flights)
- [Opodo](https://www.opodo.com/)
- [Apify: Google Flights API actor](https://apify.com/lergassy/google-flights-api) and [Apify pricing](https://apify.com/pricing)
- [Southwest fares on Google Flights](https://thriftytraveler.com/news/airlines/southwest-fares-now-appear-on-google-flights/) and [on Kayak](https://onemileatatime.com/news/southwest-airlines-flights-kayak/)
- [Skyscanner Travel API eligibility](https://skyscannerpartnersupport.zendesk.com/hc/en-us/articles/25486916837405-What-is-the-acceptance-criteria-for-being-eligible-for-the-Car-Hire-API)
