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

If several are set, OpenAI wins unless you set `LLM_PROVIDER` or pass `--provider`. Optional `LLM_MODEL` overrides the default (`gpt-4o`, `claude-sonnet-4-5`, `gemini-2.5-flash`).

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
| `evidence/artifacts/hcu-open-savings-subaccount.json` | capability artifact |
| `evidence/discovery/` | GPT-4o discovery run |
| `evidence/replay/` | replay success, member `10001` |
| `evidence/replay-not-found/` | replay CIF miss, member `99999` |

Raw traces also land in `evidence/runs/` locally; that folder is gitignored.

Happy path on screen: sign on → inquire `12345` → savings `4,250.18` → OFAC Continue → Submit Open → **SUB-ACCOUNT OPENED**.
