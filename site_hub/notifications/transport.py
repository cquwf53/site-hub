"""Webhook transport abstraction and bounded default HTTP implementation."""

from __future__ import annotations

from typing import Protocol

from site_hub.notifications.models import WebhookRequest, WebhookResponse


class WebhookTransport(Protocol):
    async def request(self, request: WebhookRequest) -> WebhookResponse: ...


class HttpxWebhookTransport:
    """One-request clients keep ownership and shutdown rules unambiguous.

    Redirects are intentionally disabled here.  The executor validates and
    follows each redirect itself so the same scheme and redirect limits apply
    to both the production transport and injected test transports.
    """

    async def request(self, request: WebhookRequest) -> WebhookResponse:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError("HTTP transport is unavailable") from exc

        client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=request.timeout_seconds,
        )
        try:
            outbound = client.build_request(
                "POST",
                request.url,
                json=dict(request.payload),
                headers=dict(request.headers),
            )
            response = await client.send(outbound, stream=True)
        except BaseException:
            await client.aclose()
            raise

        async def close() -> None:
            try:
                await response.aclose()
            finally:
                await client.aclose()

        return WebhookResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            body=response.aiter_bytes(),
            close=close,
        )


__all__ = ["HttpxWebhookTransport", "WebhookTransport"]
