"""Bearer-token auth for everything the server serves over HTTP.

One pre-shared token (``CONDUCTOR_DESKTOP_AUTH_TOKEN`` or ``_AUTH_TOKEN_FILE``)
guards the MCP transport and the dashboard alike. MCP clients send
``Authorization: Bearer <token>``. A browser can't add that header to a page
load, so the dashboard also takes ``/dashboard?token=<token>`` once, answers
with a cookie and redirects to the bare URL. The cookie carries a value
derived from the token, not the token itself, and only works under
``/dashboard``: a leaked cookie reads the dashboard but can't drive the
desktop.

Plain HTTP: the token crosses the network in the clear. It keeps out
other hosts on a LAN you trust and DNS-rebinding pages (they can't know
the token, and the cookie belongs to the server's own hostname); for
anything else, keep the bind on loopback and tunnel.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qsl, urlencode

COOKIE = "conductor_desktop_dashboard"
_DASHBOARD = "/dashboard"
#: Shorter tokens are refused at startup; `secrets.token_urlsafe(32)` is 43.
MIN_TOKEN_LEN = 16


def is_loopback(host: str) -> bool:
    """True for a bind address only this machine can reach."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def cookie_value(token: str) -> str:
    return hmac.new(token.encode(), b"dashboard-cookie", hashlib.sha256).hexdigest()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _in_dashboard(path: str) -> bool:
    return path == _DASHBOARD or path.startswith(_DASHBOARD + "/")


class BearerAuth:
    """ASGI middleware: refuse any HTTP request without the token."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token
        self.cookie = cookie_value(token)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":   # lifespan
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        path = scope.get("path", "")

        scheme, _, given = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and _same(given.strip(), self.token):
            await self.app(scope, receive, send)
            return

        if _in_dashboard(path):
            jar = SimpleCookie()
            try:
                jar.load(headers.get("cookie", ""))
            except CookieError:
                jar = SimpleCookie()
            if COOKIE in jar and _same(jar[COOKIE].value, self.cookie):
                await self.app(scope, receive, send)
                return
            query = parse_qsl(scope.get("query_string", b"").decode("latin-1"))
            given = next((v for k, v in query if k == "token"), None)
            if given is not None and _same(given, self.token):
                rest = urlencode([(k, v) for k, v in query if k != "token"])
                await _send(send, 303, [
                    (b"location", (path + ("?" + rest if rest else "")).encode()),
                    (b"set-cookie", (f"{COOKIE}={self.cookie}; Path={_DASHBOARD}; "
                                     "HttpOnly; SameSite=Strict").encode()),
                ])
                return

        await _send(send, 401, [(b"www-authenticate", b'Bearer realm="conductor-desktop"')],
                    b"unauthorized: send Authorization: Bearer <CONDUCTOR_DESKTOP_AUTH_TOKEN>\n")


async def _send(send, status: int, headers: list[tuple[bytes, bytes]],
                body: bytes = b"") -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()), *headers]})
    await send({"type": "http.response.body", "body": body})
