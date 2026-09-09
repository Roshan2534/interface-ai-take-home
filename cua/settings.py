from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DEFAULT_TARGET = "http://127.0.0.1:7878/"
DEFAULT_GOAL = (
    "Sign on to the member servicing console. Look up member 12345, "
    "read the current savings balance, open a new REGULAR SHARE sub-account "
    "with an opening deposit of 25.00, and return the member name, savings "
    "balance, new account number, and confirmation code."
)

OPERATOR_ID = os.getenv("HCU_OPERATOR_ID", "TELLER01")
OPERATOR_PASSWORD = os.getenv("HCU_PASSWORD", "train")
MAX_STEPS = int(os.getenv("CUA_MAX_STEPS", "24"))
EVIDENCE_DIR = ROOT / "evidence"
ARTIFACT_DIR = EVIDENCE_DIR / "artifacts"
RUNS_DIR = EVIDENCE_DIR / "runs"
DEMO_DIR = EVIDENCE_DIR / "demo"
MOCK_SERVER = ROOT / "mock-core" / "server.py"

DEFAULT_MODELS = {
    "openai": "gpt-6-astra",
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-3.8-flash",
}


def secrets() -> dict[str, str]:
    return {
        "operator_id": OPERATOR_ID,
        "password": OPERATOR_PASSWORD,
    }


def env_key(name: str) -> str:
    return (os.getenv(name) or "").strip()


HANDOFF_MODE = env_key("CUA_HANDOFF") or "wait"
OPERATOR_PORT = int(os.getenv("CUA_OPERATOR_PORT") or "7879")
HANDOFF_MAX_ATTEMPTS = int(os.getenv("CUA_HANDOFF_MAX_ATTEMPTS") or "2")

# Servicing flow only. /wire and /gl-override exist on the host but are not allowed.
_DEFAULT_ALLOWED_PATHS = (
    "/health,/login,/console,/frame,/inquiry,/open,/training,/logout,/timeout,/print"
)
_DEFAULT_DENIED_PATHS = "/wire,/gl-override"


def _csv_paths(raw: str, fallback: str) -> list[str]:
    text = (raw or fallback).strip()
    return [item.strip() for item in text.split(",") if item.strip()]


def allowed_path_prefixes() -> list[str]:
    return _csv_paths(env_key("CUA_ALLOWED_PATH_PREFIXES"), _DEFAULT_ALLOWED_PATHS)


def denied_path_prefixes() -> list[str]:
    return _csv_paths(env_key("CUA_DENIED_PATH_PREFIXES"), _DEFAULT_DENIED_PATHS)


def watch_pace(show: bool) -> tuple[int, int]:
    """Pause so a person can see the control. Returns (highlight_ms, dwell_ms).

    On when the browser is headed or a video is recording. Headless replay
    without --record stays fast unless CUA_PACE_MS is set.
    """
    raw_dwell = os.getenv("CUA_PACE_MS")
    if raw_dwell is not None and raw_dwell.strip() != "":
        dwell = max(0, int(raw_dwell))
    else:
        dwell = 2500 if show else 0
    raw_hi = os.getenv("CUA_HIGHLIGHT_MS")
    if raw_hi is not None and raw_hi.strip() != "":
        highlight = max(0, int(raw_hi))
    elif dwell:
        highlight = 1100
    else:
        highlight = 0
    return highlight, dwell
