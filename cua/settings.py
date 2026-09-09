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
MOCK_SERVER = ROOT / "mock-core" / "server.py"

DEFAULT_MODELS = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-5",
    "gemini": "gemini-2.5-flash",
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
