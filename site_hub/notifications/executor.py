"""Async notification-outbox executor with secret-safe failure handling."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from site_hub.notifications.models import (
    DeliveryResult,
    EnqueueResult,
    NotificationLease,
    NotificationPolicy,
    WebhookRequest,
    WebhookResponse,
)
from site_hub.notifications.store import NotificationOutboxStore
from site_hub.notifications.transport import HttpxWebhookTransport, WebhookTransport


class SecretReader(Protocol):
    def reveal(self, secret_id: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _DeliveryFailure(Exception):
    error_code: str
    retryable: bool
    response_code: str | None = None


class NotificationOutboxExecutor:
    """Lease and deliver notifications one at a time.

    The object owns no background task.  Lifespan integrations can call
    :meth:`run_forever`; API-triggered drains can call :meth:`run_until_idle`.
    """

    def __init__(
        self,
        store: NotificationOutboxStore,
        secrets: SecretReader,
        *,
        transport: WebhookTransport | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.store = store
        self.secrets = secrets
        self.transport = transport or HttpxWebhookTransport()
        self.policy: NotificationPolicy = store.policy
        self.last_error: str | None = None
        self.worker_id = worker_id or f"notification-{uuid4().hex}"
        if len(self.worker_id) > 150:
            raise ValueError("worker_id cannot exceed 150 characters")
        if self.policy.lease_seconds < self.policy.total_timeout_seconds + 1:
            raise ValueError(
                "lease_seconds must exceed total_timeout_seconds by at least one second"
            )

    async def enqueue_event(self, event_id: str) -> EnqueueResult:
        return await asyncio.to_thread(self.store.enqueue_event, event_id)

    async def run_once(self) -> DeliveryResult | None:
        lease = await asyncio.to_thread(self.store.claim, self.worker_id)
        if lease is None:
            return None

        try:
            response_code = await asyncio.wait_for(
                self._deliver(lease),
                timeout=self.policy.total_timeout_seconds,
            )
        except asyncio.CancelledError:
            # The lease is deliberately left intact for expiry recovery.
            raise
        except TimeoutError:
            return await asyncio.to_thread(
                self.store.mark_failure,
                lease.outbox_id,
                lease.lease_owner,
                error_code="delivery_timeout",
                retryable=True,
            )
        except _DeliveryFailure as failure:
            return await asyncio.to_thread(
                self.store.mark_failure,
                lease.outbox_id,
                lease.lease_owner,
                error_code=failure.error_code,
                retryable=failure.retryable,
                response_code=failure.response_code,
            )
        except Exception:
            # Never persist or return transport exception text: it commonly
            # contains the full request URL, query token, or auth headers.
            return await asyncio.to_thread(
                self.store.mark_failure,
                lease.outbox_id,
                lease.lease_owner,
                error_code="delivery_error",
                retryable=True,
            )

        persisted = await asyncio.to_thread(
            self.store.mark_sent,
            lease.outbox_id,
            lease.lease_owner,
            response_code=response_code,
        )
        return DeliveryResult(
            outbox_id=lease.outbox_id,
            status="sent" if persisted else "stale",
            attempt=lease.attempt,
            response_code=response_code,
        )

    async def run_until_idle(self, *, limit: int = 100) -> tuple[DeliveryResult, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        completed: list[DeliveryResult] = []
        for _ in range(limit):
            result = await self.run_once()
            if result is None:
                break
            completed.append(result)
        return tuple(completed)

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        while not stop_event.is_set():
            try:
                result = await self.run_once()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                # Claim/store failures must not permanently kill notifications.
                # Keep exception text out of public state and logs.
                self.last_error = "notification_store_unavailable"
                result = None
            if result is not None:
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=poll_interval_seconds
                )
            except TimeoutError:
                pass

    async def _deliver(self, lease: NotificationLease) -> str:
        if lease.channel_type.casefold() != "webhook":
            raise _DeliveryFailure("unsupported_channel", retryable=False)

        try:
            endpoint_bytes = await asyncio.to_thread(
                self.secrets.reveal, lease.endpoint_secret_id
            )
            endpoint = endpoint_bytes.decode("utf-8", errors="strict")
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _DeliveryFailure("endpoint_unavailable", retryable=False) from None

        current_url = self._validated_url(endpoint)
        payload = self._payload(lease)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "SiteHub-Notification/1",
            "Idempotency-Key": lease.idempotency_key,
        }
        redirects = 0
        while True:
            request = WebhookRequest(
                url=current_url,
                payload=payload,
                headers=headers,
                timeout_seconds=self.policy.request_timeout_seconds,
            )
            try:
                response = await self.transport.request(request)
            except asyncio.CancelledError:
                raise
            except Exception:
                raise _DeliveryFailure("transport_error", retryable=True) from None

            try:
                status = self._status_code(response)
                await self._consume_response(response)
                if 200 <= status < 300:
                    return str(status)
                if 300 <= status < 400:
                    location = self._header(response, "location")
                    if not location:
                        raise _DeliveryFailure(
                            "redirect_missing", retryable=False, response_code=str(status)
                        )
                    if redirects >= self.policy.max_redirects:
                        raise _DeliveryFailure(
                            "redirect_limit", retryable=False, response_code=str(status)
                        )
                    current_url = self._validated_url(urljoin(current_url, location))
                    redirects += 1
                    continue
                if status in {408, 425, 429} or 500 <= status < 600:
                    raise _DeliveryFailure(
                        "http_server_error", retryable=True, response_code=str(status)
                    )
                raise _DeliveryFailure(
                    "http_client_error", retryable=False, response_code=str(status)
                )
            finally:
                with suppress(Exception):
                    await response.aclose()

    @staticmethod
    def _payload(lease: NotificationLease) -> dict[str, Any]:
        return {
            "event": {
                "id": lease.event_id,
                "type": lease.event_type,
                "severity": lease.severity,
                "entity_type": lease.entity_type,
                "entity_id": lease.entity_id,
                "title": lease.title,
                "detail": lease.detail,
                "payload": lease.payload,
                "occurred_at": lease.occurred_at,
            }
        }

    @staticmethod
    def _validated_url(value: str) -> str:
        try:
            parsed = urlsplit(value)
            if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
                raise ValueError
        except (TypeError, ValueError):
            raise _DeliveryFailure("invalid_endpoint", retryable=False) from None
        return value

    @staticmethod
    def _status_code(response: WebhookResponse) -> int:
        try:
            status = int(response.status_code)
        except (TypeError, ValueError):
            raise _DeliveryFailure("invalid_response", retryable=False) from None
        if status < 100 or status > 599:
            raise _DeliveryFailure("invalid_response", retryable=False)
        return status

    @staticmethod
    def _header(response: WebhookResponse, name: str) -> str | None:
        wanted = name.casefold()
        try:
            for key, value in response.headers.items():
                if str(key).casefold() == wanted:
                    return str(value)
        except Exception:
            raise _DeliveryFailure("invalid_response", retryable=False) from None
        return None

    async def _consume_response(self, response: WebhookResponse) -> None:
        content_length = self._header(response, "content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.policy.response_body_limit:
                    raise _DeliveryFailure("response_too_large", retryable=False)
            except ValueError:
                pass

        body = response.body
        if isinstance(body, (bytes, bytearray, memoryview)):
            if len(body) > self.policy.response_body_limit:
                raise _DeliveryFailure("response_too_large", retryable=False)
            return
        total = 0
        try:
            async for chunk in body:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise _DeliveryFailure("invalid_response", retryable=False)
                total += len(chunk)
                if total > self.policy.response_body_limit:
                    raise _DeliveryFailure("response_too_large", retryable=False)
        except _DeliveryFailure:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _DeliveryFailure("transport_error", retryable=True) from None


__all__ = ["NotificationOutboxExecutor", "SecretReader"]
