from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ScrapedProduct:
    name: str
    external_key: str | None = None
    price: float | None = None
    original_price: str | None = None
    stock: int = -1
    stock_text: str | None = None
    sold: int = 0
    buy_url: str | None = None
    category_name: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScrapeResult:
    success: bool
    products: list[ScrapedProduct] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
