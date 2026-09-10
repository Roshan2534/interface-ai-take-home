from __future__ import annotations

import re
from typing import Any

from cua.artifact import build_capability, save_capability
from cua.evidence import RunLog
from cua.handoff import SessionControl, _exhausted_message
from cua.log import log_event, setup_logging
from cua.outcomes import after_human, classify_screen, needs_handoff
from cua.schema import AgentAction, RunResult
from cua.settings import HANDOFF_MODE, secrets
from cua.surface import Surface, _squash


_LABEL_FIELDS = {
    "OPERATOR ID": "OPID",
    "PASSWORD": "PSWD",
    "MEM NO": "MEMNO",
    "MEMBER NO": "MEMNO",
    "MEMBER NUMBER": "MEMNO",
    "PROD": "PROD",
    "PRODUCT": "PROD",
    "DEPAMT": "DEPAMT",
    "DEPOSIT": "DEPAMT",
    "OPENING DEPOSIT": "DEPAMT",
}


def _fill_value(action: AgentAction) -> AgentAction:
    if action.secret:
        store = secrets()
        if action.secret not in store:
            raise RuntimeError(f"Unknown secret {action.secret}")
        action.text = store[action.secret]
    if not action.field_name and action.name:
        mapped = _LABEL_FIELDS.get(action.name.strip().upper())
        if mapped:
            action.field_name = mapped
    return action


def _human_gate(
    control: SessionControl,
    mode: str,
    *,
    reason: str,
    goal: str,
    step_id: str | None,
    observed: str,
    screenshot: bytes | None,
    attempt: int = 1,
    restated: bool = False,
) -> str:
    log_event(
        "handoff.gate",
        mode=mode,
        reason=reason,
        step_id=step_id,
        attempt=attempt,
        restated=restated,
    )
    if mode == "off":
        log_event("handoff.skipped", reason=reason, mode="off")
        return "abort"
    return control.escalate(
        reason=reason,
        goal=goal,
        step_id=step_id,
        observed=observed,
        screenshot=screenshot,
        attempt=attempt,
        max_attempts=control.max_attempts,
        restated=restated,
    )


def _handoff_until_clear(
    control: SessionControl,
    mode: str,
    *,
    goal: str,
    step_id: str | None,
    reason: str,
    observed: str,
    screenshot: bytes | None,
    inspect,
) -> dict[str, Any]:
    """Hand off up to control.max_attempts times, then abort if still unknown.

    inspect() must return {kind, hit, blob, screenshot, reason} where kind is
    continue | known_stop | blocked.
    """
    limit = control.max_attempts
    current_reason = reason
    current_observed = observed
    current_shot = screenshot
    restated = False
    while True:
        control.blocked_passes += 1
        n = control.blocked_passes
        log_event(
            "handoff.attempt",
            n=n,
            max=limit,
            restated=restated,
            reason=current_reason,
        )
        if n > limit:
            log_event("handoff.exhausted", n=n, max=limit, reason=current_reason)
            print(
                _exhausted_message(
                    reason=current_reason,
                    observed=current_observed,
                    max_attempts=limit,
                ),
                flush=True,
            )
            return {
                "kind": "exhausted",
                "hit": None,
                "blob": current_observed,
                "reason": current_reason,
            }
        decision = _human_gate(
            control,
            mode,
            reason=current_reason,
            goal=goal,
            step_id=step_id,
            observed=current_observed,
            screenshot=current_shot,
            attempt=n,
            restated=restated,
        )
        if decision == "abort":
            log_event("handoff.operator_abort", n=n)
            return {"kind": "abort", "hit": None, "blob": current_observed, "reason": current_reason}
        check = inspect()
        log_event(
            "handoff.recheck",
            n=n,
            kind=check.get("kind"),
            outcome=(check.get("hit").outcome if check.get("hit") is not None else None),
        )
        if check["kind"] != "blocked":
            control.blocked_passes = 0
            log_event("handoff.resolved", n=n, kind=check["kind"])
            return check
        restated = True
        current_reason = check.get("reason") or current_reason
        current_observed = check.get("blob") or current_observed
        current_shot = check.get("screenshot") or current_shot
        log_event("handoff.still_blocked", n=n, reason=current_reason)


def _inspect_after_human(surface: Surface, before: str | None = None):
    def inspect() -> dict[str, Any]:
        blob = surface.visible_text()
        kind, hit = after_human(blob)
        reason = hit.reason if hit else ""
        if kind == "continue" and before is not None and blob.strip() == before.strip():
            kind = "blocked"
            reason = (
                "You pressed Enter without changing the screen. "
                "The agent still cannot proceed from this page."
            )
            log_event("handoff.unchanged_screen")
        elif kind == "blocked":
            reason = (
                hit.reason
                + " The screen after you pressed Enter is still not one this capability recorded."
            )
        return {
            "kind": kind,
            "hit": hit,
            "blob": blob,
            "screenshot": surface.shot(),
            "reason": reason,
        }

    return inspect


