"""VShop scraper registry adapted to the Site Hub store/job boundary.

The concrete adapters are the full legacy implementations.  This module keeps
the legacy non-scraping and empty-result behavior while replacing direct calls
to the old global database module with the injected Site Hub store.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping
from uuid import UUID

from site_hub.scrapers.base import BaseScraper
from site_hub.scrapers.classic import ClassicScraper
from site_hub.scrapers.compat_config import SCRAPE_TIMEOUT_SECONDS
from site_hub.scrapers.custom_spa import CustomSpaScraper
from site_hub.scrapers.manual import ManualScraper
from site_hub.scrapers.models import ScrapeResult
from site_hub.scrapers.next_api import NextApiScraper
from site_hub.scrapers.network import sanitize_log_value


NON_SCRAPING_TYPES = {"manual", "none", "", None}


class ManagerScraper(BaseScraper):
    """Opt-in auto-detection adapter using the complete legacy implementations.

    Every adapter retains its own internal fallback logic.  Failure or an empty
    successful result advances to the next adapter instead of shortening the
    chain.  This adapter exists for imported/custom configurations that name
    ``manager`` explicitly; legacy sites with a concrete type remain concrete.
    """

    scraper_type = "manager"
    DEFAULT_PROBE_ORDER = ("next_api", "classic", "custom_spa")

    def __init__(self, adapters: Mapping[str, BaseScraper] | None = None) -> None:
        self.adapters: dict[str, BaseScraper] = dict(adapters or {
            "next_api": NextApiScraper(),
            "classic": ClassicScraper(),
            "custom_spa": CustomSpaScraper(),
        })

    async def scrape(
        self, site_url: str, scraper_config: dict[str, Any] | None = None,
    ) -> ScrapeResult:
        config = scraper_config or {}
        requested_order = config.get("probe_order") or self.DEFAULT_PROBE_ORDER
        order = [str(value) for value in requested_order]
        errors: list[str] = []
        empty_adapters: list[str] = []
        start = self._timer_start()

        for adapter_id in order:
            adapter = self.adapters.get(adapter_id)
            if adapter is None:
                errors.append(f"{adapter_id}: unknown adapter")
                continue
            try:
                result = await adapter.scrape(site_url, config)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(f"{adapter_id}: {sanitize_log_value(exc)}")
                continue
            if result.success and result.products:
                result.metadata.setdefault("selected_adapter", adapter_id)
                result.metadata.setdefault("probe_order", order)
                result.duration_seconds = self._timer_elapsed(start)
                return result
            if result.success:
                empty_adapters.append(adapter_id)
            elif result.error_message:
                errors.append(f"{adapter_id}: {sanitize_log_value(result.error_message)}")

        detail = "; ".join(errors)
        if empty_adapters:
            empty_detail = ", ".join(empty_adapters)
            detail = f"no products from {empty_detail}" + (f"; {detail}" if detail else "")
        return ScrapeResult(
            success=False,
            error_message=detail[:2000] or "no manager adapter found products",
            duration_seconds=self._timer_elapsed(start),
            metadata={"probe_order": order},
        )


# Public registry mirrors the old manager module and now includes the explicit
# manager adapter requested by the unified configuration model.
SCRAPER_REGISTRY: dict[str, BaseScraper] = {
    "classic": ClassicScraper(),
    "next_api": NextApiScraper(),
    "custom_spa": CustomSpaScraper(),
    "manual": ManualScraper(),
    "manager": ManagerScraper(),
}


async def test_scrape(
    site_url: str, scraper_type: str, scraper_config: str | dict[str, Any] = "{}",
) -> ScrapeResult:
    """Legacy-compatible, non-persisting test entry point."""
    scraper = SCRAPER_REGISTRY.get(scraper_type)
    if scraper is None:
        return ScrapeResult(success=False, error_message=f"unknown scraper type: {scraper_type}")
    if isinstance(scraper_config, str):
        try:
            config = json.loads(scraper_config or "{}")
        except (TypeError, ValueError):
            config = {}
    else:
        config = dict(scraper_config)
    return await scraper.scrape(site_url, config)


class ProductScraperService:
    """Timeout and persistence bridge used by product-monitor jobs."""

    def __init__(
        self, store: Any, *, timeout_seconds: int = SCRAPE_TIMEOUT_SECONDS,
        registry: Mapping[str, BaseScraper] | None = None,
    ) -> None:
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.registry = dict(registry or SCRAPER_REGISTRY)

    async def __call__(
        self, site: dict[str, Any], job: dict[str, Any],
    ) -> dict[str, Any]:
        runtime = await self.store.get_product_runtime(UUID(str(site["id"])))
        if not runtime or not runtime.get("enabled"):
            return {"skipped": True, "reason": "product monitoring disabled"}

        scraper_type = str(runtime.get("scraper_type") or "none")
        if scraper_type in NON_SCRAPING_TYPES:
            return {
                "skipped": True,
                "reason": "manual or non-scraping product catalog",
                "scraper": scraper_type,
            }
        scraper = self.registry.get(scraper_type)
        if scraper is None:
            raise ValueError(f"unknown product scraper: {scraper_type}")

        try:
            result = await asyncio.wait_for(
                scraper.scrape(
                    str(site["url"]), runtime.get("runtime_config") or {}
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"product scrape timed out after {self.timeout_seconds} seconds"
            ) from exc

        if not result.success:
            raise RuntimeError(result.error_message or "product scrape failed")

        # The old manager deliberately did not overwrite a catalog when a
        # scraper succeeded but found zero products.  Preserve that safety rule.
        persisted: dict[str, int] = {"created": 0, "updated": 0, "missing": 0}
        if result.products:
            persisted = await self.store.save_scrape_result(
                UUID(str(site["id"])),
                result.products,
                job_id=UUID(str(job["id"])),
                lease_token=str(job["lease_token"]),
            )

        return {
            **result.metadata,
            **persisted,
            "scraper": scraper_type,
            "products_found": len(result.products),
            "categories_found": len(result.categories),
            "duration_seconds": result.duration_seconds,
            "empty_result": not result.products,
        }


__all__ = [
    "ManagerScraper",
    "NON_SCRAPING_TYPES",
    "ProductScraperService",
    "SCRAPER_REGISTRY",
    "test_scrape",
]
