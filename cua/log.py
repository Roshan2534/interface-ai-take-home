"""Run-scoped logs. Call log_event() from every cua module, including future work.

Each run writes:
  evidence/runs/<kind>-<stamp>/system.log    human-readable
  evidence/runs/<kind>-<stamp>/system.jsonl  one JSON object per event
plus stderr at INFO. Secrets are redacted before write.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from cua.redact import redact_obj

LOGGER = logging.getLogger("cua")


class _JsonlHandler(logging.Handler):
    def __init__(self, path: Path) -> None:
        super().__init__(level=logging.DEBUG)
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record: logging.LogRecord) -> None:
        payload = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update({key: val for key, val in extra.items() if key != "ts"})
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(redact_obj(payload), default=str) + "\n")
        except Exception:
            pass


def setup_logging(run_dir: Path | None = None) -> logging.Logger:
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.handlers.clear()
    LOGGER.propagate = False
    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.INFO)
    stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    LOGGER.addHandler(stream)
    if run_dir:
        run_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(run_dir / "system.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        LOGGER.addHandler(file_handler)
        LOGGER.addHandler(_JsonlHandler(run_dir / "system.jsonl"))
    LOGGER.debug("logging_ready run_dir=%s", run_dir)
    return LOGGER


def log_event(event: str, **fields: Any) -> None:
    safe = redact_obj(fields)
    extra = {"event": event, "fields": safe if isinstance(safe, dict) else {}}
    LOGGER.info(
        "%s %s",
        event,
        json.dumps(safe, default=str) if safe else "",
        extra=extra,
    )
