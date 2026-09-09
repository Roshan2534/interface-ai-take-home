from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cua.redact import redact_obj
from cua.settings import RUNS_DIR


class RunLog:
    def __init__(self, goal: str, target: str, kind: str = "discover") -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.dir = RUNS_DIR / f"{kind}-{stamp}"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "steps").mkdir(exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self._write(
            "run.json",
            {"kind": kind, "goal": goal, "target": target, "started_at": stamp},
        )

    def event(self, **payload: Any) -> None:
        safe = redact_obj(payload)
        assert isinstance(safe, dict)
        self.events.append(safe)
        from cua.log import log_event

        log_event("run.event", **safe)
        with (self.dir / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe) + "\n")

    def save_step(self, index: int, payload: dict[str, Any], screenshot: bytes | None) -> None:
        from cua.log import LOGGER

        self._write(f"steps/{index:02d}.json", redact_obj(payload))
        LOGGER.debug("run.save_step index=%s bytes=%s", index, len(screenshot or b""))
        if screenshot:
            (self.dir / "steps" / f"{index:02d}.png").write_bytes(screenshot)

    def finish(self, result: dict[str, Any]) -> None:
        from cua.log import log_event

        log_event("run.finish", **{k: result.get(k) for k in ("status", "outcome", "step_id", "reason")})
        self._write("result.json", redact_obj(result))

    def _write(self, rel: str, payload: Any) -> None:
        path = self.dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
