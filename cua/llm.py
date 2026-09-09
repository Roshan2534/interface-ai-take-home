from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any

from cua.settings import DEFAULT_MODELS, env_key

SYSTEM = """You operate a legacy bank back-office UI the way a teller would.
You receive the goal, an accessibility-oriented snapshot of each frame, and a screenshot.

Return ONE JSON object only, no markdown, with this shape:
{
  "thought": "short reason",
  "action": "click|fill|select|press|wait|done|fail",
  "frame": "root|main|menu|banner",
  "role": "button|textbox|link|combobox|menuitem|optional",
  "name": "visible or accessible name, optional",
  "field_name": "HTML name attribute if you see one (OPID, PSWD, MEMNO, PROD, DEPAMT)",
  "text": "string to type or option to select",
  "secret": "operator_id|password|null - use this instead of putting secrets in text",
  "bind": "member_id|product|deposit|null - mark typed values that should become artifact inputs",
  "key": "Enter|Tab|optional",
  "wait_ms": 400,
  "outputs": {"optional": "on done"},
  "checkpoint": ["visible text that proves success, on done"],
  "error": "on fail"
}

Rules:
- Before sign-on the page has no named frames; use frame "root".
- After sign-on this host uses a frameset: menu, banner, main. Do the work in "main" unless you need the menu.
- Prefer field_name when listed. Prefer role+name for buttons/links.
- Buttons and links must use action=click. Never use press for CONTINUE, INQUIRE, ENTER, or SUBMIT OPEN.
- action=press is only for a real keyboard key with no target control (set key, leave name empty).
- On OFAC or other interstitials, click the CONTINUE button in frame main, then fill PROD and DEPAMT and click SUBMIT OPEN.
- Sign on with secrets operator_id and password. Never echo the password.
- Do not invent URLs. Do not leave the allowlisted origin.
- Stop with action=done as soon as the goal's confirmation/result is visible, and fill outputs from the screen.
- If you are stuck after several tries, action=fail with a clear error.
"""

_ALIASES = {
    "openai": "openai",
    "gpt": "openai",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "gemini": "gemini",
    "google": "gemini",
}


@dataclass
class LLMConfig:
    provider: str
    model: str
    api_key: str


def _provider_keys() -> dict[str, str]:
    return {
        "openai": env_key("OPENAI_API_KEY"),
        "anthropic": env_key("ANTHROPIC_API_KEY") or env_key("CLAUDE_API_KEY"),
        "gemini": env_key("GEMINI_API_KEY") or env_key("GOOGLE_API_KEY"),
    }


def resolve_llm(provider: str | None = None) -> LLMConfig:
    from cua.log import log_event

    cfg = _pick_llm(provider)
    log_event("llm.resolve", provider=cfg.provider, model=cfg.model)
    return cfg


def _pick_llm(provider: str | None = None) -> LLMConfig:
    keys = _provider_keys()
    requested = (provider or env_key("LLM_PROVIDER")).lower()
    if requested:
        name = _ALIASES.get(requested)
        if not name:
            raise RuntimeError(
                f"Unknown LLM provider {requested!r}. Use openai, anthropic/claude, or gemini."
            )
        if not keys[name]:
            raise RuntimeError(_missing_key_message(name))
        return LLMConfig(name, _model_for(name), keys[name])

    present = [name for name, key in keys.items() if key]
    if not present:
        raise RuntimeError(
            "No LLM key found. Set one of OPENAI_API_KEY, ANTHROPIC_API_KEY, "
            "or GEMINI_API_KEY in .env (see .env.example)."
        )
    if len(present) > 1:
        chosen = next(name for name in ("openai", "anthropic", "gemini") if name in present)
        print(
            f"Multiple LLM keys found; using {chosen}. "
            "Set LLM_PROVIDER or pass --provider to pick another.",
            flush=True,
        )
        return LLMConfig(chosen, _model_for(chosen), keys[chosen])
    name = present[0]
    return LLMConfig(name, _model_for(name), keys[name])


def _model_for(provider: str) -> str:
    override = env_key("LLM_MODEL")
    if override:
        return override
    return DEFAULT_MODELS[provider]


def _missing_key_message(provider: str) -> str:
    names = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    return f"{names[provider]} is not set. Add it to .env."


def complete(messages: list[dict[str, Any]], cfg: LLMConfig | None = None) -> dict[str, Any]:
    from cua.log import log_event

    cfg = cfg or resolve_llm()
    log_event("llm.request", provider=cfg.provider, model=cfg.model, turns=len(messages))
    if cfg.provider == "openai":
        raw = _complete_openai(cfg, messages)
    elif cfg.provider == "anthropic":
        raw = _complete_anthropic(cfg, messages)
    else:
        raw = _complete_gemini(cfg, messages)
    parsed = _parse_json(raw)
    log_event("llm.response", provider=cfg.provider, action=parsed.get("action"), thought=parsed.get("thought"))
    return parsed


def _complete_openai(cfg: LLMConfig, messages: list[dict[str, Any]]) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=cfg.api_key)
    response = client.chat.completions.create(
        model=cfg.model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": SYSTEM}, *messages],
    )
    return response.choices[0].message.content or "{}"


def _complete_anthropic(cfg: LLMConfig, messages: list[dict[str, Any]]) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.api_key)
    converted: list[dict[str, Any]] = []
    for message in messages:
        converted.append(
            {"role": message["role"], "content": _anthropic_content(message["content"])}
        )
    response = client.messages.create(
        model=cfg.model,
        max_tokens=1024,
        temperature=0,
        system=SYSTEM,
        messages=converted,
    )
    parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
    return "\n".join(parts) or "{}"


def _complete_gemini(cfg: LLMConfig, messages: list[dict[str, Any]]) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=cfg.api_key)
    contents = []
    for message in messages:
        role = "user" if message["role"] == "user" else "model"
        contents.append(
            types.Content(role=role, parts=_gemini_parts(message["content"], types))
        )
    response = client.models.generate_content(
        model=cfg.model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    return response.text or "{}"


def _anthropic_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for part in content:
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            media, data = _split_data_url(part["image_url"]["url"])
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media, "data": data},
                }
            )
    return blocks


def _gemini_parts(content: Any, types: Any) -> list[Any]:
    if isinstance(content, str):
        return [types.Part.from_text(text=content)]
    parts = []
    for part in content:
        if part.get("type") == "text":
            parts.append(types.Part.from_text(text=part["text"]))
        elif part.get("type") == "image_url":
            media, data = _split_data_url(part["image_url"]["url"])
            parts.append(
                types.Part.from_bytes(data=base64.b64decode(data), mime_type=media)
            )
    return parts


def _split_data_url(url: str) -> tuple[str, str]:
    header, data = url.split(",", 1)
    media = "image/jpeg"
    if ":" in header:
        media = header.split(":", 1)[1].split(";", 1)[0] or media
    return media, data


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM did not return a JSON object")
    return parsed
