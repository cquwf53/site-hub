from __future__ import annotations

import os


def _first(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value
    return default


# SITEHUB aliases are accepted, while the legacy VSHOP names and exact defaults
# remain valid so the transplanted adapters behave like the frozen application.
BROWSER_HEADLESS = _first(
    "SITEHUB_BROWSER_HEADLESS", "VSHOP_BROWSER_HEADLESS", default="true"
).casefold() in {"1", "true", "yes", "on"}
try:
    BROWSER_TIMEOUT = int(_first(
        "SITEHUB_BROWSER_TIMEOUT_MS", "VSHOP_BROWSER_TIMEOUT", default="30000"
    ))
except ValueError:
    BROWSER_TIMEOUT = 30000

try:
    SCRAPE_TIMEOUT_SECONDS = int(_first(
        "SITEHUB_SCRAPE_TIMEOUT_SECONDS", "VSHOP_SCRAPE_TIMEOUT", default="180"
    ))
except ValueError:
    SCRAPE_TIMEOUT_SECONDS = 180
