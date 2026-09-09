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
from cua.policy import origin
from cua.settings import DEFAULT_GOAL, DEFAULT_TARGET, MOCK_SERVER
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
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_SERVER), "--host", host, "--port", port],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        try:
            urlopen(health, timeout=0.4)
            print(f"Started mock host at {origin(target)}")
            return proc
        except (URLError, OSError, TimeoutError):
            time.sleep(0.15)
    proc.kill()
    raise RuntimeError(f"Could not start mock-core on {host}:{port}")


def _run(args: argparse.Namespace) -> int:
    mock = ensure_mock(args.target)
    playwright = browser = None
    try:
        playwright, browser, page = open_browser(headed=args.headed)
        surface = Surface(page, allowed_origin=args.target)
        if args.cmd == "discover":
            result = discover(args.goal, args.target, surface, provider=args.provider)
        elif args.cmd == "smoke":
            result = smoke(args.goal, args.target, surface)
        else:
            capability = load_capability(Path(args.artifact))
            result = replay(capability, args.target, dict(args.input or []), surface)
        print(json.dumps(result.model_dump(), indent=2))
        return 0 if result.status == "success" else 1
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
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
