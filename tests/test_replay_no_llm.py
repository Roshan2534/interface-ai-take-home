"""Replay must not import a model client. Run: python3 tests/test_replay_no_llm.py"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cua.prove import check_replay_imports  # noqa: E402


def main() -> int:
    proof = check_replay_imports()
    print(json.dumps(proof, indent=2))
    if not proof["ok"]:
        print("FAIL: replay can reach a model client", file=sys.stderr)
        return 1
    print("ok: cua.replay cannot import cua.llm / openai / anthropic / gemini")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
