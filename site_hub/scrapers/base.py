from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, FeatureNotFound

from site_hub.scrapers.models import ScrapedProduct, ScrapeResult


PRICE = re.compile(r"(?:¥|￥|RMB|CNY|USD|\$)?\s*(\d+(?:\.\d{1,4})?)", re.I)
STOCK = re.compile(r"(?:库存|stock)\s*[:：]?\s*(\d+)", re.I)
SOLD = re.compile(r"(?:已售|销量|sold)\s*[:：]?\s*(\d+)", re.I)


class BaseScraper(ABC):
    scraper_type = "base"

    @abstractmethod
    async def scrape(self, site_url: str, config: dict[str, Any] | None = None) -> ScrapeResult: ...

    # Exact legacy timer behavior (wall clock, two decimal places).  The short
    # aliases retain compatibility with the initial Site Hub scaffold.
    def _timer_start(self) -> float:
        return time.time()

    def _timer_elapsed(self, start_time: float) -> float:
        return round(time.time() - start_time, 2)

    def started(self) -> float:
        return self._timer_start()

    def elapsed(self, start: float) -> float:
        return self._timer_elapsed(start)


def parse_catalog_html(html: str, base_url: str) -> list[ScrapedProduct]:
    """Conservative parser shared by server-rendered and browser adapters."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        soup = BeautifulSoup(html, "html.parser")
    selectors = (
        "[data-product-id]", ".product-card", ".goods-item", ".product-item",
        ".commodity-item", ".card.product", "table tbody tr", ".commodity-list tr",
    )
    cards = []
    for selector in selectors:
        found = soup.select(selector)
        if found:
            cards = found
            if len(found) > 1: break
    products: list[ScrapedProduct] = []
    seen: set[str] = set()
    for card in cards[:5000]:
        text = " ".join(card.stripped_strings)
        if not text or len(text) > 5000: continue
        name_el = card.select_one("[data-name],.goods-title,.product-title,.name,.title,h2,h3,h4,h5,strong")
        name = (name_el.get("data-name") if name_el and name_el.has_attr("data-name") else name_el.get_text(" ", strip=True) if name_el else "")
        if not name and card.name == "tr":
            cells = card.select("td")
            name = next((cell.get_text(" ", strip=True) for cell in cells if len(cell.get_text(" ", strip=True)) > 2), "")
        name = name.strip()[:500]
        if len(name) < 2: continue
        price_el = card.select_one("[data-price],.price,.goods-price,.product-price")
        price_text = price_el.get("data-price") if price_el and price_el.has_attr("data-price") else price_el.get_text(" ", strip=True) if price_el else text
        price_match = PRICE.search(str(price_text))
        stock_match, sold_match = STOCK.search(text), SOLD.search(text)
        link = card.select_one("a[href]")
        buy_url = urljoin(base_url, link.get("href")) if link else base_url
        external = card.get("data-product-id") or card.get("data-id")
        key = str(external or name).casefold()
        if key in seen: continue
        seen.add(key)
        products.append(ScrapedProduct(
            name=name, external_key=str(external) if external else None,
            price=float(price_match.group(1)) if price_match else None,
            stock=int(stock_match.group(1)) if stock_match else -1,
            stock_text=stock_match.group(1) if stock_match else None,
            sold=int(sold_match.group(1)) if sold_match else 0,
            buy_url=buy_url,
            category_name=(card.get("data-category") or "全部")[:200],
        ))
    return products
