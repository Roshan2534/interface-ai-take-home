from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import urlopen

from cua.agent import discover, replay, smoke
from cua.artifact import load_capability
from cua.log import log_event, setup_logging
from cua.policy import origin
from cua.settings import (
    DEFAULT_GOAL,
    DEFAULT_TARGET,
    DEMO_DIR,
    HANDOFF_MAX_ATTEMPTS,
    MOCK_SERVER,
    watch_pace,
)
from cua.surface import Surface, open_browser


def ensure_mock(target: str) -> subprocess.Popen | None:
    parsed = urlparse(target)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    health = urljoin(target if target.endswith("/") else target + "/", "health")
    try:
        urlopen(health, timeout=1)
        return None
    except (URLError, OSError, TimeoutError):
        pass
    port = str(parsed.port or 7878)
    host = parsed.hostname or "127.0.0.1"
    log_event("mock.starting", host=host, port=port)
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_SERVER), "--host", host, "--port", port],
        stdout=subprocess.DEVNULL,
    )
    for _ in range(40):
        try:
            urlopen(health, timeout=0.4)
            print(f"Started mock host at {origin(target)}")
            log_event("mock.ready", url=origin(target))
            return proc
        except (URLError, OSError, TimeoutError):
            time.sleep(0.15)
    proc.kill()
    raise RuntimeError(f"Could not start mock-core on {host}:{port}")


def _run(args: argparse.Namespace) -> int:
    setup_logging()
    log_event(
        "cli.start",
        cmd=args.cmd,
        target=args.target,
        headed=args.headed,
        handoff=getattr(args, "handoff", None),
        handoff_max=getattr(args, "handoff_max", None),
    )
    mock = ensure_mock(args.target)
    playwright = browser = page = context = None
    video_dir = None
    if getattr(args, "record", False) or args.cmd == "record-demo":
        video_dir = DEMO_DIR / "raw"
        log_event("video.start", dir=str(video_dir))
    try:
        playwright, browser, page, context = open_browser(
            headed=args.headed, video_dir=video_dir
        )
        highlight_ms, dwell_ms = watch_pace(bool(args.headed or video_dir))
        log_event("surface.pace", highlight_ms=highlight_ms, dwell_ms=dwell_ms)
        surface = Surface(
            page,
            allowed_origin=args.target,
            highlight_ms=highlight_ms,
            dwell_ms=dwell_ms,
        )
        if args.cmd == "discover":
            result = discover(
                args.goal,
                args.target,
                surface,
                provider=args.provider,
                handoff_mode=args.handoff,
                handoff_max=args.handoff_max,
            )
            code = 0 if result.status in {"success", "business_outcome"} else 1
            print(json.dumps(result.model_dump(), indent=2))
        elif args.cmd == "smoke":
            result = smoke(args.goal, args.target, surface)
            code = 0 if result.status in {"success", "business_outcome"} else 1
            print(json.dumps(result.model_dump(), indent=2))
        elif args.cmd == "record-demo":
            result = _record_demo(surface, args.target)
            code = 0 if result.get("ok") else 1
            print(json.dumps(result, indent=2))
        else:
            capability = load_capability(Path(args.artifact))
            result = replay(
                capability,
                args.target,
                dict(args.input or []),
                surface,
                handoff_mode=args.handoff,
                handoff_max=args.handoff_max,
            )
            code = 0 if result.status in {"success", "business_outcome"} else 1
            print(json.dumps(result.model_dump(), indent=2))
            log_event("cli.finish", status=result.status, outcome=result.outcome, run_dir=result.run_dir)
        return code
    finally:
        video = page.video if page is not None else None
        if page is not None:
            page.close()
        if context is not None:
            context.close()
        saved = None
        if video is not None:
            try:
                saved = _keep_demo_video(Path(video.path()), args.cmd)
            except Exception as exc:
                log_event("video.save_failed", error=str(exc))
        if browser:
            browser.close()
        if playwright:
            playwright.stop()
        if mock:
            mock.terminate()
        if saved:
            print(f"Saved screen recording to {saved}", flush=True)


def _keep_demo_video(src: Path, cmd: str) -> Path | None:
    if not src.exists():
        log_event("video.missing", src=str(src))
        return None
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    name = "replay-tour.webm" if cmd == "record-demo" else f"{cmd}.webm"
    dest = DEMO_DIR / name
    shutil.copy2(src, dest)
    log_event("video.saved", src=str(src), dest=str(dest), bytes=dest.stat().st_size)
    return dest


def _record_demo(surface, target: str) -> dict:
    capability = load_capability(Path("evidence/artifacts/hcu-open-savings-subaccount.json"))
    segments = []
    plan = (
        ("10001", "off", "success"),
        ("99999", "off", "business_outcome"),
        ("77777", "simulate", "success"),
    )
    for member, mode, expect in plan:
        log_event("demo.segment", member_id=member, handoff=mode)
        print(f"\n--- demo member {member} ---\n", flush=True)
        result = replay(
            capability,
            target,
            {"member_id": member},
            surface,
            handoff_mode=mode,
        )
        segments.append(
            {
                "member_id": member,
                "status": result.status,
                "outcome": result.outcome,
                "expected_status": expect,
            }
        )
        log_event(
            "demo.segment_done",
            member_id=member,
            status=result.status,
            outcome=result.outcome,
        )
    ok = all(row["status"] == row["expected_status"] for row in segments)
    return {"ok": ok, "segments": segments}


def _parse_input(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError("expected name=value")
    key, value = raw.split("=", 1)
    return key, value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Computer-use discovery and replay against a live UI"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--target", default=DEFAULT_TARGET)
    common.add_argument("--headed", action="store_true", help="show the browser")
    common.add_argument(
        "--handoff",
        choices=["off", "wait", "simulate"],
        default=None,
        help="HITL: wait (press Enter in the terminal to hand control back), simulate CONTINUE, or disable",
    )
    common.add_argument(
        "--handoff-max",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Hand off at most N times; abort if the last return is still unknown "
            f"(default {HANDOFF_MAX_ATTEMPTS}, or CUA_HANDOFF_MAX_ATTEMPTS)"
        ),
    )
    common.add_argument(
        "--record",
        action="store_true",
        help="record a Playwright video of the browser (saved under evidence/demo/)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    discover_p = sub.add_parser("discover", parents=[common], help="LLM-driven happy path")
    discover_p.add_argument("--goal", default=DEFAULT_GOAL)
    discover_p.add_argument(
        "--provider",
        choices=["openai", "anthropic", "claude", "gemini", "google"],
        default=None,
        help="LLM vendor; default is whichever API key is set",
    )

    smoke_p = sub.add_parser(
        "smoke", parents=[common], help="scripted locator check (not discovery)"
    )
    smoke_p.add_argument("--goal", default=DEFAULT_GOAL)

    replay_p = sub.add_parser(
        "replay", parents=[common], help="replay a saved artifact with no LLM"
    )
    replay_p.add_argument(
        "--artifact",
        default="evidence/artifacts/hcu-open-savings-subaccount.json",
    )
    replay_p.add_argument("--input", action="append", type=_parse_input, default=[])

    sub.add_parser(
        "record-demo",
        parents=[common],
        help="record one video: replay 10001, 99999 not-found, 77777 HITL simulate",
    )

    args = parser.parse_args(argv)
    if args.handoff_max is not None and args.handoff_max < 1:
        parser.error("--handoff-max must be at least 1")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
