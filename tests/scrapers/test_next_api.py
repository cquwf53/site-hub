from __future__ import annotations

import asyncio

import httpx

from site_hub.scrapers.models import ScrapedProduct, ScrapeResult
from site_hub.scrapers import next_api as next_api_module
from site_hub.scrapers.next_api import API_PATHS, NextApiScraper
from site_hub.scrapers.network import (
    API_MAX_REDIRECTS,
    redact_url,
    sanitize_log_value,
)


def test_extract_products_list_preserves_legacy_response_shapes() -> None:
    scraper = NextApiScraper()
    item = {"name": "Plan"}

    assert scraper._extract_products_list([item]) == [item]
    assert scraper._extract_products_list({"products": [item]}) == [item]
    assert scraper._extract_products_list({"data": {"records": [item]}}) == [item]
    assert scraper._extract_products_list({"data": []}) is None
    assert scraper._extract_products_list([]) is None
    assert scraper._extract_products_list("not-json") is None


def test_parse_api_products_keeps_aliases_i18n_and_detail_url_fallbacks() -> None:
    products = NextApiScraper()._parse_api_products(
        [
            {
                "gd_name": {"zh-CN": "中文套餐", "en-US": "Plan"},
                "actual_price": "9.75",
                "auto_stock_available": "6",
                "sales_volume": "11",
                "category": {"name": {"zh-CN": "云服务", "en-US": "Cloud"}},
                "detail_url": "/goods/abc",
                "type": 1,
                "is_recommend": True,
                "gd_description": {"long": "value"},
            },
            {"price": 10},
        ],
        "https://shop.example",
    )

    assert len(products) == 1
    product = products[0]
    assert product.name == "中文套餐"
    assert product.price == 9.75
    assert product.original_price == "¥9.75"
    assert product.stock == 6
    assert product.stock_text == "6"
    assert product.sold == 11
    assert product.category_name == "云服务"
    assert product.buy_url == "https://shop.example/goods/abc"
    assert len(product.tags) == 2
    assert product.description == "{'long': 'value'}"


def test_known_api_success_short_circuits_probe_and_browser(monkeypatch) -> None:
    scraper = NextApiScraper()
    calls: list[tuple[str, str]] = []

    async def try_api(base_url: str, path: str, configured_headers=None):
        calls.append((base_url, path))
        return ScrapeResult(
            success=True, products=[ScrapedProduct(name="from configured api")]
        )

    async def should_not_probe(base_url: str, configured_headers=None):
        raise AssertionError("generic API probing must not run")

    async def should_not_browse(base_url: str, start_time: float):
        raise AssertionError("Playwright fallback must not run")

    monkeypatch.setattr(scraper, "_try_api", try_api)
    monkeypatch.setattr(scraper, "_probe_api_paths", should_not_probe)
    monkeypatch.setattr(scraper, "_scrape_with_playwright", should_not_browse)

    result = asyncio.run(
        scraper.scrape("https://shop.example/", {"api_path": "/known"})
    )

    assert result.success is True
    assert calls == [("https://shop.example", "/known")]


def test_api_misses_fall_back_to_playwright(monkeypatch) -> None:
    scraper = NextApiScraper()
    calls: list[str] = []

    async def no_probe_result(base_url: str, configured_headers=None):
        calls.append(f"probe:{base_url}")
        return None

    async def browser_result(base_url: str, start_time: float):
        calls.append(f"browser:{base_url}")
        return ScrapeResult(
            success=True, products=[ScrapedProduct(name="browser fallback")]
        )

    monkeypatch.setattr(scraper, "_probe_api_paths", no_probe_result)
    monkeypatch.setattr(scraper, "_scrape_with_playwright", browser_result)

    result = asyncio.run(scraper.scrape("https://shop.example/"))

    assert result.success is True
    assert calls == [
        "probe:https://shop.example",
        "browser:https://shop.example",
    ]


def test_probe_runs_all_legacy_paths_and_returns_first_product_result(monkeypatch) -> None:
    scraper = NextApiScraper()
    seen: set[str] = set()

    async def try_api(base_url: str, path: str, configured_headers=None):
        seen.add(path)
        await asyncio.sleep(0)
        if path == API_PATHS[-1]:
            return ScrapeResult(
                success=True, products=[ScrapedProduct(name="found")]
            )
        return None

    monkeypatch.setattr(scraper, "_try_api", try_api)

    result = asyncio.run(scraper._probe_api_paths("https://shop.example"))

    assert result is not None
    assert result.products[0].name == "found"
    assert seen == set(API_PATHS)


