from __future__ import annotations

import html
import json
import select
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cua.log import log_event
from cua.redact import redact_obj
from cua.schema import AgentAction
from cua.settings import OPERATOR_PORT


def _handoff_instructions(
    *,
    reason: str,
    desk_url: str,
    attempt: int = 1,
    max_attempts: int = 2,
    restated: bool = False,
) -> str:
    last_chance = attempt == max_attempts
    if restated:
        header = (
            f"STILL CANNOT PROCEED (attempt {attempt} of {max_attempts})\n"
            "You pressed Enter, but the agent still does not know how to\n"
            "continue from the current screen (nothing changed, or the screen\n"
            "is not one this capability recorded)."
        )
    else:
        header = "HUMAN INTERVENTION REQUIRED"
    last = (
        "\nThis is the last chance. If you press Enter and the screen is\n"
        "still not one the agent can continue from, the run will abort.\n"
        if last_chance
        else ""
    )
    return (
        "\n"
        "============================================================\n"
        f"{header}\n"
        "============================================================\n"
        "Automation has paused. You now own the live Chromium window\n"
        "(same session, do not log in again, do not open a new browser).\n"
        f"\nWhy: {reason}\n"
        f"{last}"
        "\n"
        "How to hand control back to the agent:\n"
        "  1. Put the host on a screen this capability can continue from.\n"
        "  2. Return to this terminal.\n"
        "  3. Press Enter to resume automation from the current screen.\n"
        "     Type abort then Enter to stop the run instead.\n"
        "\n"
        f"Optional: open {desk_url} and click RESUME AUTOMATION\n"
        "(or ABORT RUN) if you would rather not use the terminal.\n"
        "============================================================\n"
    )


def _exhausted_message(*, reason: str, observed: str, max_attempts: int = 2) -> str:
    times = "once" if max_attempts == 1 else f"{max_attempts} times"
    return (
        "\n"
        "============================================================\n"
        f"ABORTING: still cannot continue after {max_attempts} handoff(s)\n"
        "============================================================\n"
        f"Control was handed to you {times}. After you pressed Enter the last\n"
        "time, the live screen was still not one this capability knows how\n"
        "to proceed from (nothing useful changed, or a different unexpected screen).\n"
        f"\nLast problem: {reason}\n"
        f"Last screen: {observed[:300]}\n"
        "The run will stop. Re-run after the host is on a known screen.\n"
        "============================================================\n"
    )


