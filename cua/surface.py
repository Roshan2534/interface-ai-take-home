from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import Any
from playwright.sync_api import Frame, FrameLocator, Page, sync_playwright

from cua.log import LOGGER, log_event
from cua.policy import assert_action, assert_allowed, assert_page_for_act
from cua.schema import AgentAction, Locator, Step

TIMEOUT = 8000
Scope = Page | FrameLocator


class Surface:
    def __init__(
        self,
        page: Page,
        allowed_origin: str,
        highlight_ms: int = 0,
        dwell_ms: int = 0,
    ) -> None:
        self.page = page
        self.allowed_origin = allowed_origin
        self.highlight_ms = highlight_ms
        self.dwell_ms = dwell_ms

    def goto(self, url: str) -> None:
        log_event("surface.goto", url=url)
        assert_allowed(url, self.allowed_origin)
        self.page.goto(url, wait_until="domcontentloaded")

    def observe(self, *, rich: bool = True) -> dict[str, Any]:
        """Snapshot for the LLM (rich) or a cheap look at the live page.

        Accessibility snapshots on this frameset take tens of seconds per frame,
        so they are not collected. Controls + visible text + a JPEG are enough.
        """
        assert_allowed(self.page.url, self.allowed_origin, check_path=False)
        started = time.perf_counter()
        frames = []
        for frame in self.page.frames:
            if _skip_frame(frame):
                continue
            name = frame.name or ("root" if frame == self.page.main_frame else "")
            frames.append(
                {
                    "name": name or "(unnamed)",
                    "url": frame.url,
                    "controls": _controls(frame) if rich else [],
                    "aria": "",
                    "text": _text(frame)[:2500],
                }
            )
        jpeg = self.shot() if rich else b""
        log_event(
            "surface.observe",
            ms=int((time.perf_counter() - started) * 1000),
            rich=rich,
            frames=len(frames),
        )
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "frames": frames,
            "jpeg_b64": base64.b64encode(jpeg).decode("ascii") if jpeg else "",
            "png": jpeg,
        }

    def shot(self) -> bytes:
        started = time.perf_counter()
        try:
            data = self.page.screenshot(type="jpeg", quality=28, animations="disabled")
        except TypeError:
            data = self.page.screenshot(type="jpeg", quality=28)
        log_event("surface.shot", ms=int((time.perf_counter() - started) * 1000))
        return data

    def act(self, action: AgentAction) -> None:
        assert_page_for_act(self.page, self.allowed_origin)
        assert_action(action.action)
        risky = (action.name or "").strip().upper() in {"SUBMIT OPEN"}
        log_event(
            "surface.act",
            action=action.action,
            frame=action.frame,
            name=action.name,
            field_name=action.field_name,
            risky=risky,
        )
        if risky:
            log_event(
                "policy.risky_action",
                name=action.name,
                handling="allow_and_log",
                note="Opening a share is irreversible; production would confirm or escalate.",
            )
        if action.action == "wait":
            self.page.wait_for_timeout(max(action.wait_ms, 100))
            return
        if action.action == "press":
            self.page.keyboard.press(action.key or "Enter")
            self._settle()
            return

        target = self._locate(action)
        label = action.name or action.field_name or action.action
        self._spotlight(target, action.action, label)
        if action.action == "click":
            target.click(timeout=TIMEOUT, no_wait_after=True)
        elif action.action == "fill":
            target.fill(action.text or "", timeout=TIMEOUT)
        elif action.action == "select":
            value = action.text or ""
            try:
                target.select_option(value=value, timeout=TIMEOUT)
            except Exception:
                target.select_option(label=value, timeout=TIMEOUT)
        else:
            raise RuntimeError(f"Unsupported action {action.action}")
        self._settle()

    def replay_step(self, step: Step, value: str | None) -> None:
        log_event("surface.replay_step", step_id=step.id, action=step.action, frame=step.frame)
        assert_page_for_act(self.page, self.allowed_origin)
        fake = AgentAction(
            action=step.action,  # type: ignore[arg-type]
            frame=step.frame,
            field_name=next((loc.name for loc in step.locators if loc.by == "name"), None),
            role=next((loc.role for loc in step.locators if loc.by == "role"), None),
            name=next(
                (
                    loc.accessible_name or loc.text
                    for loc in step.locators
                    if loc.accessible_name or loc.text
                ),
                None,
            ),
            text=value,
            key=step.key,
            wait_ms=step.wait_ms or 400,
        )
        if step.action == "wait":
            self.act(fake)
            return
        if step.locators:
            last_error = None
            for loc in step.locators:
                try:
                    self._click_locator(step, loc, value)
                    self._settle()
                    return
                except Exception as exc:
                    last_error = exc
                    LOGGER.debug("locator_miss step=%s err=%s", step.id, exc)
            log_event("surface.replay_failed", step_id=step.id, error=str(last_error))
            raise RuntimeError(f"Replay failed at {step.id}: {last_error}")
        self.act(fake)

    def checkpoint(self, frame_name: str, texts: list[str]) -> bool:
        blob = _squash(_text(self._frame(frame_name)))
        return all(_squash(item) in blob for item in texts)

    def visible_text(self) -> str:
        return "\n".join(
            _text(frame) for frame in self.page.frames if not _skip_frame(frame)
        )

    def _click_locator(self, step: Step, loc: Locator, value: str | None) -> None:
        scope = self._scope(step.frame)
        handle = None
        if step.action == "click":
            label = loc.accessible_name or loc.text or loc.name
            if label:
                handle = self._find_submit(scope, label)
        if handle is None:
            if loc.by == "name" and loc.name:
                handle = scope.locator(f"[name='{loc.name}']").first
            elif loc.by == "role" and loc.role:
                kwargs = {}
                if loc.accessible_name:
                    kwargs["name"] = _flex_name(loc.accessible_name)
                handle = scope.get_by_role(loc.role, **kwargs).first
            elif loc.by == "text" and loc.text:
                handle = scope.get_by_text(_flex_name(loc.text)).first
            else:
                raise RuntimeError(f"Empty locator on {step.id}")
            handle.wait_for(state="visible", timeout=TIMEOUT)
        label = _human_label(step, loc)
        self._spotlight(handle, step.action, label)
        if step.action == "click":
            handle.click(timeout=TIMEOUT, no_wait_after=True)
        elif step.action == "fill":
            handle.fill(value or "", timeout=TIMEOUT)
        elif step.action == "select":
            try:
                handle.select_option(value=value or "", timeout=TIMEOUT)
            except Exception:
                handle.select_option(label=value or "", timeout=TIMEOUT)
        elif step.action == "press":
            self.page.keyboard.press(step.key or "Enter")

    def _locate(self, action: AgentAction):
        scope = self._scope(action.frame)
        if action.action == "click" and action.name:
            submit = self._find_submit(scope, action.name)
            if submit is not None:
                return submit
        candidates = []
        if action.field_name:
            candidates.append(scope.locator(f"[name='{action.field_name}']"))
            candidates.append(scope.locator(f"input[name='{action.field_name}'], select[name='{action.field_name}']"))
        if action.role:
            kwargs = {}
            if action.name:
                kwargs["name"] = _flex_name(action.name)
            candidates.append(scope.get_by_role(action.role, **kwargs))
        if action.name:
            trimmed = action.name.strip()
            hyphen = re.sub(r"\s+", "-", trimmed)
            spaced = re.sub(r"-", " ", trimmed)
            for variant in dict.fromkeys([trimmed, hyphen, spaced]):
                candidates.append(scope.locator(f"input[type=submit][value*='{variant}']"))
            if action.action not in {"fill", "select"}:
                candidates.append(scope.get_by_text(_flex_name(trimmed)))
        for loc in candidates:
            try:
                handle = loc.first
                handle.wait_for(state="visible", timeout=2500)
                if action.action in {"fill", "select"}:
                    tag = handle.evaluate("el => (el.tagName || '').toLowerCase()")
                    if tag not in {"input", "textarea", "select"}:
                        continue
                return handle
            except Exception:
                continue
        raise RuntimeError(
            f"Could not find control action={action.action} frame={action.frame} "
            f"field_name={action.field_name} role={action.role} name={action.name}"
        )

    def _find_submit(self, scope: Scope, name: str):
        want = re.sub(r"[^A-Z0-9]+", "", name.upper())
        if not want:
            return None
        loc = scope.locator("input[type=submit], input[type=button], button")
        try:
            loc.first.wait_for(state="attached", timeout=2500)
        except Exception:
            return None
        for i in range(loc.count()):
            item = loc.nth(i)
            blob = ""
            try:
                blob = " ".join(
                    part
                    for part in (item.get_attribute("value"), item.inner_text())
                    if part
                )
            except Exception:
                blob = item.get_attribute("value") or ""
            got = re.sub(r"[^A-Z0-9]+", "", blob.upper())
            if got == want or want in got or got in want:
                return item
        return None

    def _scope(self, name: str) -> Scope:
        key = (name or "root").lower()
        if key in {"root", "top", "_top", "page", ""}:
            return self.page
        selector = f"frame[name='{key}']"
        self.page.wait_for_selector(selector, timeout=TIMEOUT)
        return self.page.frame_locator(selector)

    def _frame(self, name: str) -> Page | Frame:
        key = (name or "root").lower()
        if key in {"root", "top", "_top", "page", ""}:
            return self.page
        frame = self.page.frame(name=key)
        if frame is None:
            self.page.wait_for_timeout(350)
            frame = self.page.frame(name=key)
        if frame is None:
            for item in self.page.frames:
                if (item.name or "").lower() == key:
                    return item
            return self.page
        return frame

    def _spotlight(self, handle, action: str, label: str) -> None:
        if self.highlight_ms <= 0 and self.dwell_ms <= 0:
            return
        verb = {
            "click": "CLICK",
            "fill": "TYPE",
            "select": "SELECT",
            "press": "PRESS",
        }.get(action, action.upper())
        caption = f"{verb}: {label}" if label else verb
        try:
            handle.evaluate(_SPOTLIGHT_JS, caption)
        except Exception:
            pass
        if self.highlight_ms > 0:
            self.page.wait_for_timeout(self.highlight_ms)

    def _settle(self) -> None:
        self.page.wait_for_timeout(120)
        try:
            self.page.wait_for_selector("frame[name=main]", timeout=1500)
        except Exception:
            pass
        assert_page_for_act(self.page, self.allowed_origin)
        if self.dwell_ms > 0:
            self.page.wait_for_timeout(self.dwell_ms)