def test_api_client_verifies_tls_limits_redirects_and_parses_stream(monkeypatch) -> None:
    captured: dict = {}
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept"] == "application/json"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"products": [{"id": 7, "name": "Safe plan", "price": 12}]},
        )

    def client_factory(**kwargs):
        captured.update(kwargs)
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(next_api_module.httpx, "AsyncClient", client_factory)

    result = asyncio.run(
        NextApiScraper()._try_api(
            "https://user:password@shop.example?access_token=secret",
            "/api/products",
        )
    )

    assert result is not None and result.success is True
    assert result.products[0].name == "Safe plan"
    assert captured["follow_redirects"] is True
    assert captured["max_redirects"] == API_MAX_REDIRECTS
    assert "verify" not in captured
    assert isinstance(captured["timeout"], httpx.Timeout)


def test_api_rejects_declared_and_streamed_oversized_bodies(monkeypatch) -> None:
    original_client = httpx.AsyncClient

    def declared_too_large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "17", "content-type": "application/json"},
            content=b"{}",
        )

    def first_factory(**kwargs):
        return original_client(transport=httpx.MockTransport(declared_too_large), **kwargs)

    monkeypatch.setattr(next_api_module, "API_MAX_RESPONSE_BYTES", 16)
    monkeypatch.setattr(next_api_module.httpx, "AsyncClient", first_factory)
    assert asyncio.run(
        NextApiScraper()._try_api("https://shop.example", "/api/products")
    ) is None

    def streamed_too_large(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"{" + (b" " * 16) + b"}",
        )

    def second_factory(**kwargs):
        return original_client(transport=httpx.MockTransport(streamed_too_large), **kwargs)

    monkeypatch.setattr(next_api_module.httpx, "AsyncClient", second_factory)
    assert asyncio.run(
        NextApiScraper()._try_api("https://shop.example", "/api/products")
    ) is None


def test_api_total_timeout_is_bounded(monkeypatch) -> None:
    class SlowStream:
        async def __aenter__(self):
            await asyncio.sleep(1)

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class SlowClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def stream(self, *args, **kwargs):
            return SlowStream()

    monkeypatch.setattr(next_api_module, "API_TOTAL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(next_api_module.httpx, "AsyncClient", SlowClient)

    assert asyncio.run(
        NextApiScraper()._try_api("https://shop.example", "/api/products")
    ) is None


def test_api_probe_cancellation_is_propagated_and_children_are_drained(monkeypatch) -> None:
    scraper = NextApiScraper()
    children_finished = 0

    async def slow_try_api(base_url: str, path: str, configured_headers=None):
        nonlocal children_finished
        try:
            await asyncio.sleep(60)
        finally:
            children_finished += 1

    monkeypatch.setattr(scraper, "_try_api", slow_try_api)

    async def scenario() -> None:
        task = asyncio.create_task(scraper._probe_api_paths("https://shop.example"))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        raise AssertionError("probe cancellation must propagate")

    asyncio.run(scenario())
    assert children_finished == len(API_PATHS)


def test_url_and_header_redaction_removes_credentials_and_secret_values() -> None:
    safe_url = redact_url(
        "https://alice:password@example.test:8443/path?token=top-secret#session"
    )
    assert safe_url == "https://example.test:8443/path?redacted#redacted"

    safe_message = sanitize_log_value(
        "GET https://alice:password@example.test/path?token=top-secret "
        "Authorization: Bearer auth-secret\nCookie: sid=cookie-secret"
    )
    assert "password" not in safe_message
    assert "top-secret" not in safe_message
    assert "auth-secret" not in safe_message
    assert "cookie-secret" not in safe_message
    assert "Authorization:[redacted]" in safe_message
    assert "Cookie:[redacted]" in safe_message

    assigned_secrets = sanitize_log_value(
        'token=plain-token password: plain-password '
        '"api_key":"plain-api-key" webhook_url=https://hooks.example.test/private/key'
    )
    assert "plain-token" not in assigned_secrets
    assert "plain-password" not in assigned_secrets
    assert "plain-api-key" not in assigned_secrets
    assert "hooks.example.test" not in assigned_secrets
    assert assigned_secrets.count("[redacted]") == 4
