"""HTTP catalog: list capabilities, emit tools, invoke one by name."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from cua.catalog import (
    CatalogError,
    bind_arguments,
    card,
    error_payload,
    get_capability,
    invocation_payload,
    list_catalog,
    tool_specs,
)
from cua.log import log_event
from cua.replay import replay
from cua.settings import watch_pace
from cua.surface import Surface, open_browser


def _json_bytes(payload: Any, code: int = 200) -> tuple[int, bytes]:
    body = json.dumps(payload, indent=2).encode("utf-8")
    return code, body


def handle_request(
    method: str,
    path: str,
    body: bytes,
    *,
    target: str,
    headed: bool,
    handoff: str | None,
    handoff_max: int | None,
) -> tuple[int, bytes]:
    parsed = urlparse(path)
    route = parsed.path.rstrip("/") or "/"
    if method == "GET" and route in {"/health", "/v1/health"}:
        return _json_bytes({"ok": True})
    if method == "GET" and route == "/v1/capabilities":
        return _json_bytes({"capabilities": list_catalog()})
    if method == "GET" and route.startswith("/v1/capabilities/"):
        name = route.split("/v1/capabilities/", 1)[1]
        try:
            capability, artifact = get_capability(name)
        except CatalogError as exc:
            return _json_bytes(error_payload(exc), 404)
        return _json_bytes(card(capability, artifact))
    if method == "GET" and route == "/v1/tools":
        return _json_bytes({"tools": tool_specs()})
    if method == "POST" and route == "/v1/invoke":
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return _json_bytes(
                {"error": "body must be JSON", "code": "invalid_call"}, 400
            )
        if not isinstance(payload, dict):
            return _json_bytes(
                {"error": "body must be an object", "code": "invalid_call"}, 400
            )
        name = payload.get("name") or payload.get("capability")
        arguments = payload.get("arguments")
        if arguments is None:
            arguments = payload.get("inputs") or {}
        try:
            capability, artifact = get_capability(str(name or ""))
            bound = bind_arguments(capability, arguments)
        except CatalogError as exc:
            status = 404 if exc.code == "unknown_capability" else 400
            return _json_bytes(error_payload(exc), status)
        log_event("catalog.invoke", capability_id=capability.id, via="http")
        envelope = _run_invoke(
            capability,
            artifact,
            str(name),
            arguments if isinstance(arguments, dict) else {},
            bound,
            target=target,
            headed=headed,
            handoff=handoff,
            handoff_max=handoff_max,
        )
        result = envelope["result"]
        code = 200 if result["status"] in {"success", "business_outcome"} else 500
        return _json_bytes(envelope, code)
    return _json_bytes({"error": "not found", "code": "not_found"}, 404)


def _run_invoke(
    capability,
    artifact,
    call_name: str,
    arguments: dict[str, Any],
    bound: dict[str, str],
    *,
    target: str,
    headed: bool,
    handoff: str | None,
    handoff_max: int | None,
) -> dict[str, Any]:
    highlight_ms, dwell_ms = watch_pace(headed)
    playwright, browser, page, context = open_browser(headed=headed)
    try:
        surface = Surface(
            page,
            allowed_origin=target,
            highlight_ms=highlight_ms,
            dwell_ms=dwell_ms,
        )
        result = replay(
            capability,
            target,
            bound,
            surface,
            handoff_mode=handoff,
            handoff_max=handoff_max,
        )
        return invocation_payload(
            capability, artifact, call_name, arguments, bound, result
        )
    finally:
        if page is not None:
            page.close()
        if context is not None:
            context.close()
        if browser:
            browser.close()
        if playwright:
            playwright.stop()


def serve(
    host: str,
    port: int,
    *,
    target: str,
    headed: bool,
    handoff: str | None,
    handoff_max: int | None,
) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _write(self, code: int, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            code, body = handle_request(
                method,
                self.path,
                raw,
                target=target,
                headed=headed,
                handoff=handoff,
                handoff_max=handoff_max,
            )
            self._write(code, body)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            log_event("catalog.http", message=fmt % args)

    httpd = HTTPServer((host, port), Handler)
    log_event("catalog.serve", host=host, port=port, target=target)
    print(f"Capability catalog on http://{host}:{port}/v1/tools", flush=True)
    httpd.serve_forever()