def _controls(frame: Page | Frame) -> list[dict[str, str]]:
    try:
        return frame.evaluate(
            """() => [...document.querySelectorAll('input,select,button,a,textarea')]
              .slice(0, 40)
              .map(el => ({
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type') || '',
                name: el.getAttribute('name') || '',
                value: (el.getAttribute('type') === 'password' ? '' : (el.value || '')).slice(0, 60),
                text: ((el.innerText || el.value || el.textContent || '') + '')
                  .replace(/\\s+/g, ' ').trim().slice(0, 80)
              }))""",
            timeout=800,
        )
    except Exception:
        return []


_FIELD_LABELS = {
    "OPID": "OPERATOR ID",
    "PSWD": "PASSWORD",
    "MEMNO": "MEM NO",
    "PROD": "PRODUCT",
    "DEPAMT": "DEPOSIT",
}


def _flex_name(text: str) -> re.Pattern:
    core = re.sub(r"[\s\-]+", r"[\\s\\-]+", re.escape(text.strip()))
    return re.compile(core, re.I)


def _human_label(step: Step, loc: Locator) -> str:
    for item in (loc, *step.locators):
        if item.accessible_name:
            return item.accessible_name
        if item.text:
            return item.text
    if loc.name and loc.name.upper() in _FIELD_LABELS:
        return _FIELD_LABELS[loc.name.upper()]
    return loc.name or step.action


