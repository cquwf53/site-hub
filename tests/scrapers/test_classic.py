from __future__ import annotations

import asyncio

from bs4 import BeautifulSoup

from site_hub.scrapers.classic import ClassicScraper


class _LayoutPage:
    def __init__(self, matches: set[str]) -> None:
        self.matches = matches
        self.queries: list[str] = []

    async def query_selector(self, selector: str):
        self.queries.append(selector)
        return object() if selector in self.matches else None


def test_layout_detection_preserves_myuki_indicator_fallbacks() -> None:
    async def invoke() -> None:
        scraper = ClassicScraper()
        direct = _LayoutPage({"td.commodity-click"})
        paired = _LayoutPage({"text=商品分类", "text=选择商品"})
        standard = _LayoutPage(set())

        assert await scraper._detect_layout(direct) == "myuki_table"
        assert await scraper._detect_layout(paired) == "myuki_table"
        assert await scraper._detect_layout(standard) == "standard"

    asyncio.run(invoke())


def test_single_card_parser_keeps_legacy_price_stock_sold_link_and_tags() -> None:
    card = BeautifulSoup(
        """
        <article class="product-card">
          <a href="/buy/42"><h5>Legacy Plan</h5></a>
          <span class="price">¥12.50</span>
          <span>库存：7</span><span>已售：23</span>
          <span class="badge">hot</span>
        </article>
        """,
        "html.parser",
    ).select_one("article")

    product = ClassicScraper()._parse_single_card(
        card, "https://shop.example", "Subscriptions"
    )

    assert product is not None
    assert product.name == "Legacy Plan"
    assert product.price == 12.5
    assert product.original_price == "¥12.50"
    assert product.stock == 7
    assert product.stock_text == "7"
    assert product.sold == 23
    assert product.buy_url == "https://shop.example/buy/42"
    assert product.tags == ["hot"]
    assert product.category_name == "Subscriptions"


def test_single_card_parser_uses_base_url_when_no_buy_link_exists() -> None:
    card = BeautifulSoup(
        '<div class="product-card"><h4>Fallback Product</h4></div>', "html.parser"
    ).select_one("div")

    product = ClassicScraper()._parse_single_card(
        card, "https://shop.example", "All"
    )

    assert product is not None
    assert product.price is None
    assert product.stock == -1
    assert product.buy_url == "https://shop.example"
