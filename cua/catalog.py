"""Saved artifacts as named tools an agent can list and call.

Replay stays the executor. This module only loads JSON, emits a function-calling
schema, binds typed arguments, and hands a Capability to replay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cua.artifact import load_capability
from cua.log import log_event
from cua.schema import Capability, IOField, RunResult
from cua.settings import ARTIFACT_DIR, ROOT


class CatalogError(Exception):
    def __init__(self, message: str, code: str = "invalid_call") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _repo_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def artifact_files(directory: Path | None = None) -> list[Path]:
    root = directory or ARTIFACT_DIR
    if not root.exists():
        return []
    return sorted(path for path in root.glob("*.json") if path.is_file())


def load_catalog(directory: Path | None = None) -> list[tuple[Capability, Path]]:
    found: list[tuple[Capability, Path]] = []
    for path in artifact_files(directory):
        capability = load_capability(path)
        found.append((capability, path))
    log_event("catalog.load", count=len(found), dir=str(directory or ARTIFACT_DIR))
    return found


def get_capability(name: str, directory: Path | None = None) -> tuple[Capability, Path]:
    needle = (name or "").strip()
    if not needle:
        raise CatalogError("capability name is required", code="unknown_capability")
    catalog = load_catalog(directory)
    matches: list[tuple[Capability, Path]] = []
    lowered = needle.lower()
    for capability, path in catalog:
        if capability.id == needle or capability.name.lower() == lowered:
            matches.append((capability, path))
    if not matches:
        known = [item.id for item, _ in catalog]
        raise CatalogError(
            f"unknown capability {needle!r}. known: {known}",
            code="unknown_capability",
        )
    if len(matches) > 1:
        raise CatalogError(
            f"capability {needle!r} matches more than one artifact",
            code="invalid_call",
        )
    capability, path = matches[0]
    log_event("catalog.get", capability_id=capability.id, path=_repo_rel(path))
    return capability, path


def card(capability: Capability, path: Path) -> dict[str, Any]:
    return {
        "id": capability.id,
        "name": capability.name,
        "version": capability.version,
        "description": capability.description,
        "surface": capability.surface,
        "entry": capability.entry,
        "inputs": [field.model_dump() for field in capability.inputs],
        "outputs": [field.model_dump() for field in capability.outputs],
        "step_count": len(capability.steps),
        "checkpoint": capability.checkpoint.texts,
        "artifact_path": _repo_rel(path),
    }


def list_catalog(directory: Path | None = None) -> list[dict[str, Any]]:
    return [card(capability, path) for capability, path in load_catalog(directory)]


def _json_type(field: IOField) -> str:
    raw = (field.type or "string").lower()
    if raw in {"string", "number", "integer", "boolean"}:
        return raw
    return "string"


def _field_schema(field: IOField) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "type": _json_type(field),
        "description": field.description or field.name,
    }
    if field.default is not None:
        spec["default"] = field.default
    return spec


def _required_names(capability: Capability) -> list[str]:
    names = []
    for field in capability.inputs:
        if field.required and field.default is None:
            names.append(field.name)
    return names


def tool_spec(capability: Capability) -> dict[str, Any]:
    """OpenAI-style function tool. Name is the capability id."""
    properties = {field.name: _field_schema(field) for field in capability.inputs}
    return {
        "type": "function",
        "function": {
            "name": capability.id,
            "description": capability.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": _required_names(capability),
                "additionalProperties": False,
            },
        },
    }


def tool_specs(directory: Path | None = None) -> list[dict[str, Any]]:
    return [tool_spec(capability) for capability, _ in load_catalog(directory)]


def _coerce(field: IOField, value: Any) -> str:
    kind = _json_type(field)
    if value is None:
        raise CatalogError(f"{field.name} must not be null", code="invalid_call")
    if kind == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        raise CatalogError(f"{field.name} must be a boolean", code="invalid_call")
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise CatalogError(f"{field.name} must be an integer", code="invalid_call")
        return str(value)
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CatalogError(f"{field.name} must be a number", code="invalid_call")
        return str(value)
    if not isinstance(value, (str, int, float)):
        raise CatalogError(f"{field.name} must be a string", code="invalid_call")
    text = str(value).strip()
    if not text:
        raise CatalogError(f"{field.name} must not be empty", code="invalid_call")
    return text


def bind_arguments(
    capability: Capability, arguments: dict[str, Any] | None
) -> dict[str, str]:
    payload = arguments if arguments is not None else {}
    if not isinstance(payload, dict):
        raise CatalogError("arguments must be an object", code="invalid_call")
    allowed = {field.name for field in capability.inputs}
    extra = sorted(set(payload) - allowed)
    if extra:
        raise CatalogError(
            f"unknown arguments: {extra}. allowed: {sorted(allowed)}",
            code="invalid_call",
        )
    bound: dict[str, str] = {}
    for field in capability.inputs:
        if field.name in payload:
            bound[field.name] = _coerce(field, payload[field.name])
        elif field.default is not None:
            bound[field.name] = field.default
        elif field.required:
            raise CatalogError(
                f"missing required argument: {field.name}",
                code="invalid_call",
            )
    log_event(
        "catalog.bind",
        capability_id=capability.id,
        inputs=list(bound),
    )
    return bound


def invocation_payload(
    capability: Capability,
    path: Path,
    call_name: str,
    arguments: dict[str, Any],
    bound: dict[str, str],
    result: RunResult,
) -> dict[str, Any]:
    dumped = result.model_dump()
    dumped["artifact_path"] = dumped.get("artifact_path") or _repo_rel(path)
    if dumped.get("run_dir"):
        dumped["run_dir"] = _repo_rel(Path(dumped["run_dir"]))
    return {
        "call": {"name": call_name, "arguments": arguments},
        "bound": bound,
        "capability": {
            "id": capability.id,
            "name": capability.name,
            "version": capability.version,
            "artifact_path": _repo_rel(path),
        },
        "result": dumped,
    }


def error_payload(exc: CatalogError) -> dict[str, str]:
    return {"error": exc.message, "code": exc.code}
