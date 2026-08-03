from __future__ import annotations

import asyncio

import pytest

from site_hub.scrapers.base import BaseScraper
from site_hub.scrapers.models import ScrapedProduct, ScrapeResult


class _SlowScraper(BaseScraper):
    scraper_type = "slow"

    async def scrape(self, site_url: str, config=None) -> ScrapeResult:
        await asyncio.sleep(1)
        return ScrapeResult(success=True)


def test_scraped_product_legacy_defaults_and_store_shape() -> None:
    product = ScrapedProduct(name="test product")

    assert product.price is None
    assert product.stock == -1
    assert product.sold == 0
    assert product.tags == []
    assert product.as_dict() == {
        "name": "test product",
        "external_key": None,
        "price": None,
        "original_price": None,
        "stock": -1,
        "stock_text": None,
        "sold": 0,
        "buy_url": None,
        "category_name": None,
        "description": None,
        "tags": [],
    }


def test_scrape_result_legacy_success_shape() -> None:
    result = ScrapeResult(
        success=True,
        products=[ScrapedProduct(name="a", price=1.0)],
        categories=["all"],
        duration_seconds=1.2,
    )

    assert result.success is True
    assert len(result.products) == 1
    assert result.error_message is None
    assert result.metadata == {}


def test_legacy_timer_uses_wall_clock_and_two_decimal_places(monkeypatch) -> None:
    scraper = _SlowScraper()
    clock = iter((10.0, 11.234))
    monkeypatch.setattr("site_hub.scrapers.base.time.time", lambda: next(clock))

    started = scraper._timer_start()

    assert started == 10.0
    assert scraper._timer_elapsed(started) == 1.23


def test_external_timeout_still_cancels_slow_adapter() -> None:
    async def invoke() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                _SlowScraper().scrape("https://example.com"), timeout=0.001
            )

    asyncio.run(invoke())
