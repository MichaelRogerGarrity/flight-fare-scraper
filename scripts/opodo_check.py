"""A run of Opodo searches through the `opodo` source: does it keep answering?

Placeholder routes unrelated to anything this repo tracks, one a metro code; prints one
line per search (outcome, rows kept, nonstop rows, cheapest) and a total. Run from the
repository root, so the package imports.

    python scripts/opodo_check.py SEARCHES PAUSE_SECONDS
"""

import logging
import sys
import time
from datetime import date, timedelta

from flight_fare_scraper.config import build_query
from flight_fare_scraper.scrapers.opodo import OpodoScraper

ROUTES = [("JFK", "LAX"), ("BOS", "DEN"), ("ATL", "LAS"), ("NYC", "SFO"), ("SEA", "HNL"), ("MIA", "PHX")]


def main():
    searches, pause = int(sys.argv[1]), float(sys.argv[2])
    logging.basicConfig(level=logging.WARNING)
    counts = {}
    with OpodoScraper() as scraper:
        for i in range(searches):
            origin, destination = ROUTES[i % len(ROUTES)]
            depart = date.today() + timedelta(days=30 + 7 * (i // len(ROUTES)))
            query = build_query(origin, destination, depart.isoformat(), (depart + timedelta(days=7)).isoformat(),
                                nonstop="false", site="opodo")
            started = time.monotonic()
            try:
                rows = scraper.search(query)
                outcome = "ok"
                cheapest = min(rows, key=lambda row: row.price) if rows else None
                detail = (f"{len(rows):4d} rows  {sum(1 for r in rows if not r.outbound_stops and not r.return_stops):4d}"
                          f" nonstop  cheapest {cheapest.currency} {cheapest.price:,.0f}" if cheapest else "no rows")
            except Exception as error:
                outcome, detail = type(error).__name__, ""
            counts[outcome] = counts.get(outcome, 0) + 1
            print(f"{i + 1:3d} {origin}-{destination} {depart}  {outcome:18s} {detail}  "
                  f"{time.monotonic() - started:4.0f}s", flush=True)
            if i < searches - 1:
                time.sleep(pause)
    print("\n" + ", ".join(f"{n} {outcome}" for outcome, n in counts.items()) + f" of {searches}")


if __name__ == "__main__":
    main()
