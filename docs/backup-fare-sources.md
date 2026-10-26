# Backup fare sources

Notes for the case where Kayak stays closed to the scheduled runners. Researched
2026-10-25, after Kayak began refusing GitHub-hosted runners across the board on
2026-10-23 (see "The circuit breaker" in the README). **This is a contingency plan,
not a migration:** Kayak data is richer for our purposes, so switch only if access
doesn't come back within a reasonable time.

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
- [Skyscanner Travel API eligibility](https://skyscannerpartnersupport.zendesk.com/hc/en-us/articles/25486916837405-What-is-the-acceptance-criteria-for-being-eligible-for-the-Car-Hire-API)
