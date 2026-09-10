from __future__ import annotations

import json
import re
from typing import Any

from cua.artifact import build_capability, save_capability
from cua.evidence import RunLog
from cua.handoff import SessionControl
from cua.llm import complete, resolve_llm
from cua.log import log_event, setup_logging
from cua.replay import _fill_value, _handoff_until_clear, _inspect_after_human
from cua.schema import AgentAction, RunResult
from cua.settings import HANDOFF_MODE, MAX_STEPS, secrets
from cua.surface import Surface, _squash


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
    action = AgentAction.model_validate(raw)
    if action.bind in {"", "null", "none", "optional"}:
        action.bind = None
    if action.secret in {"", "null", "none"}:
        action.secret = None
    # Models often say "press CONTINUE"; that is a click, not a keyboard key.
    if action.action == "press" and (action.name or action.field_name or action.role):
        action.action = "click"
    return action



def _stable_checkpoint(raw: list[str], outputs: dict[str, str], blob: str) -> list[str]:
    """Keep success markers, drop this-run ids so replay still works on other members."""
    skip = [value for value in outputs.values() if value]
    kept: list[str] = []
    for item in raw:
        upper = item.upper()
        if any(value.upper() in upper for value in skip):
            continue
        if re.search(r"\b\d{2}-\d+", item) or re.search(r"CUS-\d+", item, re.I):
            continue
        kept.append(item)
    if "SUB-ACCOUNT OPENED" in _squash(blob):
        if not any("SUB-ACCOUNT OPENED" in item.upper() for item in kept):
            kept.insert(0, "SUB-ACCOUNT OPENED")
        return kept or ["SUB-ACCOUNT OPENED"]
    return kept or raw



def discover(
    goal: str,
    target: str,
    surface: Surface,
    provider: str | None = None,
    handoff_mode: str | None = None,
    handoff_max: int | None = None,
) -> RunResult:
    cfg = resolve_llm(provider)
    print(f"Using {cfg.provider} / {cfg.model}", flush=True)
    log = RunLog(goal, target, kind="discover")
    setup_logging(log.dir)
    mode = handoff_mode or HANDOFF_MODE
    control = SessionControl(surface, log.dir, mode=mode, max_attempts=handoff_max)
    log_event(
        "discover.start",
        goal=goal,
        target=target,
        provider=cfg.provider,
        handoff=mode,
        handoff_max=control.max_attempts,
    )
    log_event("run.dir", path=str(log.dir), kind="discover")
    surface.goto(target)
    recorded: list[AgentAction] = []
    history: list[dict[str, Any]] = []
    outputs: dict[str, str] = {}
    checkpoint: list[str] = []
    checkpoint_frame = "main"
    repeats = 0

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

        sig = (action.action, action.frame, action.name, action.field_name)
        if recorded and (
            recorded[-1].action,
            recorded[-1].frame,
            recorded[-1].name,
            recorded[-1].field_name,
        ) == sig:
            repeats += 1
        else:
            repeats = 0
        if repeats >= 2:
            log_event("discover.stuck_repeat", step=step, name=action.name)
            if mode != "off":
                before = surface.visible_text()
                recovered = _handoff_until_clear(
                    control,
                    mode,
                    goal=goal,
                    step_id=f"s{step}",
                    reason="Agent repeated the same control without the screen changing",
                    observed=before[:800],
                    screenshot=surface.shot(),
                    inspect=_inspect_after_human(surface, before),
                )
                if recovered["kind"] in {"abort", "exhausted"}:
                    result = RunResult(
                        status="failed",
                        outcome="handoff_exhausted"
                        if recovered["kind"] == "exhausted"
                        else "handoff_abort",
                        goal=goal,
                        target=target,
                        error=recovered.get("reason") or "Operator aborted after stuck loop",
                        steps=step,
                        run_dir=str(log.dir),
                    )
                    log.finish(result.model_dump())
                    return result
            history.append(
                {
                    "role": "user",
                    "content": (
                        "That control was already used and the screen did not change. "
                        "Do not repeat it. If you are on OFAC, click CONTINUE once. "
                        "If the open form is visible, select PROD=REGULAR SHARE, fill DEPAMT=25.00, "
                        "then click SUBMIT OPEN."
                    ),
                }
            )
            repeats = 0
            continue

        if action.action == "fail":
            log_event("discover.agent_fail", step=step, error=action.error or action.thought)
            if mode != "off":
                before = surface.visible_text()
                recovered = _handoff_until_clear(
                    control,
                    mode,
                    goal=goal,
                    step_id=f"s{step}",
                    reason=action.error or action.thought or "agent failed",
                    observed=before[:800],
                    screenshot=surface.shot(),
                    inspect=_inspect_after_human(surface, before),
                )
                if recovered["kind"] == "continue":
                    log_event("discover.fail_resumed")
                    continue
                if recovered["kind"] in {"abort", "exhausted"}:
                    result = RunResult(
                        status="failed",
                        outcome="handoff_exhausted"
                        if recovered["kind"] == "exhausted"
                        else "handoff_abort",
                        goal=goal,
                        target=target,
                        error=recovered.get("reason") or action.error or "agent failed",
                        steps=step,
                        run_dir=str(log.dir),
                    )
                    log.finish(result.model_dump())
                    return result
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
            checkpoint_frame = action.frame or "main"
            blob = surface.visible_text()
            checkpoint = _stable_checkpoint(
                action.checkpoint or list(outputs.values()),
                outputs,
                blob,
            )
            if checkpoint and not surface.checkpoint(checkpoint_frame, checkpoint):
                if not all(_squash(item) in _squash(blob) for item in checkpoint):
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
            log_event("discover.done", artifact=str(path), outputs=outputs)
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
        control.blocked_passes = 0

    result = RunResult(
        status="failed",
        goal=goal,
        target=target,
        error=f"Stopped after {MAX_STEPS} steps without completing the goal",
        steps=MAX_STEPS,
        run_dir=str(log.dir),
    )
    log_event("discover.max_steps", steps=MAX_STEPS)
    log.finish(result.model_dump())
    return result

