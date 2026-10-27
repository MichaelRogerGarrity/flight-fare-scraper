from typing import Dict, Type

from .base import BaseScraper
from .fli_source import FliScraper
from .flightpowers import FlightPowersScraper
from .kayak import KayakScraper
from .serp_google import SearchApiScraper, SerpApiScraper

# A route or query picks its source by name (`site`), which is also stored on each row.
SCRAPERS: Dict[str, Type[BaseScraper]] = {
    scraper.name: scraper
    for scraper in (KayakScraper, SerpApiScraper, SearchApiScraper, FlightPowersScraper, FliScraper)
}


def get_scraper_class(site: str) -> Type[BaseScraper]:
    try:
        return SCRAPERS[site]
    except KeyError:
        raise ValueError(f"Unknown site '{site}'. Available: {', '.join(SCRAPERS)}")