class SessionControl:
    """Who owns the live Playwright page. Automation and human never drive it at once."""

    def __init__(
        self,
        surface,
        run_dir: Path,
        mode: str = "wait",
        max_attempts: int | None = None,
    ) -> None:
        from cua.settings import HANDOFF_MAX_ATTEMPTS

        self.surface = surface
        self.run_dir = run_dir
        self.mode = mode  # off | wait | simulate
        self.max_attempts = max(1, int(max_attempts or HANDOFF_MAX_ATTEMPTS))
        self.controller = "automation"
        self.interventions: list[dict[str, Any]] = []
        self._resume = threading.Event()
        self._abort = threading.Event()
        self._shot = run_dir / "handoff-shot.jpg"
        self._request: dict[str, Any] = {}
        self.human_events: list[dict[str, Any]] = []
        self.desk_port = OPERATOR_PORT
        self._on_nav = None
        self.blocked_passes = 0
        log_event(
            "control.init",
            controller=self.controller,
            mode=self.mode,
            desk_port=self.desk_port,
            max_attempts=self.max_attempts,
        )

    def assert_automation(self) -> None:
        if self.controller != "automation":
            raise RuntimeError(f"automation blocked; controller={self.controller}")

    def escalate(
        self,
        *,
        reason: str,
        goal: str,
        step_id: str | None,
        observed: str,
        screenshot: bytes | None,
        attempt: int = 1,
        max_attempts: int | None = None,
        restated: bool = False,
    ) -> str:
        """Pause automation, give the live session to a human, wait, then return resume|abort."""
        if self.mode == "off":
            log_event("handoff.skipped", reason=reason, mode="off")
            return "abort"

        limit = max_attempts if max_attempts is not None else self.max_attempts

        self.controller = "human"
        if screenshot:
            self._shot.write_bytes(screenshot)
        self._request = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "goal": goal,
            "step_id": step_id,
            "observed": observed[:800],
            "controller": self.controller,
            "live_session": "same Playwright page / Chromium instance",
            "attempt": attempt,
            "max_attempts": limit,
            "restated": restated,
        }
        log_event("handoff.start", **self._request)
        self._install_recorder()
        self._resume.clear()
        self._abort.clear()

        desk = OperatorDesk(self)
        desk.start()
        desk_url = f"http://127.0.0.1:{self.desk_port}/"
        log_event("handoff.desk", url=desk_url, mode=self.mode, attempt=attempt, restated=restated)
        message = _handoff_instructions(
            reason=reason,
            desk_url=desk_url,
            attempt=attempt,
            max_attempts=limit,
            restated=restated,
        )
        log_event("handoff.instructions_shown", desk_url=desk_url, attempt=attempt, restated=restated)
        print(message, flush=True)

        if self.mode == "simulate":
            self._simulate_operator()
            self._resume.set()
        else:
            self._wait_for_operator()

        decision = "abort" if self._abort.is_set() else "resume"
        if decision == "abort":
            print(
                "\nHANDOFF: run aborted. Control was not returned to the agent.\n",
                flush=True,
            )
        else:
            print(
                "\nHANDOFF: handing control back to the agent. "
                "Automation will continue from this screen.\n",
                flush=True,
            )
        record = {
            **self._request,
            "decision": decision,
            "human_events": redact_obj(self.human_events),
            "ended_at": datetime.now(timezone.utc).isoformat(),
        }
        self.interventions.append(record)
        path = self.run_dir / "handoff.json"
        path.write_text(json.dumps(redact_obj(self.interventions), indent=2, default=str), encoding="utf-8")
        desk.stop()
        self._remove_recorder()
        self.controller = "automation"
        log_event(
            "handoff.end",
            decision=decision,
            human_event_count=len(self.human_events),
            controller=self.controller,
        )
        return decision

    def _simulate_operator(self) -> None:
        log_event("handoff.simulate", action="click CONTINUE on live session")
        self.human_events.append(
            {
                "kind": "simulate",
                "action": "click",
                "name": "CONTINUE",
                "note": "Test operator on the same Playwright page, not a new session",
            }
        )
        try:
            self.surface.act(
                AgentAction(action="click", frame="main", role="button", name="CONTINUE")
            )
        except Exception as exc:
            log_event("handoff.simulate_failed", error=str(exc))

    def _wait_for_operator(self) -> None:
        tty = sys.stdin.isatty()
        log_event("handoff.wait", stdin_tty=tty)
        while not self._resume.is_set() and not self._abort.is_set():
            if not tty:
                self._resume.wait(timeout=0.4)
                continue
            try:
                ready, _, _ = select.select([sys.stdin], [], [], 0.4)
            except (OSError, ValueError) as exc:
                log_event("handoff.stdin_select_failed", error=str(exc))
                self._resume.wait(timeout=0.4)
                continue
            if not ready:
                continue
            line = sys.stdin.readline()
            text = (line or "").strip().lower()
            log_event("handoff.stdin", text=text[:40] or "<enter>")
            if text in {"abort", "a", "q", "n"}:
                self._abort.set()
                log_event("operator.terminal_abort")
            else:
                self._resume.set()
                log_event("operator.terminal_resume")

    def _install_recorder(self) -> None:
        page = self.surface.page

        def on_nav(frame) -> None:
            if self.controller != "human":
                return
            self.human_events.append(
                {"kind": "navigate", "url": frame.url, "name": frame.name}
            )
            log_event("human.navigate", url=frame.url, frame=frame.name)

        try:
            page.expose_binding(
                "cuaRecord",
                lambda _source, data: self._on_human(data),
            )
        except Exception:
            pass
        script = """() => {
          if (window.__cuaRec) return;
          window.__cuaRec = true;
          const send = (d) => { try { window.cuaRecord(d); } catch (e) {} };
          document.addEventListener('click', (e) => {
            const t = e.target;
            send({
              kind: 'click',
              tag: t && t.tagName,
              name: t && t.getAttribute && t.getAttribute('name'),
              text: ((t && (t.innerText || t.value)) || '').replace(/\\s+/g,' ').trim().slice(0,80)
            });
          }, true);
          document.addEventListener('change', (e) => {
            const t = e.target;
            const sensitive = ((t && t.getAttribute('name')) || '').toUpperCase() === 'PSWD';
            send({
              kind: 'change',
              tag: t && t.tagName,
              name: t && t.getAttribute && t.getAttribute('name'),
              value: sensitive ? '[redacted]' : String((t && t.value) || '').slice(0,80)
            });
          }, true);
        }"""
        self._on_nav = on_nav
        try:
            page.on("framenavigated", on_nav)
        except Exception:
            pass
        for frame in page.frames:
            try:
                frame.evaluate(script)
            except Exception as exc:
                log_event("handoff.recorder_frame_failed", error=str(exc), url=frame.url)

    def _remove_recorder(self) -> None:
        if self._on_nav is None:
            return
        try:
            self.surface.page.remove_listener("framenavigated", self._on_nav)
            log_event("handoff.recorder_removed")
        except Exception as exc:
            log_event("handoff.recorder_remove_failed", error=str(exc))
        self._on_nav = None

    def _on_human(self, data: Any) -> None:
        if self.controller != "human":
            return
        if not isinstance(data, dict):
            return
        self.human_events.append(data)
        log_event("human.action", **data)


