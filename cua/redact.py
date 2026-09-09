from __future__ import annotations

import re

_SECRET_KEYS = ("password", "pswd", "token", "secret", "cookie", "hcusess")


def redact(value: str) -> str:
    text = value
    text = re.sub(r"(?i)(password|pswd|token|secret)\s*[:=]\s*\S+", r"\1: [redacted]", text)
    text = re.sub(r"HCUSESS=[0-9a-fA-F]+", "HCUSESS=[redacted]", text)
    return text


def redact_obj(obj: object) -> object:
    if isinstance(obj, dict):
        out = {}
        for key, val in obj.items():
            if str(key).lower() in _SECRET_KEYS or str(key).lower() == "text" and _looks_like_secret_field(obj):
                out[key] = "[redacted]"
            else:
                out[key] = redact_obj(val)
        return out
    if isinstance(obj, list):
        return [redact_obj(item) for item in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


def _looks_like_secret_field(obj: dict) -> bool:
    secret = str(obj.get("secret") or "")
    field = str(obj.get("field_name") or "")
    return secret == "password" or field.upper() in {"PSWD", "PASSWORD"}
