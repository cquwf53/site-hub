from __future__ import annotations

import secrets
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from site_hub.config import Settings


def error_response(status: int, code: str, message: str, *, request_id: str | None = None,
                   field: str | None = None, context: dict | None = None) -> JSONResponse:
    detail: dict[str, object] = {"code": code, "message": message}
    if field:
        detail["field"] = field
    if context:
        detail["context"] = context
    body: dict[str, object] = {"error": detail}
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status)


class LocalRequestSecurityMiddleware(BaseHTTPMiddleware):
    """DNS-rebinding, cross-origin and bounded-body protection for localhost."""

    def __init__(self, app, *, settings: Settings, max_body_bytes: int = 2 * 1024 * 1024) -> None:
        super().__init__(app)
        self.settings = settings
        self.max_body_bytes = max_body_bytes

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id", "")[:100] or str(uuid4())
        request.state.request_id = request_id
        host = request.headers.get("host", "").strip().lower().rstrip(".")
        if not host or host not in self.settings.allowed_hosts:
            return error_response(400, "INVALID_HOST", "Host header is not allowed", request_id=request_id)

        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > self.max_body_bytes:
                    return error_response(413, "BODY_TOO_LARGE", "Request body is too large", request_id=request_id)
            except ValueError:
                return error_response(400, "INVALID_CONTENT_LENGTH", "Invalid Content-Length", request_id=request_id)

        # Content-Length is only an early rejection. Buffering the bounded local
        # request also enforces the limit for HTTP/1.1 chunked bodies and HTTP/2.
        # Starlette's cached request replays these bytes to the downstream app.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            body = await request.body()
            if len(body) > self.max_body_bytes:
                return error_response(
                    413, "BODY_TOO_LARGE", "Request body is too large", request_id=request_id
                )

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site", "").lower()
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme not in {"http", "https"} or parsed.netloc.lower().rstrip(".") != host:
                    return error_response(403, "CROSS_ORIGIN", "Cross-origin mutation rejected", request_id=request_id)
            elif fetch_site == "cross-site":
                return error_response(403, "CROSS_ORIGIN", "Cross-site mutation rejected", request_id=request_id)
            supplied = request.headers.get("x-csrf-token") or request.headers.get("x-site-hub-csrf") or ""
            if not secrets.compare_digest(supplied, self.settings.csrf_token):
                return error_response(403, "CSRF_FAILED", "CSRF token is missing or invalid", request_id=request_id)

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
