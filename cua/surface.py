from __future__ import annotations

import base64
import re
from typing import Any
from playwright.sync_api import Frame, FrameLocator, Page, sync_playwright

from cua.log import LOGGER, log_event
from cua.policy import assert_action, assert_allowed
from cua.schema import AgentAction, Locator, Step

TIMEOUT = 8000
Scope = Page | FrameLocator


class Surface:
    def __init__(self, page: Page, allowed_origin: str) -> None:
        self.page = page
        self.allowed_origin = allowed_origin

    def goto(self, url: str) -> None:
        log_event("surface.goto", url=url)
        assert_allowed(url, self.allowed_origin)
        self.page.goto(url, wait_until="domcontentloaded")

    def observe(self) -> dict[str, Any]:
        assert_allowed(self.page.url, self.allowed_origin)
        LOGGER.debug("surface.observe url=%s", self.page.url)
        frames = []
        for frame in self.page.frames:
            name = frame.name or ("root" if frame == self.page.main_frame else "")
            frames.append(
                {
                    "name": name or "(unnamed)",
                    "url": frame.url,
                    "controls": _controls(frame),
                    "aria": _aria(frame),
                    "text": _text(frame)[:2500],
                }
            )
        jpeg = self.page.screenshot(type="jpeg", quality=42)
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "frames": frames,
            "jpeg_b64": base64.b64encode(jpeg).decode("ascii"),
            "png": jpeg,
        }

    def act(self, action: AgentAction) -> None:
        assert_allowed(self.page.url, self.allowed_origin)
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
        blob = _text(self._frame(frame_name)).upper()
        return all(item.upper() in blob for item in texts)

    def visible_text(self) -> str:
        return "\n".join(_text(frame) for frame in self.page.frames)

    def _click_locator(self, step: Step, loc: Locator, value: str | None) -> None:
        scope = self._scope(step.frame)
        if loc.by == "name" and loc.name:
            handle = scope.locator(f"[name='{loc.name}']").first
        elif loc.by == "role" and loc.role:
            kwargs = {}
            if loc.accessible_name:
                kwargs["name"] = re.compile(re.escape(loc.accessible_name.strip()), re.I)
            handle = scope.get_by_role(loc.role, **kwargs).first
        elif loc.by == "text" and loc.text:
            handle = scope.get_by_text(re.compile(re.escape(loc.text.strip()), re.I)).first
        else:
            raise RuntimeError(f"Empty locator on {step.id}")
        handle.wait_for(state="visible", timeout=TIMEOUT)
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
        candidates = []
        if action.field_name:
            candidates.append(scope.locator(f"[name='{action.field_name}']"))
            candidates.append(scope.locator(f"input[name='{action.field_name}'], select[name='{action.field_name}']"))
        if action.role:
            kwargs = {}
            if action.name:
                kwargs["name"] = re.compile(re.escape(action.name.strip()), re.I)
            candidates.append(scope.get_by_role(action.role, **kwargs))
        if action.name:
            trimmed = action.name.strip()
            candidates.append(scope.locator(f"input[type=submit][value*='{trimmed}']"))
            candidates.append(scope.get_by_text(re.compile(re.escape(trimmed), re.I)))
        for loc in candidates:
            try:
                loc.first.wait_for(state="visible", timeout=2500)
                return loc.first
            except Exception:
                continue
        raise RuntimeError(
            f"Could not find control action={action.action} frame={action.frame} "
            f"field_name={action.field_name} role={action.role} name={action.name}"
        )

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

    def _settle(self) -> None:
        self.page.wait_for_timeout(500)
        try:
            self.page.wait_for_selector("frame[name=main]", timeout=2000)
        except Exception:
            pass
        assert_allowed(self.page.url, self.allowed_origin)


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
              }))"""
        )
    except Exception:
        return []


def _aria(frame: Page | Frame) -> str:
    try:
        return frame.locator("body").aria_snapshot()[:4000]
    except Exception:
        return ""


def _text(frame: Page | Frame) -> str:
    try:
        return frame.inner_text("body")
    except Exception:
        return ""


def open_browser(headed: bool):
    log_event("browser.launch", headed=headed, timeout_ms=TIMEOUT)
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=not headed)
    page = browser.new_page(viewport={"width": 1100, "height": 800})
    page.set_default_timeout(TIMEOUT)
    log_event("browser.ready", headed=headed)
    return playwright, browser, page