_SPOTLIGHT_JS = """(el, caption) => {
  el.scrollIntoView({block: 'nearest', inline: 'nearest'});
  el.style.setProperty('outline', '4px solid #ffcc00', 'important');
  el.style.setProperty('outline-offset', '3px', 'important');
  el.style.setProperty('box-shadow', '0 0 0 8px rgba(255, 204, 0, 0.45)', 'important');
  el.style.setProperty('background-color', '#fff3b0', 'important');
  const old = document.getElementById('cua-callout');
  if (old) old.remove();
  const tag = document.createElement('div');
  tag.id = 'cua-callout';
  tag.textContent = caption;
  tag.style.cssText = [
    'position:absolute',
    'z-index:2147483647',
    'background:#111',
    'color:#ffcc00',
    'font:bold 14px/1.2 monospace',
    'padding:6px 10px',
    'border:2px solid #ffcc00',
    'pointer-events:none',
    'white-space:nowrap',
  ].join(';');
  const r = el.getBoundingClientRect();
  const top = Math.max(4, r.top - 34);
  tag.style.left = Math.max(4, r.left + window.scrollX) + 'px';
  tag.style.top = (top + window.scrollY) + 'px';
  document.body.appendChild(tag);
}"""


def _squash(text: str) -> str:
    return " ".join((text or "").upper().split())


def _skip_frame(frame: Page | Frame) -> bool:
    url = (frame.url or "").strip()
    return url in {"", "about:blank", "about:srcdoc"}


def _text(frame: Page | Frame) -> str:
    try:
        return frame.inner_text("body", timeout=800)
    except Exception:
        return ""


def open_browser(headed: bool, video_dir: Path | None = None):
    log_event("browser.launch", headed=headed, timeout_ms=TIMEOUT, video=bool(video_dir))
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=not headed)
    context = None
    if video_dir:
        video_dir = Path(video_dir)
        video_dir.mkdir(parents=True, exist_ok=True)
        context = browser.new_context(
            viewport={"width": 1100, "height": 800},
            record_video_dir=str(video_dir),
            record_video_size={"width": 1100, "height": 800},
        )
        page = context.new_page()
    else:
        page = browser.new_page(viewport={"width": 1100, "height": 800})
    page.set_default_timeout(TIMEOUT)
    log_event("browser.ready", headed=headed, video_dir=str(video_dir) if video_dir else None)
    return playwright, browser, page, context
