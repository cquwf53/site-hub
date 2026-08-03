"""Public, secret-safe types for notification delivery."""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class NotificationPolicy:
    """Bounded delivery policy.

    ``max_retries`` counts retries after the first delivery attempt.  Backoff is
    deterministic exponential delay, capped by ``backoff_max_seconds``.
    """

    lease_seconds: float = 60.0
    max_retries: int = 3
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 300.0
    total_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 10.0
    max_redirects: int = 3
    response_body_limit: int = 64 * 1024

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.backoff_base_seconds < 0:
            raise ValueError("backoff_base_seconds cannot be negative")
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds cannot be below the base delay")
        if self.total_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ValueError("delivery timeouts must be positive")
        if self.max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if self.response_body_limit < 0:
            raise ValueError("response_body_limit cannot be negative")

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1

    def retry_delay(self, attempt: int) -> float:
        exponent = max(0, min(attempt - 1, 30))
        return min(
            self.backoff_max_seconds,
            self.backoff_base_seconds * (2**exponent),
        )


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    event_id: str
    created: int
    existing: int
    outbox_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    retried: int = 0
    dead: int = 0
    cancelled: int = 0


@dataclass(frozen=True, slots=True)
class NotificationLease:
    outbox_id: str
    event_id: str
    channel_id: str
    channel_type: str
    idempotency_key: str
    attempt: int
    lease_owner: str = field(repr=False)
    endpoint_secret_id: str = field(repr=False)
    event_type: str
    severity: str
    entity_type: str
    entity_id: str | None
    title: str = field(repr=False)
    detail: str = field(repr=False)
    payload: Any = field(repr=False)
    occurred_at: str


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    outbox_id: str
    status: str
    attempt: int
    response_code: str | None = None
    error_code: str | None = None
    retry_at: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookRequest:
    """Request handed to a transport; repr deliberately hides sensitive data."""

    url: str = field(repr=False)
    payload: Mapping[str, Any] = field(repr=False)
    headers: Mapping[str, str]
    timeout_seconds: float


@dataclass(slots=True)
class WebhookResponse:
    """Streaming response returned by an injectable transport."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body: bytes | AsyncIterable[bytes] = field(default=b"", repr=False)
    close: Callable[[], Awaitable[None]] | None = field(
        default=None, repr=False, compare=False
    )

    async def aclose(self) -> None:
        if self.close is not None:
            await self.close()
