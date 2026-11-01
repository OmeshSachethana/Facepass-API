import json
import logging
import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from facepass.core.logging import request_id_var

logger = logging.getLogger("facepass.access")

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_BODYLESS_METHODS = {"GET", "HEAD", "OPTIONS", "DELETE"}


class RequestContextMiddleware:
    """Assign a request id, set response headers and write one access log line per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = Headers(scope=scope).get("x-request-id", "")
        request_id = supplied if _SAFE_REQUEST_ID.match(supplied) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        response_status = 500

        async def send_with_headers(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            logger.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": response_status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            request_id_var.reset(token)


class BodySizeLimitMiddleware:
    """Reject oversized requests before reading them.

    The server enforces Content-Length, so a declared length can't be exceeded. Requests
    that don't declare one are refused because their size can't be known up front.
    """

    def __init__(self, app: ASGIApp, max_upload_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_upload_bytes + _MULTIPART_OVERHEAD_BYTES

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in _BODYLESS_METHODS:
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is None or not declared.isdigit():
            await self._reject(send, 411, "length_required", "Content-Length required")
        elif int(declared) > self.max_body_bytes:
            await self._reject(send, 413, "image_too_large", "Request body too large")
        else:
            await self.app(scope, receive, send)

    @staticmethod
    async def _reject(send: Send, status: int, code: str, title: str) -> None:
        body = json.dumps(
            {
                "type": f"urn:facepass:problem:{code}",
                "title": title,
                "status": status,
                "code": code,
                "request_id": request_id_var.get(),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/problem+json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
