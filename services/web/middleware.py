"""
Plain ASGI middleware for the web app. Not BaseHTTPMiddleware, which would
buffer the live status streams.
"""

import json
from collections.abc import Callable

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "  # the progress bars set their width inline
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'none'"
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",  # job ids are part of page addresses
}
# FastAPI's interactive docs load scripts from a CDN, which the policy would block
DOCS_PATHS = ("/docs", "/redoc")


async def refuse(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    """
    Refuses a request body over limit_for(path), before the multipart parser
    spools it to memory or disk. A declared Content-Length is checked up front;
    the bytes are counted as well, because a chunked body declares nothing.
    """

    def __init__(self, app: ASGIApp, limit_for: Callable[[str], int]) -> None:
        self.app = app
        self.limit_for = limit_for

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["path"])
        declared = Headers(scope=scope).get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            await refuse(send, 413, "The request body is too large.")
            return

        received = 0
        exceeded = False
        started = False

        async def counting_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                return  # the app is answering a body we cut off; the 413 replaces it
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except BodyTooLarge:
            if started:
                raise
        # FastAPI turns a failure while reading a form into a 400, so the app's
        # own answer cannot be trusted here
        if exceeded and not started:
            await refuse(send, 413, "The request body is too large.")


class SecurityMiddleware:
    """
    Adds the browser security headers, and refuses a POST that the browser says
    came from another site. Browsers send Sec-Fetch-Site on every request;
    scripts and curl do not, so API clients are unaffected.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with_csp = not scope["path"].startswith(DOCS_PATHS)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if with_csp:
                    headers.setdefault(
                        "Content-Security-Policy", CONTENT_SECURITY_POLICY
                    )
            await send(message)

        site = Headers(scope=scope).get("sec-fetch-site")
        if scope["method"] == "POST" and site == "cross-site":
            await refuse(send_with_headers, 403, "Cross-site requests are refused.")
            return
        await self.app(scope, receive, send_with_headers)
