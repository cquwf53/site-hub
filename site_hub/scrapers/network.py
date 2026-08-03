"""Shared network safety limits and log redaction for scraper adapters."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


API_CONNECT_TIMEOUT_SECONDS = 10.0
API_READ_TIMEOUT_SECONDS = 15.0
API_TOTAL_TIMEOUT_SECONDS = 30.0
API_MAX_REDIRECTS = 5
API_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
API_MAX_CAPTURED_RESPONSES = 16

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)"
    r"\s*([:=])\s*([^\r\n,]+)"
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)([\"']?\b(?:password|access[_-]?token|refresh[_-]?token|token|secret|"
    r"api[_-]?key|webhook(?:[_-]?(?:url|endpoint))?|endpoint)[\"']?\s*[:=]\s*)"
    r"([\"']?[^\s,;}]+)"
)


def redact_url(value: object) -> str:
    """Remove credentials, query parameters, and fragments from a URL."""

    raw = str(value)
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return "<redacted-url>"
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{host}:{port}" if port is not None else host
        query = "redacted" if parsed.query else ""
        fragment = "redacted" if parsed.fragment else ""
        return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, query, fragment))
    except (TypeError, ValueError):
        return "<redacted-url>"


def sanitize_log_value(value: object, *, max_length: int = 1000) -> str:
    """Make exception/result text safe for logs and persisted job errors."""

    text = str(value)
    text = _HEADER_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    text = _SENSITIVE_VALUE_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    text = _URL_RE.sub(lambda match: redact_url(match.group(0)), text)
    if len(text) > max_length:
        text = text[: max_length - 3] + "..."
    return text
