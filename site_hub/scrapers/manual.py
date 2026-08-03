from __future__ import annotations

from typing import Any

from site_hub.scrapers.base import BaseScraper
from site_hub.scrapers.models import ScrapeResult


class ManualScraper(BaseScraper):
    scraper_type = "manual"

    async def scrape(self, site_url: str, config: dict[str, Any] | None = None) -> ScrapeResult:
        return ScrapeResult(True, metadata={"skipped": True, "reason": "manual catalog"})
