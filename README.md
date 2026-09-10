# Horizon Credit Union computer-use take-home

Record a UI flow once with an LLM, save it as a typed capability, then replay it without the model.

Target is a local legacy-style member console (`mock-core/`), not a real bank. Use a virtualenv so Playwright and the LLM SDKs stay isolated. Run every command below from the repo root, with the venv active. Python 3.10 or newer.

## Setup

```bash
python3 -m venv .venv
```

Creates an isolated Python environment in `.venv`.

```bash
source .venv/bin/activate
```

Puts that environment on your `PATH` for this terminal. On Windows: `.venv\Scripts\activate`.

```bash
pip install -r requirements.txt
```

Installs Playwright, Pydantic, python-dotenv, and the OpenAI / Anthropic / Gemini clients.

```bash
python3 -m playwright install chromium
```

Downloads the Chromium build Playwright drives. Needed even for headless replay.

```bash
cp .env.example .env
```

Copies the config template. Open `.env` and put **one** API key on its line:

- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY` (Claude)
- `GEMINI_API_KEY`

Leave the other key lines blank. If several are set, OpenAI wins unless you set `LLM_PROVIDER` or pass `--provider` on `discover`. Optional `LLM_MODEL` overrides the default (`gpt-6-astra`, `claude-sonnet-5`, `gemini-3.8-flash`). Replay, catalog, invoke, serve, and `prove-replay` do not need a key.

Mock operator: `TELLER01` / `train`. The CLI starts `mock-core/server.py` on `http://127.0.0.1:7878/` if `/health` is down. You do not need a second terminal.

## Demo path

1. LLM discovery. Opens Chromium, drives the mock with a model, and writes `evidence/artifacts/hcu-open-savings-subaccount.json`. `--headed` shows the browser.

```bash
python3 -m cua discover --headed
```

This overwrites the committed Astra artifact. Skip this step unless you want a new recording. No key needed after this if you use the file already in the repo.

Same command with an explicit host URL and goal instead of the defaults:

```bash
python3 -m cua discover \
  --target http://127.0.0.1:7878/ \
  --goal "Sign on. Look up member 12345, read the savings balance, open a REGULAR SHARE sub-account with 25.00, and return the new account number and confirmation."
```

`--provider openai|anthropic|gemini` forces a vendor when more than one key is set.

2. Replay the saved artifact with no model, member `10001` (Robert). Binds `member_id`; `product` and `deposit` stay the recorded defaults. Prints a JSON result when the checkpoint `SUB-ACCOUNT OPENED` matches.

```bash
python3 -m cua replay --headed --input member_id=10001
```

3. Same replay on member `99999`. The host returns RECORD NOT FOUND. The run stops after Inquire with `status=business_outcome`, `outcome=not_found`. Not a crash, and it does not hand off.

```bash
python3 -m cua replay --headed --input member_id=99999
```

4. Same replay on member `77777`. The host shows `HOST MESSAGE 12E` (address-change pending), a screen this artifact never recorded. `--handoff wait` pauses the live Chromium window for you.

```bash
python3 -m cua replay --headed --handoff wait --input member_id=77777
```

See **Human intervention** below for what the terminal prints and how to hand control back.

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

Unattended HITL: no waiting at the keyboard. A test operator clicks CONTINUE on the same live page, then replay finishes.

```bash
python3 -m cua replay --handoff simulate --input member_id=77777
```

## Capability catalog

Saved artifacts under `evidence/artifacts/` are tools. That path is replay. It does not load a model.

List every saved capability (id, description, typed inputs/outputs). No browser.

```bash
python3 -m cua catalog
```

Emit OpenAI-style function tools for those capabilities (JSON Schema args). No browser.

```bash
python3 -m cua tools
```

Look up a capability by id or name, bind typed args, and replay it. Unknown argument names fail before Chromium starts. Omitted inputs use the values on the artifact (`product` and `deposit` here).

```bash
python3 -m cua invoke --name hcu-open-savings-subaccount --input member_id=10001
```

HTTP version of the same contract. Starts the mock if needed. Default `http://127.0.0.1:7880/`. Each `POST /v1/invoke` opens Chromium. `--handoff off` so a curl success path does not pause for a human.

```bash
python3 -m cua serve --handoff off
```

In another terminal, list tools, then invoke member `10001`:

```bash
curl -s http://127.0.0.1:7880/v1/tools
curl -s http://127.0.0.1:7880/v1/invoke \
  -H 'Content-Type: application/json' \
  -d '{"name":"hcu-open-savings-subaccount","arguments":{"member_id":"10001"}}'
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

No API key. Checks that `cua/replay.py` cannot import `cua/llm.py` or an OpenAI / Anthropic / Gemini SDK, then exits.

```bash
python3 -m cua prove-replay --imports-only
```

Same import check, then replays members `10001` (success), `99999` (not found), and `77777` (HITL simulate). Same three cases as the demo video.

```bash
python3 -m cua prove-replay
```

One replay at a time, headless, no key:

```bash
python3 -m cua replay --input member_id=10001
python3 -m cua replay --input member_id=99999
```

Scripted locator walk on the mock (sign-on, inquire, open). Not discovery and not the saved artifact. Do not treat it as the LLM run.

```bash
python3 -m cua smoke
```

Catalog bind/schema tests. No browser, no key.

```bash
python3 tests/test_catalog.py
```

Same import-graph check as `prove-replay --imports-only`. No browser, no key.

```bash
python3 tests/test_replay_no_llm.py
```

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
| `evidence/catalog/` | catalog `invoke` of the same artifact, member `10001` |
| `evidence/demo/replay-tour.webm` | screen recording: replay `10001`, CIF miss `99999`, HITL `77777` |

Raw traces also land in `evidence/runs/` locally; that folder is gitignored.

Records one Playwright video of replay `10001`, CIF miss `99999`, and HITL `77777` simulate. Writes `evidence/demo/replay-tour.webm`. No LLM.

```bash
python3 -m cua record-demo
```

`--headed` and `record-demo` outline the control in yellow and pause so you can see the click. Headless replay without `--record` stays fast. Override with `CUA_PACE_MS` / `CUA_HIGHLIGHT_MS`.

`--record` on `discover`, `replay`, or `smoke` saves a video of that one command under `evidence/demo/`.

Happy path on screen: sign on → inquire `12345` → savings `4,250.18` → OFAC Continue → Submit Open → **SUB-ACCOUNT OPENED**.
