# Horizon Credit Union computer-use take-home

Record a UI flow once with an LLM, save it as a typed capability, then replay it without the model.

Target is a local legacy-style member console (`mock-core/`), not a real bank. Use a virtualenv so Playwright and the LLM SDKs stay isolated.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env
```

Discovery needs **one** key in `.env`:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY` (Claude)
- `GEMINI_API_KEY`

If several are set, OpenAI wins unless you set `LLM_PROVIDER` or pass `--provider`. Optional `LLM_MODEL` overrides the default (`gpt-6-astra`, `claude-sonnet-5`, `gemini-3.8-flash`).

Mock operator: `TELLER01` / `train`. The CLI starts `mock-core/server.py` on `http://127.0.0.1:7878/` if `/health` is down. You do not need a second terminal.

## Demo path

1. LLM discovery (live UI, writes the artifact):

```bash
python3 -m cua discover --headed
```

Same run with an explicit goal and target:

```bash
python3 -m cua discover \
  --target http://127.0.0.1:7878/ \
  --goal "Sign on. Look up member 12345, read the savings balance, open a REGULAR SHARE sub-account with 25.00, and return the new account number and confirmation."
```

2. Deterministic replay, no LLM, different member:

```bash
python3 -m cua replay --headed --input member_id=10001
```

3. Replay that hits a known CIF miss (business outcome, not a crash):

```bash
python3 -m cua replay --headed --input member_id=99999
```

Expected: stop after Inquire, `status=business_outcome`, `outcome=not_found`.

4. Human-in-the-loop when the host shows a screen the artifact never recorded (member `77777`):

```bash
python3 -m cua replay --headed --handoff wait --input member_id=77777
```

See **Human intervention** below for what the terminal prints and how to hand control back to the agent.

## Human intervention

Use `--headed` so Chromium is visible. When the run hits a recoverable unknown screen (for example `HOST MESSAGE 12E` on member `77777`), automation **stops driving that same window**. It does not open a second browser or ask you to log in again.

The terminal prints `HUMAN INTERVENTION REQUIRED` plus the reason, then how to resume:

1. Finish the host step in the open Chromium window (for `77777`, click **CONTINUE** on the address-change interstitial).
2. Return to the terminal.
3. **Press Enter** to hand control back to the agent. Automation continues from the current screen.
4. Type `abort` then Enter if the run should stop instead.

Optional: open `http://127.0.0.1:7879/` and click **RESUME AUTOMATION** (or **ABORT RUN**). Same effect as the terminal.

### Retry limit (customizable, default 2)

If you press Enter without changing the screen, or you leave the host on a page this capability does not know, the agent **does not keep guessing**. It prints `STILL CANNOT PROCEED` with the current problem and hands the live window back to you again.

Default: **2** handoffs. After you return control the second time and the screen is still unknown, the run aborts (`outcome=handoff_exhausted`).

Set the limit:

```bash
python3 -m cua replay --headed --handoff wait --handoff-max 2 --input member_id=77777
```

Or in `.env` (CLI wins if both are set):

```
CUA_HANDOFF_MAX_ATTEMPTS=2
```

`--handoff-max` must be at least 1. Logged as `handoff.attempt`, `handoff.recheck`, `handoff.still_blocked`, `handoff.exhausted`.

A CIF miss (`99999`) does **not** hand off. That is a known business outcome (`not_found`), not a human step.

`--handoff off|wait|simulate` also works on discover (stuck loops / agent fail). Default mode is `wait` (`CUA_HANDOFF`). Headless `--handoff wait` still pauses, but you will not see the host UI unless you use `--headed`.

Unattended evidence path (test operator clicks CONTINUE on the live page, no waiting):

```bash
python3 -m cua replay --handoff simulate --input member_id=77777
```

## Logging

Every command writes stderr plus, once a run directory exists:

- `evidence/runs/<kind>-<stamp>/system.log`
- `evidence/runs/<kind>-<stamp>/system.jsonl`

Events are named `module.verb` (`replay.start`, `handoff.start`, `surface.act`, `llm.request`, …). Secrets are redacted. Future work should keep calling `log_event()` from `cua.log` rather than `print`.

## Safety allowlist

The agent may only drive `http://127.0.0.1:7878/` (the `--target` origin). It may only `click`, `fill`, `select`, `press`, and `wait`.

On that host, routes are allowlisted too. Member servicing (`/inquiry`, `/open`, frames, login) is allowed. The mock menu also has two real programs the agent **must not** drive:

- `/wire` — Wire Transfer
- `/gl-override` — GL Override

A person can open those in the browser. If the agent lands there, the next action is refused (`policy.denied_path`). Configure in `.env`:

```
CUA_ALLOWED_PATH_PREFIXES=/health,/login,/console,/frame,/inquiry,/open,/training,/logout,/timeout,/print
CUA_DENIED_PATH_PREFIXES=/wire,/gl-override
```

**SUBMIT OPEN** still runs automatically on replay and is logged as risky. The irreversible click is allowed because the capability was already recorded; discovery/HITL covers unknown screens.

## Without live LLM / extra services

Replay and smoke do not call an LLM. They still need the local mock (auto-started) and Chromium.

```bash
python3 -m cua replay --input member_id=10001
python3 -m cua replay --input member_id=99999
python3 -m cua smoke
```

`smoke` is a scripted locator check, not discovery. Do not treat it as the LLM run.

## Evidence

Curated pack (what to read in the repo):

| Path | What |
|---|---|
| `REPORT.md` | design write-up (required headings) |
| `evidence/artifacts/hcu-open-savings-subaccount.json` | capability artifact |
| `evidence/discovery/` | OpenAI `gpt-6-astra` discovery run |
| `evidence/replay/` | replay success, member `10001` |
| `evidence/replay-not-found/` | replay CIF miss, member `99999` |
| `evidence/handoff/` | replay HITL, member `77777`, `--handoff simulate` |
| `evidence/demo/replay-tour.webm` | screen recording: replay `10001`, CIF miss `99999`, HITL `77777` |

Raw traces also land in `evidence/runs/` locally; that folder is gitignored.

Re-record the video (no LLM):

```bash
python3 -m cua record-demo
```

`--headed` and `record-demo` outline the control in yellow and pause so you can see the click. Headless replay without `--record` stays fast. Override with `CUA_PACE_MS` / `CUA_HIGHLIGHT_MS`.

Or add `--record` to any other command (`discover`, `replay`, `smoke`).

Happy path on screen: sign on → inquire `12345` → savings `4,250.18` → OFAC Continue → Submit Open → **SUB-ACCOUNT OPENED**.