class OperatorDesk:
    def __init__(self, control: SessionControl) -> None:
        self.control = control
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        control = self.control

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                log_event("operator.http", message=fmt % args)

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/shot.jpg" and control._shot.exists():
                    body = control._shot.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                req = control._request
                restated = bool(req.get("restated"))
                attempt = req.get("attempt", 1)
                title = (
                    f"STILL CANNOT PROCEED — attempt {attempt}"
                    if restated
                    else "HUMAN INTERVENTION REQUIRED"
                )
                last = (
                    "<p><b>Last chance.</b> If you resume and the screen is still unknown, the run will abort.</p>"
                    if restated
                    else ""
                )
                page = f"""<!DOCTYPE html>
<html><head><title>HCU operator desk</title>
<style>
 body {{ font-family: Tahoma, sans-serif; font-size: 13px; background:#c0c0c8; }}
 .navy {{ background:#000080; color:#fff; padding:8px; font-weight:bold; }}
 .warn {{ background:#ffcc66; border:2px solid #996600; padding:8px; margin:8px 0; }}
 img {{ max-width: 100%; border: 1px solid #404040; }}
</style></head>
<body>
<div class="navy">{html.escape(str(title))}</div>
<p>Controller: <b>{html.escape(control.controller)}</b> (same Chromium session the agent was using)</p>
<div class="warn"><b>Why paused:</b> {html.escape(str(req.get('reason','')))}<br>
<b>Goal:</b> {html.escape(str(req.get('goal','')))}<br>
<b>Step:</b> {html.escape(str(req.get('step_id','')))}</div>
{last}
<p><b>How to hand control back to the agent:</b> put the host on a screen this capability can continue from, then either press <b>Enter in the terminal</b>, or click <b>RESUME AUTOMATION</b> below. Click ABORT RUN only if the session should stop.</p>
<p><img src="/shot.jpg" alt="paused screen"></p>
<form method="POST" action="/resume"><input type="submit" value="  RESUME AUTOMATION (hand back to agent)  "></form>
<form method="POST" action="/abort"><input type="submit" value="  ABORT RUN  "></form>
</body></html>
"""
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                _ = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
                if path == "/abort":
                    control._abort.set()
                    log_event("operator.abort")
                else:
                    control._resume.set()
                    log_event("operator.resume")
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()

        last_error: OSError | None = None
        for port in range(OPERATOR_PORT, OPERATOR_PORT + 10):
            try:
                self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                self.control.desk_port = port
                log_event("operator.desk_bound", port=port)
                break
            except OSError as exc:
                last_error = exc
                log_event("operator.desk_bind_failed", port=port, error=str(exc))
                self.server = None
        if not self.server:
            raise RuntimeError(f"Could not bind operator desk: {last_error}")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            log_event("operator.desk_stopped")
