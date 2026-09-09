from __future__ import annotations

import argparse
import json
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
from cua.settings import DEFAULT_GOAL, DEFAULT_TARGET, HANDOFF_MAX_ATTEMPTS, MOCK_SERVER
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
    playwright = browser = None
    try:
        playwright, browser, page = open_browser(headed=args.headed)
        surface = Surface(page, allowed_origin=args.target)
        if args.cmd == "discover":
            result = discover(
                args.goal,
                args.target,
                surface,
                provider=args.provider,
                handoff_mode=args.handoff,
                handoff_max=args.handoff_max,
            )
        elif args.cmd == "smoke":
            result = smoke(args.goal, args.target, surface)
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
        log_event("cli.finish", status=result.status, outcome=result.outcome, run_dir=result.run_dir)
        print(json.dumps(result.model_dump(), indent=2))
        return 0 if result.status in {"success", "business_outcome"} else 1
    finally:
        if browser:
            browser.close()
        if playwright:
            playwright.stop()
        if mock:
            mock.terminate()


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

    args = parser.parse_args(argv)
    if args.handoff_max is not None and args.handoff_max < 1:
        parser.error("--handoff-max must be at least 1")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
