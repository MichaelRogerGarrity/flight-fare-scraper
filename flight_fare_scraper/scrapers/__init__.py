from typing import Dict, Type

from .base import BaseScraper
from .kayak import KayakScraper

SCRAPERS: Dict[str, Type[BaseScraper]] = {
    KayakScraper.name: KayakScraper,
}


def get_scraper_class(site: str) -> Type[BaseScraper]:
    try:
        return SCRAPERS[site]
    except KeyError:
        raise ValueError(f"Unknown site '{site}'. Available: {', '.join(SCRAPERS)}")
