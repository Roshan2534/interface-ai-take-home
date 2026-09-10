from __future__ import annotations

import ast
import sys
from pathlib import Path

from cua.settings import ROOT

FORBIDDEN_CUA = {"cua.llm", "cua.agent"}
FORBIDDEN_PKGS = {"openai", "anthropic", "google.genai", "google.generativeai"}
REPLAY_ENTRY = "cua.replay"


def _module_file(name: str) -> Path | None:
    if not name.startswith("cua."):
        return None
    path = ROOT / Path(*name.split(".")).with_suffix(".py")
    return path if path.exists() else None


def _imports_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{node.module}.{alias.name}")
    return found


def check_replay_imports() -> dict[str, object]:
    """Fail if replay's source or import graph can load a model client."""
    hits: list[str] = []
    seen: set[str] = set()
    stack = [REPLAY_ENTRY]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _module_file(name)
        if path is None:
            continue
        for imported in sorted(_imports_in(path)):
            top = imported.split(".")[0]
            if imported in FORBIDDEN_CUA or imported.startswith("cua.llm") or imported.startswith("cua.agent"):
                hits.append(f"{name} imports {imported}")
            if top in FORBIDDEN_PKGS:
                hits.append(f"{name} imports {imported}")
            child = imported
            if imported.startswith("cua.") and _module_file(child):
                stack.append(child)
            elif imported.startswith("cua."):
                parent = ".".join(imported.split(".")[:2])
                if _module_file(parent):
                    stack.append(parent)

    import cua.replay  # noqa: F401

    runtime = [
        name
        for name in ("cua.llm", "cua.agent", "openai", "anthropic", "google.genai")
        if name in sys.modules
    ]
    ok = not hits and not runtime
    return {
        "ok": ok,
        "forbidden_in_source": hits,
        "forbidden_in_runtime": runtime,
        "replay_modules": sorted(name for name in seen if name.startswith("cua.")),
    }
