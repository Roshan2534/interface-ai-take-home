"""Catalog is an agent-facing tool list. Run: python3 tests/test_catalog.py"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cua.catalog import (  # noqa: E402
    CatalogError,
    bind_arguments,
    get_capability,
    list_catalog,
    tool_specs,
)
from cua.serve import handle_request  # noqa: E402


def _fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    cards = list_catalog()
    if not cards:
        _fail("catalog is empty")
    ids = [row["id"] for row in cards]
    if "hcu-open-savings-subaccount" not in ids:
        _fail(f"missing open-savings capability in {ids}")
    card = next(row for row in cards if row["id"] == "hcu-open-savings-subaccount")
    if "steps" in card:
        _fail("catalog cards must not include recorded steps")
    input_names = {field["name"] for field in card["inputs"]}
    if input_names != {"member_id", "product", "deposit"}:
        _fail(f"unexpected inputs {input_names}")

    tools = tool_specs()
    spec = next(item["function"] for item in tools if item["function"]["name"] == card["id"])
    params = spec["parameters"]
    if params["additionalProperties"] is not False:
        _fail("tool schema must reject unknown keys")
    if "member_id" not in params["properties"]:
        _fail("tool schema missing member_id")
    if params["properties"]["member_id"]["type"] != "string":
        _fail("member_id should be a string")

    capability, path = get_capability("Open savings sub-account")
    if capability.id != "hcu-open-savings-subaccount":
        _fail("lookup by name failed")
    if path.name != "hcu-open-savings-subaccount.json":
        _fail(f"unexpected artifact path {path}")

    bound = bind_arguments(capability, {"member_id": "10001"})
    if bound["member_id"] != "10001":
        _fail("member_id did not bind")
    if bound["product"] != "REGULAR SHARE - SAVINGS":
        _fail("product default did not bind")
    if bound["deposit"] != "25.00":
        _fail("deposit default did not bind")

    try:
        bind_arguments(capability, {"member_id": "10001", "wire_to": "99"})
        _fail("unknown argument should raise")
    except CatalogError as exc:
        if exc.code != "invalid_call":
            _fail(f"wrong code for extra args: {exc.code}")

    try:
        get_capability("not-a-real-capability")
        _fail("unknown capability should raise")
    except CatalogError as exc:
        if exc.code != "unknown_capability":
            _fail(f"wrong code for unknown capability: {exc.code}")

    code, body = handle_request(
        "GET",
        "/v1/tools",
        b"",
        target="http://127.0.0.1:7878/",
        headed=False,
        handoff="off",
        handoff_max=1,
    )
    if code != 200:
        _fail(f"GET /v1/tools returned {code}")
    payload = json.loads(body)
    if not payload.get("tools"):
        _fail("GET /v1/tools had no tools")

    code, body = handle_request(
        "POST",
        "/v1/invoke",
        json.dumps({"name": "missing", "arguments": {"member_id": "10001"}}).encode(),
        target="http://127.0.0.1:7878/",
        headed=False,
        handoff="off",
        handoff_max=1,
    )
    if code != 404:
        _fail(f"unknown invoke should be 404, got {code}")
    err = json.loads(body)
    if err.get("code") != "unknown_capability":
        _fail(f"unknown invoke code {err}")

    code, body = handle_request(
        "POST",
        "/v1/invoke",
        json.dumps(
            {
                "name": "hcu-open-savings-subaccount",
                "arguments": {"member_id": "10001", "not_an_input": "x"},
            }
        ).encode(),
        target="http://127.0.0.1:7878/",
        headed=False,
        handoff="off",
        handoff_max=1,
    )
    if code != 400:
        _fail(f"bad args should be 400, got {code} {body!r}")

    print("ok: catalog lists the artifact, emits a typed tool, and rejects bad calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