def _inspect_locator_retry(surface: Surface, step, value: str | None):
    def inspect() -> dict[str, Any]:
        try:
            surface.replay_step(step, value)
        except Exception as exc:
            log_event("replay.retry_failed", step_id=step.id, error=str(exc))
            blob = surface.visible_text()
            kind, hit = after_human(blob)
            if kind == "blocked":
                reason = hit.reason if hit else str(exc)
            else:
                kind = "blocked"
                reason = (
                    f"Locator still failing at {step.id} after you pressed Enter: {exc}"
                )
            return {
                "kind": kind,
                "hit": hit,
                "blob": blob,
                "screenshot": surface.shot(),
                "reason": reason,
            }
        blob = surface.visible_text()
        kind, hit = after_human(blob)
        return {
            "kind": kind,
            "hit": hit,
            "blob": blob,
            "screenshot": surface.shot(),
            "reason": hit.reason if hit else "",
        }

    return inspect


def _result_from_recovery(
    recovered: dict[str, Any],
    capability,
    target: str,
    inputs: dict[str, str],
    *,
    step_id: str | None,
    steps: int,
    run_dir: str,
    fallback_error: str | None = None,
) -> RunResult:
    kind = recovered.get("kind")
    blob = recovered.get("blob") or ""
    hit = recovered.get("hit")
    if kind == "known_stop" and hit is not None:
        return _replay_stop(
            capability,
            target,
            inputs,
            step_id=step_id,
            steps=steps,
            run_dir=run_dir,
            hit=hit,
            observed=blob,
        )
    outcome = "handoff_exhausted" if kind == "exhausted" else "handoff_abort"
    reason = recovered.get("reason") or fallback_error or "Handoff did not unblock the agent"
    return RunResult(
        status="failed",
        outcome=outcome,
        goal=capability.description,
        target=target,
        inputs=inputs,
        error=reason,
        step_id=step_id,
        expected="A host screen this capability already recorded",
        observed=_clip_obs(blob) if blob else None,
        reason=reason,
        steps=steps,
        run_dir=run_dir,
    )


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


def replay(
    capability,
    target: str,
    inputs: dict[str, str],
    surface: Surface,
    handoff_mode: str | None = None,
    handoff_max: int | None = None,
) -> RunResult:
    log = RunLog(capability.description, target, kind="replay")
    setup_logging(log.dir)
    mode = handoff_mode or HANDOFF_MODE
    control = SessionControl(surface, log.dir, mode=mode, max_attempts=handoff_max)
    log_event(
        "replay.start",
        target=target,
        inputs=inputs,
        handoff=mode,
        handoff_max=control.max_attempts,
    )
    log_event("run.dir", path=str(log.dir), kind="replay")
    surface.goto(target)
    defaults = {field.name: field.default or "" for field in capability.inputs}
    last_blob = ""
    for index, step in enumerate(capability.steps, start=1):
        value = _step_value(step, inputs, defaults)
        shown = "[secret]" if (step.value_from or "").startswith("secret:") else (value or "")
        print(f"replay {step.id} {step.action} {shown}", flush=True)
        log.event(step=index, step_id=step.id, action=step.action, value=shown)
        try:
            surface.replay_step(step, value)
        except Exception as exc:
            log_event("replay.step_exception", step_id=step.id, error=str(exc))
            blob = surface.visible_text()
            shot = surface.shot()
            log.save_step(
                index,
                {
                    "step": step.model_dump(),
                    "url": surface.page.url,
                    "error": str(exc),
                    "screen": blob[:2000],
                },
                shot,
            )
            if mode == "off":
                hit = classify_screen(blob)
                result = _replay_stop(
                    capability,
                    target,
                    inputs,
                    step_id=step.id,
                    steps=index,
                    run_dir=str(log.dir),
                    hit=hit,
                    fallback_error=str(exc),
                    observed=blob,
                )
                log.finish(result.model_dump())
                return result
            recovered = _handoff_until_clear(
                control,
                mode,
                goal=capability.description,
                step_id=step.id,
                reason=f"Locator failed at {step.id}: {exc}",
                observed=blob[:800],
                screenshot=shot,
                inspect=_inspect_locator_retry(surface, step, value),
            )
            if recovered["kind"] == "continue":
                log_event("replay.retry_after_handoff", step_id=step.id)
            else:
                result = _result_from_recovery(
                    recovered,
                    capability,
                    target,
                    inputs,
                    step_id=step.id,
                    steps=index,
                    run_dir=str(log.dir),
                    fallback_error=str(exc),
                )
                log.finish(result.model_dump())
                return result

        blob = surface.visible_text()
        last_blob = blob
        hit = classify_screen(blob)
        shot = surface.shot() if (hit or index == len(capability.steps)) else None
        log.save_step(
            index,
            {
                "step": step.model_dump(),
                "url": surface.page.url,
                "screen": blob[:2000],
            },
            shot,
        )
        if hit:
            log_event("replay.classified", outcome=hit.outcome, step_id=step.id, status=hit.status)
            if needs_handoff(hit) and mode != "off":
                recovered = _handoff_until_clear(
                    control,
                    mode,
                    goal=capability.description,
                    step_id=step.id,
                    reason=hit.reason,
                    observed=hit.observed,
                    screenshot=shot or surface.shot(),
                    inspect=_inspect_after_human(surface),
                )
                if recovered["kind"] == "continue":
                    last_blob = recovered.get("blob") or surface.visible_text()
                    log_event("handoff.resolved", next_step="continue_replay")
                    continue
                result = _result_from_recovery(
                    recovered,
                    capability,
                    target,
                    inputs,
                    step_id=step.id,
                    steps=index,
                    run_dir=str(log.dir),
                )
                log.finish(result.model_dump())
                return result
            result = _replay_stop(
                capability,
                target,
                inputs,
                step_id=step.id,
                steps=index,
                run_dir=str(log.dir),
                hit=hit,
                observed=blob,
            )
            log.finish(result.model_dump())
            return result

    texts = capability.checkpoint.texts
    ok = True
    if texts:
        ok = surface.checkpoint(capability.checkpoint.frame, texts)
        if not ok:
            blob = last_blob or surface.visible_text()
            ok = all(_squash(item) in _squash(blob) for item in texts)
    if ok:
        blob = last_blob or surface.visible_text()
        outputs = {field.name: _guess_output(field.name, blob) for field in capability.outputs}
        result = RunResult(
            status="success",
            outcome="opened_subaccount",
            goal=capability.description,
            target=target,
            inputs=inputs,
            outputs=outputs,
            step_id=capability.steps[-1].id if capability.steps else None,
            expected=" ".join(texts) or "SUB-ACCOUNT OPENED",
            observed=_clip_obs(blob),
            reason="Checkpoint matched. Capability completed without the LLM.",
            steps=len(capability.steps),
            run_dir=str(log.dir),
        )
        log_event("replay.success", outputs=outputs)
    else:
        blob = last_blob or surface.visible_text()
        hit = classify_screen(blob)
        result = _replay_stop(
            capability,
            target,
            inputs,
            step_id=capability.steps[-1].id if capability.steps else None,
            steps=len(capability.steps),
            run_dir=str(log.dir),
            hit=hit,
            fallback_error=f"Checkpoint {texts!r} not visible after replay",
            observed=blob,
        )
    log.finish(result.model_dump())
    return result


