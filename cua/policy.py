from __future__ import annotations

from urllib.parse import urlparse


class PolicyDenied(RuntimeError):
    pass


def origin(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    scheme = parsed.scheme or "http"
    if port:
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def assert_allowed(url: str, allowed_origin: str) -> None:
    if origin(url) != origin(allowed_origin):
        raise PolicyDenied(
            f"Refusing to act on {url!r}; allowlist is {origin(allowed_origin)}"
        )
