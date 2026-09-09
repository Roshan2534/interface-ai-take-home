from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Diagnosis(BaseModel):
    status: Literal["business_outcome", "failed"]
    outcome: str
    expected: str
    observed: str
    reason: str


def classify_screen(blob: str) -> Diagnosis | None:
    """Map host text to the replay contract. None means keep going."""
    from cua.log import LOGGER, log_event

    flat = " ".join(blob.split())
    upper = flat.upper()
    hit: Diagnosis | None = None

    if "RECORD NOT FOUND" in upper:
        hit = Diagnosis(
            status="business_outcome",
            outcome="not_found",
            expected="Member detail page with accounts and OPEN SUB-ACCOUNT",
            observed=_clip(flat),
            reason=(
                "CIF returned a known miss. This is a business outcome for the caller, "
                "not a locator crash. Replay stops instead of opening a sub-account."
            ),
        )
    elif "SECURITY VIOLATION" in upper or "NOT AUTHORIZED" in upper:
        hit = Diagnosis(
            status="business_outcome",
            outcome="permission_denied",
            expected="Open-sub-account form after OFAC continue",
            observed=_clip(flat),
            reason=(
                "Operator is not authorized for OPN-SUB on this member. "
                "Reported as permission_denied, not a host outage."
            ),
        )
    elif "VALIDATION" in upper or "MEMBER NUMBER IS REQUIRED" in upper:
        hit = Diagnosis(
            status="business_outcome",
            outcome="validation",
            expected="Accepted input and the next host screen",
            observed=_clip(flat),
            reason="Host rejected the field values. Caller should fix inputs and retry.",
        )
    elif "SESSION TIMEOUT" in upper:
        hit = Diagnosis(
            status="failed",
            outcome="timeout",
            expected="Live servicing session still signed on",
            observed=_clip(flat),
            reason="Session expired. Hard stop; sign on again before retrying.",
        )
    elif "HOST MESSAGE 12E" in upper or "ADDRESS CHANGE PENDING" in upper:
        hit = Diagnosis(
            status="failed",
            outcome="unexpected_dialog",
            expected="Member detail (artifact was recorded without this interstitial)",
            observed=_clip(flat),
            reason=(
                "Recoverable host interstitial that this capability did not record. "
                "Stop rather than clicking through an unknown dialog."
            ),
        )
    if hit:
        log_event(
            "outcomes.classified",
            outcome=hit.outcome,
            status=hit.status,
            handoff=needs_handoff(hit),
        )
    else:
        LOGGER.debug("outcomes.clear_screen chars=%s", len(flat))
    return hit


def needs_handoff(hit: Diagnosis | None) -> bool:
    return bool(hit and hit.outcome in {"unexpected_dialog", "timeout"})


def after_human(blob: str) -> tuple[str, Diagnosis | None]:
    """Classify the live screen after the operator pressed Enter.

    continue    agent can keep running the recorded steps
    known_stop  a business outcome the artifact already knows how to report
    blocked     still not a screen the agent can continue from
    """
    hit = classify_screen(blob)
    if hit is None:
        if not looks_like_capability_screen(blob):
            return "blocked", Diagnosis(
                status="failed",
                outcome="unexpected_dialog",
                expected="A recorded servicing screen (inquiry, member detail, OFAC, or open form)",
                observed=_clip(blob),
                reason=(
                    "After you pressed Enter the host is on a page this capability "
                    "does not know how to continue from."
                ),
            )
        return "continue", None
    if needs_handoff(hit):
        return "blocked", hit
    return "known_stop", hit


def looks_like_capability_screen(blob: str) -> bool:
    upper = blob.upper()
    markers = (
        "OPEN SUB-ACCOUNT",
        "SUB-ACCOUNT OPENED",
        "OFAC",
        "OPENING DEPOSIT",
        "CIF INQUIRY",
        "SUBMIT OPEN",
        "REGULAR SHARE",
    )
    return any(marker in upper for marker in markers)


def _clip(text: str, limit: int = 400) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"
