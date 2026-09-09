# Horizon Credit Union computer-use take-home

Happy-path slice: give the system a **goal + target**, it drives the local member-servicing console, and it writes a reusable artifact.

Use a virtualenv. It keeps Playwright, the LLM SDKs, and Python 3.14 isolated so a reviewer can reproduce the run without touching their global Python.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env
```

Put **one** LLM key in `.env`:

- `OPENAI_API_KEY` for OpenAI
- `ANTHROPIC_API_KEY` for Claude
- `GEMINI_API_KEY` for Gemini

If several keys are present, it uses OpenAI unless you set `LLM_PROVIDER` or pass `--provider`. Optional `LLM_MODEL` overrides the default (`gpt-4o`, `claude-sonnet-4-5`, or `gemini-2.5-flash`).

The mock operator is `TELLER01` / `train`. The CLI boots `mock-core/server.py` if `http://127.0.0.1:7878/health` is down.

## Demo

LLM discovery (this is the assignment's real discovery run):

```bash
python3 -m cua discover --headed
python3 -m cua discover --headed --provider anthropic
python3 -m cua discover --headed --provider gemini
```

Same thing with an explicit goal and target:

```bash
python3 -m cua discover \
  --target http://127.0.0.1:7878/ \
  --goal "Sign on. Look up member 12345, read the savings balance, open a REGULAR SHARE sub-account with 25.00, and return the new account number and confirmation."
```

Replay the saved artifact with no LLM:

```bash
python3 -m cua replay --headed --input member_id=10001 --input deposit=25.00
```

Locator smoke check (scripted, not discovery — useful if you do not have a key yet):

```bash
python3 -m cua smoke --headed
```

## What you should see

Sign on → CIF inquiry for 12345 → savings 4,250.18 → OFAC continue → open sub-account → **SUB-ACCOUNT OPENED**.

Outputs and logs land in `evidence/runs/`. The capability artifact is `evidence/artifacts/hcu-open-savings-subaccount.json`.
