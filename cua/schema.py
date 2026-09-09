from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class Locator(BaseModel):
    """How to find a control. Try in order; first match wins on replay."""

    by: Literal["name", "role", "text"]
    name: str | None = None
    role: str | None = None
    accessible_name: str | None = None
    text: str | None = None


class Step(BaseModel):
    id: str
    action: Literal["click", "fill", "select", "press", "wait"]
    frame: str = "root"
    locators: list[Locator] = Field(default_factory=list)
    value_from: str | None = None
    literal: str | None = None
    key: str | None = None
    wait_ms: int | None = None
    notes: str = ""


class IOField(BaseModel):
    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    default: str | None = None


class Checkpoint(BaseModel):
    frame: str = "main"
    texts: list[str] = Field(default_factory=list)


class Capability(BaseModel):
    schema_version: str = "1.0"
    id: str
    name: str
    version: int = 1
    description: str
    surface: Literal["web"] = "web"
    entry: str
    inputs: list[IOField] = Field(default_factory=list)
    outputs: list[IOField] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    checkpoint: Checkpoint = Field(default_factory=Checkpoint)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AgentAction(BaseModel):
    thought: str = ""
    action: Literal["click", "fill", "select", "press", "wait", "done", "fail"]
    frame: str = "root"
    role: str | None = None
    name: str | None = None
    field_name: str | None = None
    text: str | None = None
    key: str | None = None
    secret: str | None = None
    bind: str | None = None
    wait_ms: int = 400
    outputs: dict[str, str] = Field(default_factory=dict)
    checkpoint: list[str] = Field(default_factory=list)
    error: str | None = None


class RunResult(BaseModel):
    status: Literal["success", "business_outcome", "failed"]
    outcome: str | None = None
    goal: str
    target: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    step_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    reason: str | None = None
    steps: int = 0
    artifact_path: str | None = None
    run_dir: str | None = None
