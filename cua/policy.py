from __future__ import annotations

from urllib.parse import urlparse


from cua.log import LOGGER, log_event


ALLOWED_ACTIONS = frozenset({"click", "fill", "select", "press", "wait"})


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
        log_event("policy.denied", url=url, allowlist=origin(allowed_origin))
        raise PolicyDenied(
            f"Refusing to act on {url!r}; allowlist is {origin(allowed_origin)}"
        )
    LOGGER.debug("policy.allow url=%s", url)


def assert_action(action: str) -> None:
    if action not in ALLOWED_ACTIONS:
        log_event("policy.unknown_action", action=action, allowed=sorted(ALLOWED_ACTIONS))
        raise PolicyDenied(f"Action {action!r} is not in the allowlist")
    LOGGER.debug("policy.action_ok action=%s", action)
