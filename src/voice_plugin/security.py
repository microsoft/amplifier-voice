"""Server-owned HTTP origin policy, independent of proxy/daemon middleware."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

_AUTHORITY = re.compile(r"(?:\[[0-9a-fA-F:]+\]|[a-zA-Z0-9.-]+)(?::[0-9]+)?\Z")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def parse_origin(value: str) -> tuple[str, str, int]:
    """Parse only HTTP(S) origins, not general URLs or lists of origins.

    Normalize host case, IPv6 spelling and default ports. Reject userinfo,
    paths (including '/'), opaque origins, escapes, whitespace and empty ports
    instead of relying on urlsplit's permissive URL handling.
    """
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("Origin must be an ASCII HTTP(S) origin")
    if not value.startswith(("http://", "https://")):
        raise ValueError("Origin must use http:// or https://")
    scheme, authority = value.split("://", 1)
    if not _AUTHORITY.fullmatch(authority):
        raise ValueError("Origin must contain only a host and optional port")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if ":" in host:
        host = ipaddress.IPv6Address(host).compressed
    elif len(host) > 253 or not all(_LABEL.fullmatch(p) for p in host.split(".")):
        raise ValueError("Invalid origin hostname")
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise ValueError("Invalid origin port")
    return scheme, host, port


def canonical_origin(value: str) -> str:
    scheme, host, port = parse_origin(value)
    authority = f"[{host}]" if ":" in host else host
    if port != (443 if scheme == "https" else 80):
        authority += f":{port}"
    return f"{scheme}://{authority}"


def single_header(request: Request, name: str) -> str | None:
    """Do not let downstream parsers choose between duplicate security headers."""
    values = request.headers.getlist(name)
    if len(values) > 1 or any(
        not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value)
        for value in values
    ):
        raise HTTPException(status_code=400, detail=f"Invalid {name} header")
    return values[0] if values else None


def origin_guard(public_origin: str) -> Callable[[Request], Awaitable[None]]:
    """Snapshot the configured public origin when a router is built.

    The transport may be HTTP behind an HTTPS proxy. Neither the ASGI scheme
    nor Forwarded/X-Forwarded-* chooses the trusted public origin. The proxy
    must preserve the public Host and enforce authentication itself.
    """
    expected = parse_origin(public_origin)

    async def guard(request: Request) -> None:
        host = single_header(request, "host")
        origin = single_header(request, "origin")
        try:
            if host is None or parse_origin(f"{expected[0]}://{host}") != expected:
                raise ValueError("Untrusted host")
            if origin is not None and parse_origin(origin) != expected:
                raise ValueError("Untrusted origin")
        except ValueError:
            raise HTTPException(
                status_code=403, detail="Untrusted voice Host or Origin"
            ) from None

    return guard