def _clip_obs(blob: str) -> str:
    return " ".join(blob.split())[:400]


def _replay_stop(
    capability,
    target: str,
    inputs: dict[str, str],
    *,
    step_id: str | None,
    steps: int,
    run_dir: str,
    hit,
    observed: str,
    fallback_error: str | None = None,
) -> RunResult:
    if hit:
        return RunResult(
            status=hit.status,
            outcome=hit.outcome,
            goal=capability.description,
            target=target,
            inputs=inputs,
            error=None if hit.status == "business_outcome" else hit.reason,
            step_id=step_id,
            expected=hit.expected,
            observed=hit.observed,
            reason=hit.reason,
            steps=steps,
            run_dir=run_dir,
        )
    return RunResult(
        status="failed",
        outcome="hard_failure",
        goal=capability.description,
        target=target,
        inputs=inputs,
        error=fallback_error or "Replay stopped",
        step_id=step_id,
        expected="Checkpoint or next recorded control",
        observed=_clip_obs(observed),
        reason=fallback_error or "Unclassified host state",
        steps=steps,
        run_dir=run_dir,
    )


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
    if name in {"product"}:
        match = re.search(r"PRODUCT\s+(.+?)(?:\s+OPENING BAL|\s+SAVINGS BAL|$)", flat)
        return match.group(1).strip() if match else ""
    if name in {"opening_deposit", "deposit", "opening_bal"}:
        match = re.search(r"OPENING BAL(?:ANCE)?\s+([\d,]+\.\d{2})", flat)
        return match.group(1) if match else ""
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
    setup_logging(log.dir)
    log_event("smoke.start", goal=goal, target=target)
    log_event("run.dir", path=str(log.dir), kind="smoke")
    surface.goto(target)
    recorded: list[AgentAction] = []
    for index, raw in enumerate(SMOKE_ACTIONS, start=1):
        action = _fill_value(raw.model_copy())
        print(
            f"smoke {index}/{len(SMOKE_ACTIONS)} {action.action} "
            f"{action.field_name or action.name or ''}",
            flush=True,
        )
        surface.act(action)
        recorded.append(action)
        log.save_step(index, {"action": action.model_dump()}, None)
    blob = surface.visible_text()
    log.save_step(len(SMOKE_ACTIONS), {"screen": blob[:2000]}, surface.shot())
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

