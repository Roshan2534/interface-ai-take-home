from __future__ import annotations

import json
import re
from typing import Any

from cua.artifact import build_capability, save_capability
from cua.evidence import RunLog
from cua.llm import complete, resolve_llm
from cua.schema import AgentAction, RunResult
from cua.settings import MAX_STEPS, secrets
from cua.surface import Surface


def _obs_text(obs: dict[str, Any], step: int, goal: str) -> str:
    frames = []
    for frame in obs["frames"]:
        frames.append(
            {
                "name": frame["name"],
                "url": frame["url"],
                "controls": frame["controls"],
                "aria": frame["aria"],
                "text": frame["text"][:1800],
            }
        )
    return json.dumps(
        {
            "step": step,
            "goal": goal,
            "url": obs["url"],
            "title": obs["title"],
            "secrets_available": list(secrets().keys()),
            "frames": frames,
        },
        indent=2,
    )


def _parse_action(raw: dict[str, Any]) -> AgentAction:
    return AgentAction.model_validate(raw)


def _fill_value(action: AgentAction) -> AgentAction:
    if action.secret:
        store = secrets()
        if action.secret not in store:
            raise RuntimeError(f"Unknown secret {action.secret}")
        action.text = store[action.secret]
    return action


def discover(
    goal: str, target: str, surface: Surface, provider: str | None = None
) -> RunResult:
    cfg = resolve_llm(provider)
    print(f"Using {cfg.provider} / {cfg.model}", flush=True)
    log = RunLog(goal, target, kind="discover")
    surface.goto(target)
    recorded: list[AgentAction] = []
    history: list[dict[str, Any]] = []
    outputs: dict[str, str] = {}
    checkpoint: list[str] = []
    checkpoint_frame = "main"

    for step in range(1, MAX_STEPS + 1):
        obs = surface.observe()
        user_text = _obs_text(obs, step, goal)
        history.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{obs['jpeg_b64']}",
                            "detail": "low",
                        },
                    },
                ],
            }
        )
        # Keep only the latest screenshot; earlier turns as text.
        compact: list[dict[str, Any]] = []
        for message in history[:-1]:
            content = message["content"]
            if isinstance(content, list):
                text = next(
                    (part["text"] for part in content if part.get("type") == "text"),
                    "",
                )
                compact.append({"role": message["role"], "content": text})
            else:
                compact.append(message)
        compact.append(history[-1])

        raw = complete(compact, cfg)
        action = _fill_value(_parse_action(raw))
        log.event(step=step, thought=action.thought, action=action.model_dump())
        log.save_step(
            step,
            {"observation": user_text, "action": action.model_dump()},
            obs["png"],
        )
        history.append({"role": "assistant", "content": json.dumps(raw)})
        print(f"discover {step} {action.action} {action.field_name or action.name or ''}", flush=True)

        if action.action == "fail":
            result = RunResult(
                status="failed",
                goal=goal,
                target=target,
                error=action.error or action.thought or "agent failed",
                steps=step,
                run_dir=str(log.dir),
            )
            log.finish(result.model_dump())
            return result

        if action.action == "done":
            outputs = action.outputs
            checkpoint = action.checkpoint or list(outputs.values())
            checkpoint_frame = action.frame or "main"
            if checkpoint and not surface.checkpoint(checkpoint_frame, checkpoint):
                # Fall back to scanning every frame before failing.
                blob = surface.visible_text().upper()
                if not all(item.upper() in blob for item in checkpoint):
                    result = RunResult(
                        status="failed",
                        goal=goal,
                        target=target,
                        error=f"Done claimed but checkpoint {checkpoint!r} not visible",
                        outputs=outputs,
                        steps=step,
                        run_dir=str(log.dir),
                    )
                    log.finish(result.model_dump())
                    return result
            capability = build_capability(
                goal=goal,
                target=target,
                recorded=recorded,
                outputs=outputs,
                checkpoint=checkpoint,
                checkpoint_frame=checkpoint_frame,
            )
            path = save_capability(capability)
            result = RunResult(
                status="success",
                goal=goal,
                target=target,
                outputs=outputs,
                steps=step,
                artifact_path=str(path),
                run_dir=str(log.dir),
            )
            log.finish(result.model_dump())
            return result

        surface.act(action)
        recorded.append(action)

    result = RunResult(
        status="failed",
        goal=goal,
        target=target,
        error=f"Stopped after {MAX_STEPS} steps without completing the goal",
        steps=MAX_STEPS,
        run_dir=str(log.dir),
    )
    log.finish(result.model_dump())
    return result


def _step_value(step, inputs: dict[str, str], defaults: dict[str, str]) -> str | None:
    source = step.value_from or ""
    if source.startswith("secret:"):
        return secrets()[source.split(":", 1)[1]]
    if source.startswith("input:"):
        name = source.split(":", 1)[1]
        if inputs.get(name):
            return inputs[name]
        if defaults.get(name):
            return defaults[name]
        return step.literal
    return step.literal


