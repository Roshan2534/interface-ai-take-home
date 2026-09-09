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
    flat = " ".join(blob.split())
    upper = flat.upper()

    if "RECORD NOT FOUND" in upper:
        return Diagnosis(
            status="business_outcome",
            outcome="not_found",
            expected="Member detail page with accounts and OPEN SUB-ACCOUNT",
            observed=_clip(flat),
            reason=(
                "CIF returned a known miss. This is a business outcome for the caller, "
                "not a locator crash. Replay stops instead of opening a sub-account."
            ),
        )
    if "SECURITY VIOLATION" in upper or "NOT AUTHORIZED" in upper:
        return Diagnosis(
            status="business_outcome",
            outcome="permission_denied",
            expected="Open-sub-account form after OFAC continue",
            observed=_clip(flat),
            reason=(
                "Operator is not authorized for OPN-SUB on this member. "
                "Reported as permission_denied, not a host outage."
            ),
        )
    if "VALIDATION" in upper or "MEMBER NUMBER IS REQUIRED" in upper:
        return Diagnosis(
            status="business_outcome",
            outcome="validation",
            expected="Accepted input and the next host screen",
            observed=_clip(flat),
            reason="Host rejected the field values. Caller should fix inputs and retry.",
        )
    if "SESSION TIMEOUT" in upper:
        return Diagnosis(
            status="failed",
            outcome="timeout",
            expected="Live servicing session still signed on",
            observed=_clip(flat),
            reason="Session expired. Hard stop; sign on again before retrying.",
        )
    if "HOST MESSAGE 12E" in upper or "ADDRESS CHANGE PENDING" in upper:
        return Diagnosis(
            status="failed",
            outcome="unexpected_dialog",
            expected="Member detail (artifact was recorded without this interstitial)",
            observed=_clip(flat),
            reason=(
                "Recoverable host interstitial that this capability did not record. "
                "Stop rather than clicking through an unknown dialog."
            ),
        )
    return None


def _clip(text: str, limit: int = 400) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"
