from __future__ import annotations

import json
from pathlib import Path

from cua.schema import Capability, Checkpoint, IOField, Locator, Step
from cua.settings import ARTIFACT_DIR


def locators_for(action) -> list[Locator]:
    found: list[Locator] = []
    if action.field_name:
        found.append(Locator(by="name", name=action.field_name))
    if action.role:
        found.append(
            Locator(by="role", role=action.role, accessible_name=action.name)
        )
    if action.name and action.action == "click":
        found.append(Locator(by="text", text=action.name.strip()))
    return found


def value_from(action) -> tuple[str | None, str | None]:
    if action.secret:
        return f"secret:{action.secret}", None
    if action.bind:
        return f"input:{action.bind}", action.text
    if action.text is not None and action.action in {"fill", "select"}:
        return "literal", action.text
    return None, action.text


def build_capability(
    *,
    goal: str,
    target: str,
    recorded: list,
    outputs: dict[str, str],
    checkpoint: list[str],
    checkpoint_frame: str,
) -> Capability:
    steps: list[Step] = []
    inputs: dict[str, IOField] = {}
    for index, action in enumerate(recorded, start=1):
        if action.action not in {"click", "fill", "select", "press", "wait"}:
            continue
        source, literal = value_from(action)
        if source and source.startswith("input:"):
            name = source.split(":", 1)[1]
            inputs.setdefault(
                name,
                IOField(
                    name=name,
                    description=f"Bound from discovery step {index}",
                    default=action.text,
                ),
            )
        steps.append(
            Step(
                id=f"s{index}",
                action=action.action,
                frame=action.frame or "root",
                locators=locators_for(action),
                value_from=source,
                literal=literal,
                key=action.key,
                wait_ms=action.wait_ms if action.action == "wait" else None,
                notes=action.thought,
            )
        )

    output_fields = [
        IOField(name=key, description="Extracted from the confirmation/result screen")
        for key in outputs
    ]
    return Capability(
        id="hcu-open-savings-subaccount",
        name="Open savings sub-account",
        description=goal,
        entry=target,
        inputs=list(inputs.values()),
        outputs=output_fields,
        steps=steps,
        checkpoint=Checkpoint(frame=checkpoint_frame, texts=checkpoint),
    )


def save_capability(capability: Capability) -> Path:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_DIR / f"{capability.id}.json"
    path.write_text(capability.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_capability(path: Path) -> Capability:
    return Capability.model_validate(json.loads(path.read_text(encoding="utf-8")))
