from __future__ import annotations

import asyncio

from site_hub.scrapers.custom_spa import CustomSpaScraper


class _EvaluatePage:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.script = ""

    async def evaluate(self, script: str):
        self.script = script
        return self.payload


def test_custom_spa_post_processing_rejects_price_names_and_normalizes_urls() -> None:
    page = _EvaluatePage(
        {
            "categories": ["Cloud", "AI"],
            "products": [
                {
                    "name": "Premium Account",
                    "price": 19.5,
                    "stock": 8,
                    "sold": 31,
                    "buyUrl": "/relative-buy-link",
                    "category": "Cloud",
                },
                {
                    "name": "¥30.00",
                    "price": 30,
                    "stock": 4,
                    "sold": 2,
                    "buyUrl": "https://shop.example/bad",
                    "category": "AI",
                },
                {"name": "x"},
            ],
        }
    )

    async def invoke():
        return await CustomSpaScraper()._extract_products(
            page, "https://shop.example/store#cloud"
        )

    products, categories = asyncio.run(invoke())

    assert categories == ["Cloud", "AI"]
    assert len(products) == 1
    assert products[0].name == "Premium Account"
    assert products[0].price == 19.5
    assert products[0].original_price == "¥19.5"
    assert products[0].stock == 8
    assert products[0].stock_text == "8"
    assert products[0].sold == 31
    assert products[0].buy_url == "https://shop.example/store"
    assert products[0].category_name == "Cloud"
    assert "querySelectorAll" in page.script
