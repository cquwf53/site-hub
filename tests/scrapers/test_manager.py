from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from site_hub.scrapers.base import BaseScraper
from site_hub.scrapers.manager import (
    ManagerScraper,
    ProductScraperService,
    SCRAPER_REGISTRY,
    test_scrape as run_test_scrape,
)
from site_hub.scrapers.models import ScrapedProduct, ScrapeResult


SITE_ID = "11111111-1111-1111-1111-111111111111"
JOB_ID = "22222222-2222-2222-2222-222222222222"
LEASE_TOKEN = "current-product-lease"


class _ResultScraper(BaseScraper):
    def __init__(self, result: ScrapeResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    async def scrape(self, site_url: str, config=None) -> ScrapeResult:
        self.calls.append((site_url, config or {}))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Store:
    def __init__(self, runtime: dict | None) -> None:
        self.runtime = runtime
        self.saved: list[tuple[UUID, list[ScrapedProduct], UUID, str]] = []

    async def get_product_runtime(self, site_id: UUID):
        assert site_id == UUID(SITE_ID)
        return self.runtime

    async def save_scrape_result(self, site_id, products, *, job_id, lease_token):
        self.saved.append((site_id, products, job_id, lease_token))
        return {"created": 1, "updated": 2, "missing": 3}


def _call(service: ProductScraperService):
    return asyncio.run(
        service(
            {"id": SITE_ID, "url": "https://shop.example"},
            {"id": JOB_ID, "lease_token": LEASE_TOKEN},
        )
    )


def test_public_registry_contains_every_legacy_adapter_and_manager() -> None:
    assert set(SCRAPER_REGISTRY) == {
        "classic",
        "next_api",
        "custom_spa",
        "manual",
        "manager",
    }


def test_manager_falls_through_exception_and_empty_success() -> None:
    broken = _ResultScraper(RuntimeError("network failed"))
    empty = _ResultScraper(ScrapeResult(success=True))
    winning = _ResultScraper(
        ScrapeResult(
            success=True,
            products=[ScrapedProduct(name="winner")],
            metadata={"source": "legacy"},
        )
    )
    manager = ManagerScraper(
        {"broken": broken, "empty": empty, "winning": winning}
    )

    result = asyncio.run(
        manager.scrape(
            "https://shop.example",
            {"probe_order": ["broken", "empty", "winning"]},
        )
    )

    assert result.success is True
    assert result.products[0].name == "winner"
    assert result.metadata == {
        "source": "legacy",
        "selected_adapter": "winning",
        "probe_order": ["broken", "empty", "winning"],
    }
    assert len(broken.calls) == len(empty.calls) == len(winning.calls) == 1


def test_manager_redacts_adapter_errors_before_persisting_them() -> None:
    leaking = _ResultScraper(
        RuntimeError(
            "GET https://user:password@example.test/api?token=secret "
            "Authorization: Bearer auth-secret Cookie: sid=cookie-secret"
        )
    )
    manager = ManagerScraper({"leaking": leaking})

    result = asyncio.run(manager.scrape(
        "https://example.test", {"probe_order": ["leaking"]}
    ))

    assert result.success is False
    assert "password" not in result.error_message
    assert "secret" not in result.error_message
    assert "[redacted]" in result.error_message


@pytest.mark.parametrize("scraper_type", ["manual", "none", ""])
def test_service_skips_non_scraping_types_without_persisting(scraper_type) -> None:
    store = _Store(
        {"enabled": True, "scraper_type": scraper_type, "runtime_config": {}}
    )

    payload = _call(ProductScraperService(store))

    assert payload["skipped"] is True
    assert store.saved == []


def test_service_does_not_mark_catalog_missing_for_empty_success() -> None:
    store = _Store(
        {"enabled": True, "scraper_type": "empty", "runtime_config": {}}
    )
    scraper = _ResultScraper(ScrapeResult(success=True, categories=["All"]))

    payload = _call(ProductScraperService(store, registry={"empty": scraper}))

    assert payload["products_found"] == 0
    assert payload["categories_found"] == 1
    assert payload["empty_result"] is True
    assert payload["created"] == payload["updated"] == payload["missing"] == 0
    assert store.saved == []


def test_service_persists_nonempty_result_through_store_boundary() -> None:
    store = _Store(
        {
            "enabled": True,
            "scraper_type": "fixture",
            "runtime_config": {"api_path": "/configured"},
        }
    )
    product = ScrapedProduct(name="Plan", external_key="p-1", price=5.0)
    scraper = _ResultScraper(
        ScrapeResult(
            success=True,
            products=[product],
            categories=["Cloud"],
            duration_seconds=0.25,
            metadata={"selected_adapter": "fixture"},
        )
    )

    payload = _call(ProductScraperService(store, registry={"fixture": scraper}))

    assert scraper.calls == [
        ("https://shop.example", {"api_path": "/configured"})
    ]
    assert store.saved == [
        (UUID(SITE_ID), [product], UUID(JOB_ID), LEASE_TOKEN)
    ]
    assert payload == {
        "selected_adapter": "fixture",
        "created": 1,
        "updated": 2,
        "missing": 3,
        "scraper": "fixture",
        "products_found": 1,
        "categories_found": 1,
        "duration_seconds": 0.25,
        "empty_result": False,
    }


def test_service_maps_timeout_to_job_runtime_error() -> None:
    store = _Store(
        {"enabled": True, "scraper_type": "slow", "runtime_config": {}}
    )
    slow = _ResultScraper(ScrapeResult(success=True))

    async def delayed_scrape(site_url: str, config=None):
        await asyncio.sleep(1)
        return ScrapeResult(success=True)

    slow.scrape = delayed_scrape
    service = ProductScraperService(
        store, timeout_seconds=0.001, registry={"slow": slow}
    )

    with pytest.raises(RuntimeError, match="timed out"):
        _call(service)
    assert store.saved == []


def test_legacy_test_entry_point_reports_unknown_scraper() -> None:
    result = asyncio.run(
        run_test_scrape("https://shop.example", "does-not-exist", "not-json")
    )

    assert result.success is False
    assert result.error_message == "unknown scraper type: does-not-exist"
