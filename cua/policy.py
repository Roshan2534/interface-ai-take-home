from __future__ import annotations

from urllib.parse import urlparse

from cua.log import LOGGER, log_event
from cua.settings import allowed_path_prefixes, denied_path_prefixes


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


def _path(url: str) -> str:
    return urlparse(url).path or "/"


def _prefix_match(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/") or "/"
    if prefix == "/":
        return path == "/"
    return path == prefix or path.startswith(prefix + "/")


def path_denied_reason(url: str) -> str | None:
    """None if this URL's path may be driven. Skip non-http (about:blank frames)."""
    if not url.startswith("http"):
        return None
    path = _path(url)
    for denied in denied_path_prefixes():
        if _prefix_match(path, denied):
            return f"path {path} is denied ({denied})"
    if path == "/":
        return None
    for allowed in allowed_path_prefixes():
        if _prefix_match(path, allowed):
            return None
    return f"path {path} is not on the route allowlist"


def assert_origin(url: str, allowed_origin: str) -> None:
    if not url.startswith("http"):
        return
    if origin(url) != origin(allowed_origin):
        log_event("policy.denied_origin", url=url, allowlist=origin(allowed_origin))
        raise PolicyDenied(
            f"Refusing to act on {url!r}; origin allowlist is {origin(allowed_origin)}"
        )
    LOGGER.debug("policy.origin_ok url=%s", url)


def assert_path(url: str) -> None:
    reason = path_denied_reason(url)
    if reason:
        log_event(
            "policy.denied_path",
            url=url,
            reason=reason,
            allowed=allowed_path_prefixes(),
            denied=denied_path_prefixes(),
        )
        raise PolicyDenied(f"Refusing to act on {url!r}; {reason}")
    if url.startswith("http"):
        LOGGER.debug("policy.path_ok path=%s", _path(url))


def assert_allowed(url: str, allowed_origin: str, *, check_path: bool = True) -> None:
    """Origin always; path when driving the UI (goto/act), not when only observing."""
    assert_origin(url, allowed_origin)
    if check_path:
        assert_path(url)


def assert_page_for_act(page, allowed_origin: str) -> None:
    """Block if the top page or any frame is off-origin or on a forbidden route."""
    assert_origin(page.url, allowed_origin)
    assert_path(page.url)
    for frame in page.frames:
        if not frame.url.startswith("http"):
            continue
        assert_origin(frame.url, allowed_origin)
        assert_path(frame.url)
        LOGGER.debug("policy.frame_ok url=%s", frame.url)


def assert_action(action: str) -> None:
    if action not in ALLOWED_ACTIONS:
        log_event("policy.unknown_action", action=action, allowed=sorted(ALLOWED_ACTIONS))
        raise PolicyDenied(f"Action {action!r} is not in the allowlist")
    LOGGER.debug("policy.action_ok action=%s", action)
