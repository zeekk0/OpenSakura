"""Only Sakura's own page may use this server: the guard against other web sites open in the same browser.

The API has no login. It trusts whoever can reach the port, which with the default server.host (127.0.0.1) is only
this PC. But a web page open in any browser tab runs on this PC too, so every HTTP request and every WebSocket
handshake must pass two checks before a route sees it:

- Host, the name the browser used for this server's address, must be a local name (localhost, 127.0.0.1, ::1), an
  IP address, server.host, or a name listed in server.allowed_hosts. A web site that points its own domain at
  127.0.0.1 (DNS rebinding) still sends its domain in Host. Rebinding needs a domain name, so an IP address in Host
  is always the user's own choice (this PC's LAN address, for example).
- Origin, which browsers send with every POST, every fetch to another origin and every WebSocket handshake, must be
  this server's own address: the same host and port as Host. A request without Origin comes from a program (the
  launcher's checks, curl, a bot) or is a plain page load, and passes.

A refused request gets 403 and a refused WebSocket is closed before it is accepted (the browser sees a failed
handshake), so no route runs. The server sends no CORS headers, so no other origin can read an answer either.
"""
from __future__ import annotations

import ipaddress
import json
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit

LOCAL_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def host_name(authority: Optional[str]) -> str:
    """'LocalHost:8010' -> 'localhost', '[::1]:8010' -> '::1'; '' when there is none or it is not a plain host[:port]."""
    a = str(authority or "").strip()
    if not a or any(c in a for c in "@/\\?# \t"):
        return ""
    try:
        return (urlsplit("//" + a).hostname or "").lower()
    except ValueError:
        return ""


def is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    return True


def names(*groups: Any) -> set[str]:
    """server.host and server.allowed_hosts as bare lower-case names. Each group is one name or a list of names."""
    out: set[str] = set()
    for g in groups:
        for x in ([g] if isinstance(g, str) else g if isinstance(g, (list, tuple, set, frozenset)) else []):
            n = host_name(str(x))
            if n:
                out.add(n)
    return out


def host_allowed(host: Optional[str], extra: Iterable[str] = ()) -> bool:
    """Is Host one of this server's names? A request without Host (HTTP/1.0) does not come from a browser."""
    if host is None:
        return True
    name = host_name(host)
    return bool(name) and (name in LOCAL_NAMES or is_ip(name) or name in set(extra))


def origin_allowed(origin: Optional[str], host: Optional[str]) -> bool:
    """Does the request come from this server's own page? Without Origin: a program or a page load, allowed.
    "null" (file:// pages, sandboxed frames), other schemes and any other host or port: refused."""
    if origin is None:
        return True
    try:
        o = urlsplit(origin.strip())
    except ValueError:
        return False
    return (o.scheme in ("http", "https") and bool(o.netloc) and bool(host)
            and o.netloc.lower() == str(host).strip().lower())


def refusal(host: Optional[str], origin: Optional[str], extra: Iterable[str] = ()) -> Optional[str]:
    """Why this request is refused, or None when it may go on."""
    if not host_allowed(host, extra):
        return (f"OpenSakura does not answer to the name {host_name(host) or host!r}. Open the page at "
                "http://localhost:<port>, or add the name to server.allowed_hosts in config/settings.json.")
    if not origin_allowed(origin, host):
        return "Refused: a request from another web site. Only OpenSakura's own page may use this server."
    return None


class OriginGuard:
    """ASGI middleware: refusal() for every HTTP request and WebSocket handshake, before any route runs.
    extra_hosts() is asked on every request, so a change to server.allowed_hosts needs no restart."""

    def __init__(self, app: Any, extra_hosts: Callable[[], Iterable[Any]] = tuple) -> None:
        self.app = app
        self.extra_hosts = extra_hosts

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] in ("http", "websocket"):
            headers: dict[str, str] = {}
            for k, v in scope.get("headers") or ():
                headers.setdefault(k.decode("latin-1").lower(), v.decode("latin-1"))
            try:
                extra = names(*self.extra_hosts())
            except Exception:  # noqa: BLE001 — a broken setting must not open the door
                extra = set()
            why = refusal(headers.get("host"), headers.get("origin"), extra)
            if why:
                if scope["type"] == "websocket":
                    await receive()   # websocket.connect; closing before accept fails the handshake (HTTP 403)
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = json.dumps({"detail": why}).encode("utf-8")
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode()),
                                        (b"x-content-type-options", b"nosniff")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