def replay(capability, target: str, inputs: dict[str, str], surface: Surface) -> RunResult:
    log = RunLog(capability.description, target, kind="replay")
    surface.goto(target)
    defaults = {field.name: field.default or "" for field in capability.inputs}
    for index, step in enumerate(capability.steps, start=1):
        value = _step_value(step, inputs, defaults)
        shown = "[secret]" if (step.value_from or "").startswith("secret:") else (value or "")
        print(f"replay {step.id} {step.action} {shown}", flush=True)
        log.event(step=index, step_id=step.id, action=step.action)
        surface.replay_step(step, value)
        if index == len(capability.steps):
            obs = surface.observe()
            log.save_step(index, {"step": step.model_dump(), "url": obs["url"]}, obs["png"])

    texts = capability.checkpoint.texts
    ok = True
    if texts:
        ok = surface.checkpoint(capability.checkpoint.frame, texts)
        if not ok:
            blob = surface.visible_text().upper()
            ok = all(item.upper() in blob for item in texts)
    outputs = {}
    if ok:
        blob = surface.visible_text()
        for field in capability.outputs:
            outputs[field.name] = _guess_output(field.name, blob)
        result = RunResult(
            status="success",
            goal=capability.description,
            target=target,
            outputs=outputs,
            steps=len(capability.steps),
            artifact_path=None,
            run_dir=str(log.dir),
        )
    else:
        result = RunResult(
            status="failed",
            goal=capability.description,
            target=target,
            error=f"Checkpoint {texts!r} not visible after replay",
            steps=len(capability.steps),
            run_dir=str(log.dir),
        )
    log.finish(result.model_dump())
    return result


def _guess_output(name: str, blob: str) -> str:
    flat = re.sub(r"\s+", " ", blob)
    if name in {"confirmation", "confirmation_code"}:
        match = re.search(r"CUS-\d+", flat)
        return match.group(0) if match else ""
    if name in {"new_account_id", "new_account_number"}:
        match = re.search(r"NEW ACCT\s+([0-9-]+)", flat)
        if match:
            return match.group(1)
        ids = re.findall(r"80-\d+-\d+", flat)
        return ids[-1] if ids else ""
    if "saving" in name.lower():
        match = re.search(r"SAVINGS BAL(?:ANCE)?\s+([\d,]+\.\d{2})", flat)
        return match.group(1) if match else ""
    if "name" in name.lower():
        match = re.search(r"MEMBER\s+\d+\s+([A-Z ]+?)(?:\s+NEW|\s+STATUS|$)", flat)
        return match.group(1).strip() if match else ""
    return ""


# Known-good teller path for locator smoke tests. Discovery does not use this.
SMOKE_ACTIONS = [
    AgentAction(action="fill", frame="root", field_name="OPID", secret="operator_id", bind=None),
    AgentAction(action="fill", frame="root", field_name="PSWD", secret="password"),
    AgentAction(action="click", frame="root", role="button", name="ENTER"),
    AgentAction(action="fill", frame="main", field_name="MEMNO", text="12345", bind="member_id"),
    AgentAction(action="click", frame="main", role="button", name="INQUIRE"),
    AgentAction(action="click", frame="main", role="button", name="OPEN SUB-ACCOUNT"),
    AgentAction(action="click", frame="main", role="button", name="CONTINUE"),
    AgentAction(action="select", frame="main", field_name="PROD", text="REGULAR SHARE", bind="product"),
    AgentAction(action="fill", frame="main", field_name="DEPAMT", text="25.00", bind="deposit"),
    AgentAction(action="click", frame="main", role="button", name="SUBMIT OPEN"),
]


def smoke(goal: str, target: str, surface: Surface) -> RunResult:
    log = RunLog(goal, target, kind="smoke")
    surface.goto(target)
    recorded: list[AgentAction] = []
    for index, raw in enumerate(SMOKE_ACTIONS, start=1):
        action = _fill_value(raw.model_copy())
        obs = surface.observe()
        log.save_step(index, {"action": action.model_dump()}, obs["png"])
        print(
            f"smoke {index}/{len(SMOKE_ACTIONS)} {action.action} "
            f"{action.field_name or action.name or ''}",
            flush=True,
        )
        surface.act(action)
        recorded.append(action)
    obs = surface.observe()
    blob = surface.visible_text()
    if "SUB-ACCOUNT OPENED" not in blob:
        result = RunResult(
            status="failed",
            goal=goal,
            target=target,
            error="Smoke finished without confirmation screen",
            run_dir=str(log.dir),
        )
        log.finish(result.model_dump())
        return result
    outputs = {
        "member_name": _guess_output("member_name", blob),
        "savings_balance": _guess_output("savings_balance", blob),
        "new_account_id": _guess_output("new_account_id", blob),
        "confirmation": _guess_output("confirmation", blob),
    }
    capability = build_capability(
        goal=goal,
        target=target,
        recorded=recorded,
        outputs=outputs,
        checkpoint=["SUB-ACCOUNT OPENED"],
        checkpoint_frame="main",
    )
    path = save_capability(capability)
    result = RunResult(
        status="success",
        goal=goal,
        target=target,
        outputs=outputs,
        steps=len(recorded),
        artifact_path=str(path),
        run_dir=str(log.dir),
    )
    log.finish(result.model_dump())
    return result

