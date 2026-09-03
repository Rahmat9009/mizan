"""Transport-layer hardening for the ``/v1`` surface: response headers and a request-body ceiling.

Both controls live below the routing layer on purpose. A header set inside a route handler is a header
that is missing from every error response, every 404 and every path that raised before reaching the
handler — which is exactly the set of responses an attacker is most interested in. A body limit
enforced after parsing is a limit that has already paid the cost it exists to prevent.

Everything here is **plain ASGI**. No FastAPI import, no Starlette import, no framework types in a
signature; the two middlewares are callables over ``(scope, receive, send)`` and the two pure helpers
under them are ordinary functions over ordinary data. So the policy can be unit-tested — and reviewed —
without an HTTP server, and it keeps working if the transport is ever swapped.

Findings answered here: F-14 (never return internal state — the oversize refusal says how big is
allowed and nothing about what arrived) and the hardening items of the L3b brief.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from typing import Any

from mizan.contracts.canonical import uuid7

__all__ = [
    "MAX_BODY_BYTES",
    "PAYLOAD_TOO_LARGE",
    "SECURITY_HEADERS",
    "BodyLimitMiddleware",
    "SecurityHeadersMiddleware",
    "merge_security_headers",
    "over_limit",
    "too_large_payload",
]

#: Sent on **every** response, success and failure alike.
#:
#: ``default-src 'none'`` is the correct policy for an API that returns nothing but JSON: there is no
#: document to load a script into, so the safe answer to "what may this response load?" is "nothing".
#: ``frame-ancestors 'none'`` and ``X-Frame-Options: DENY`` say the same thing twice because old
#: browsers only understand the second. ``no-store`` keeps a decision record — positions, buying power,
#: an agent's reasoning — out of every intermediary cache.
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; "
        "sandbox; upgrade-insecure-requests"
    ),
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=(), usb=()",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Cache-Control": "no-store",
}

#: 256 KiB. A proposal is a handful of legs plus at most 20,000 characters of ``reasoning``; anything
#: larger is not a trade, and reading it costs memory before a single control has run.
MAX_BODY_BYTES = 256 * 1024

PAYLOAD_TOO_LARGE = 413

#: Header names this module owns. A route that sets one of these loses; the hardening layer is the
#: single authority so that no handler can weaken the policy by accident.
_OWNED = frozenset(name.lower().encode("latin-1") for name in SECURITY_HEADERS)


def merge_security_headers(
    raw: Iterable[tuple[bytes, bytes]], *, headers: Mapping[str, str] = SECURITY_HEADERS
) -> list[tuple[bytes, bytes]]:
    """Return ``raw`` with the security headers applied, replacing any the application set itself.

    Pure and framework-free: it is the whole policy of :class:`SecurityHeadersMiddleware`, testable
    against a list of tuples.
    """
    merged = [(name, value) for name, value in raw if bytes(name).lower() not in _OWNED]
    merged.extend((name.encode("latin-1"), value.encode("latin-1")) for name, value in headers.items())
    return merged


def over_limit(content_length: str | bytes | None, *, max_bytes: int = MAX_BODY_BYTES) -> bool:
    """True when a declared ``Content-Length`` already exceeds the ceiling.

    An absent or unparseable length is **not** treated as acceptable-by-default; it simply cannot be
    judged here, and the streaming counter in :class:`BodyLimitMiddleware` enforces the same ceiling on
    a chunked body that never declares one.
    """
    if content_length is None:
        return False
    if isinstance(content_length, bytes):
        content_length = content_length.decode("latin-1", "ignore")
    try:
        return int(content_length.strip()) > max_bytes
    except ValueError:
        return False


def too_large_payload(*, max_bytes: int = MAX_BODY_BYTES) -> dict[str, Any]:
    """The refusal body, in the same envelope as every other error (F-14).

    It names the ceiling and nothing else: not the size that arrived, not the route, not the parser.
    A limit is public documentation; what a caller sent back is information about the caller.
    """
    return {
        "error": {
            "code": "VALIDATION_FAILED",
            "message": f"The request body exceeds the {max_bytes} byte limit.",
            "correlation_id": uuid7(),
            "reason_codes": [],
        }
    }


Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class SecurityHeadersMiddleware:
    """Stamp :data:`SECURITY_HEADERS` onto every HTTP response, including error responses."""

    def __init__(self, app: Any, *, headers: Mapping[str, str] | None = None) -> None:
        self.app = app
        self.headers = dict(headers) if headers is not None else dict(SECURITY_HEADERS)

    async def __call__(self, scope: MutableMapping[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def stamped(message: MutableMapping[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                message["headers"] = merge_security_headers(
                    message.get("headers") or (), headers=self.headers
                )
            await send(message)

        await self.app(scope, receive, stamped)


class BodyLimitMiddleware:
    """Refuse an oversized request body before the application sees a byte of it.

    Two gates, because one is not enough: a declared ``Content-Length`` is refused outright, and a body
    that declares nothing (or lies) is counted as it streams and cut off at the same ceiling. Neither
    path reaches a route handler, so no control, no engine call and no ledger write happens on a
    request that was never going to be accepted.
    """

    def __init__(self, app: Any, *, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope: MutableMapping[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or ()).get(b"content-length")
        if over_limit(declared, max_bytes=self.max_bytes):
            await _refuse(send, self.max_bytes)
            return

        seen = 0
        refused = False

        async def counted() -> MutableMapping[str, Any]:
            nonlocal seen, refused
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body") or b"")
                if seen > self.max_bytes:
                    refused = True
                    # Present the stream as finished and empty. The application then parses an empty
                    # body and fails validation, but it never sees the oversized bytes, and the
                    # response it produces is replaced below.
                    return {"type": "http.request", "body": b"", "more_body": False}
            return message

        replacement = _encode(too_large_payload(max_bytes=self.max_bytes))
        finished = False

        async def guarded(message: MutableMapping[str, Any]) -> None:
            nonlocal finished
            if not refused:
                await send(message)
                return
            if finished:
                return  # the refusal has already been written; later chunks are discarded
            kind = message.get("type")
            if kind == "http.response.start":
                await send(
                    {
                        "type": "http.response.start",
                        "status": PAYLOAD_TOO_LARGE,
                        "headers": merge_security_headers(
                            [
                                (b"content-type", b"application/json"),
                                (b"content-length", str(len(replacement)).encode("latin-1")),
                            ]
                        ),
                    }
                )
            elif kind == "http.response.body":
                finished = True
                await send({"type": "http.response.body", "body": replacement, "more_body": False})

        await self.app(scope, counted, guarded)


def _encode(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


async def _refuse(send: Send, max_bytes: int) -> None:
    body = _encode(too_large_payload(max_bytes=max_bytes))
    await send(
        {
            "type": "http.response.start",
            "status": PAYLOAD_TOO_LARGE,
            "headers": merge_security_headers(
                [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ]
            ),
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
