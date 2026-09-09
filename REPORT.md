# REPORT.md

## Architecture

The system is a single Python process with two execution paths and one shared host driver.

Discovery takes a natural-language goal and a target URL, opens Chromium through Playwright, and loops observe → LLM decide → act until the goal is met, a step limit is hit, or the agent fails. Replay loads a saved capability JSON, binds parameters, and executes the recorded steps with no model in the decision loop. Both paths use the same `Surface` (frames, locators, clicks, fills) and the same policy checks. That split is the product claim: the model discovers; the artifact is the reusable capability; production invokes replay.

I implemented against a local mock core, Horizon Credit Union, instead of a public demo site. The brief asked for a proxy that exercises a multi-step servicing flow on a hostile UI. A local frameset (menu / banner / main), table layout, and no test IDs matches that environment, stays off the open web, and makes error states deterministic (CIF miss, permission deny, OFAC, address-change interstitial, timeout). The CLI starts the mock if `/health` is down so a reviewer does not need a second terminal.

Playwright was the computer-use mechanism, not screenshot-plus-coordinates. Coordinates drift; `name`, role, and visible text survive the kind of markup banks actually ship. Observation for the LLM is controls plus visible text plus a JPEG. An accessibility snapshot of every frameset pane costs tens of seconds per step on this host, so it is not collected. Replay does not observe at all: it reads visible text, classifies the screen, and screenshots only on a stop, a handoff, or the last step.

The process is intentionally small. There is no queue, no capability catalog service, and no cluster. A CLI is enough to show the contract an agent would call. `--headed` and `record-demo` pause and outline the control so a person can see the click; headless replay without `--record` stays fast.

LLM vendors are pluggable (`openai`, `anthropic`/`claude`, `gemini`). Defaults are the current models: `gpt-6-astra`, `claude-sonnet-5`, `gemini-3.8-flash`. I ran discovery end to end against the live mock with OpenAI `gpt-6-astra`; it signed on, inquired member `12345`, opened the share, and returned Jane Okonkwo, savings `4,250.18`, new account `80-12345-03`, confirmation `CUS-44191`. That class of model is more than this loop needs. Discovery here is a short, structured JSON act cycle on a known form, not long-horizon research. Cost-efficient defaults that still fit are `gpt-5.6-luna`, `claude-haiku-4-5`, and `gemini-3.8-flash` (Gemini’s latest Flash is already the cheap workhorse). `LLM_MODEL` overrides any of these.

## Artifact schema

The artifact is a versioned Pydantic document, not a chat transcript. A calling agent should be able to read what the capability does, what it needs, and what it returns without watching the discovery video.

A capability has `id`, `name`, `version`, `description` (the original goal), `surface` (`web` today), `entry` URL, typed `inputs`, typed `outputs`, ordered `steps`, and a `checkpoint` (frame plus required visible strings). Each step has an action (`click` / `fill` / `select` / `press` / `wait`), a frame name, an ordered locator list, and a value source.

Locators are tried in order: HTML `name` first (OPID, MEMNO, DEPAMT are stable on this host), then role plus accessible name, then visible text. Discovery also stores a `value_from` of `secret:…`, `input:…`, or a literal. Secrets never land in the JSON as plaintext; replay pulls them from env. Bound inputs become the agent-facing parameters. The committed Astra artifact has ten steps: sign-on, inquire, open, OFAC Continue, then a bound `product` select and `deposit` fill before SUBMIT OPEN. The checkpoint is `SUB-ACCOUNT OPENED` in frame `main`, not “the last click returned 200.”

I kept the schema web-shaped but frame-aware rather than inventing a fully abstract “control graph.” The brief said implement one surface and not paint the design into a corner. `frame` plus locator-by-name/role is the seam: a desktop adapter would resolve the same step against an accessibility tree instead of a DOM, without changing inputs, outputs, or checkpoint.

The committed artifact is the reviewed happy-path recording. Discovery can emit a new one; we do not treat the LLM transcript as the capability.

## Determinism & error handling

Replay never asks a model what to click. After each step it classifies the visible text.

The result contract has three statuses: `success` (checkpoint matched, outputs filled), `business_outcome` (the host answered, and the caller needs that answer), and `failed` (stop, with `step_id`, expected, observed, and reason). That is the distinction the brief calls out. “RECORD NOT FOUND” on member `99999` is `not_found`, not a crash, and it does not hand off. Permission copy maps to `permission_denied`. Host validation maps to `validation`. Session expiry is a hard `timeout`. `HOST MESSAGE 12E` (address-change pending on `77777`) is `unexpected_dialog`: recoverable, but not a step this capability recorded, so replay must not click CONTINUE on its own.

Locators wait for visibility with a short timeout and fall through the list. Fill/select refuse to target a label cell (`<td>OPERATOR ID</td>`). Submit buttons match on alphanumeric name so `OPEN SUB ACCOUNT` still hits `OPEN SUB-ACCOUNT`. Those rules exist because the LLM names controls the way a teller reads them, not the way the HTML is spelled.

I did not build a general wait-for-network-idle strategy. This host is local and fast. Slow/failed load would surface as a locator timeout or a classified timeout screen, which is enough for the slice.

Evidence covers success (`10001`), CIF miss (`99999`), and HITL (`77777`). Classifiers for permission, validation, and timeout are in code against the mock; I did not ship extra packs for every branch.

## Heterogeneity & multi-tenant

Not built, by the brief. The seam is `Surface`: perceive and act. The artifact is the flow. A legacy web app that is not a frameset still fits; you record `frame=root` and the same locator kinds. A desktop core would implement the same step actions against OS automation or the accessibility tree. The checkpoint stays “this text is on the operator’s screen,” which is what you have when there is no DOM.

Multi-tenant reuse is why inputs are parameters and locators are not CSS paths tied to one CU’s skin. Member id is bound; branding in the banner is ignored. Two credit unions on the same vendor product would share the artifact and overlay tenant-specific bits (base URL, operator secrets, maybe a locator alias file) rather than re-record the flow. Drift detection is replay classification plus checkpoint failure: if a tenant’s build renamed INQUIRE, locators miss, the run fails with step and screenshot, and you re-discover or patch the overlay. I would not canonicalize `/inquiry` into `/item/:id` here; this host’s routes are already stable program names.

## Escalation & handoff

Stuck means: locator miss, unexpected dialog, discovery repeating the same control, or the agent emitting `fail`. A CIF miss is not stuck.

Handoff pauses the same Playwright Chromium. The operator does not log in again and does not get a second browser. The terminal prints why it stopped and how to give the session back: Enter to resume, `abort` then Enter to stop. An optional desk at `http://127.0.0.1:7879/` does the same. Human clicks on that live page are recorded. `--handoff simulate` exists so unattended evidence can click CONTINUE on `77777` without a person at the keyboard.

If the operator presses Enter and the screen is unchanged or still unknown, the system does not guess. It prints `STILL CANNOT PROCEED` and hands the window back. Default is two handoffs (`CUA_HANDOFF_MAX_ATTEMPTS` / `--handoff-max`). A second unsuccessful return aborts (`handoff_exhausted`). That bound is a product choice: infinite HITL loops are how unattended replay dies quietly.

A full co-browsing console is out of scope. The control-transfer model is real: automation pauses, a human owns the live session, resume inspects the current screen, then replay continues from the next recorded step.

## Safety

Three layers, all checked on act (every frame, so a `/wire` main pane is caught even when the top URL is still `/console`). Observe does not enforce routes, so a forbidden screen can still be screenshotted for the handoff packet.

1. Origin allowlist: only the `--target` origin (default `http://127.0.0.1:7878/`).
2. Route allowlist: servicing paths only. The mock menu includes Wire Transfer and GL Override as real pages a person can open; the agent cannot drive `/wire` or `/gl-override`.
3. Action allowlist: `click`, `fill`, `select`, `press`, `wait`.

Risky vs reversible: SUBMIT OPEN actually opens the share. The brief allows block, confirm, or flag. Replay **flags and logs** (`policy.risky_action`) and still clicks. Discovery already reviewed that step into the capability; asking a human on every production replay would make the artifact unusable as an agent tool. Unknown screens and HITL cover the case where the model has not reviewed the click.

Secrets: operator password is `secret:password`, redacted in logs and artifacts. Session cookies matching `HCUSESS` are stripped. `.env` is gitignored.

## Cuts

I did not build stretch goals (capability catalog API, codegen, approval states, bounded LLM repair, cross-tenant canonicalization, N-run stability). The operator desk is a stub. There are no automated tests. Evidence does not include a dedicated pack for every classified error. Desktop and multi-tenant overlays are design only.

Next, in order: a tiny locator unit test around hyphen/label matching, then an agent-facing `invoke(capability_id, inputs)` function so the artifact is callable by name, then a second mock tenant with a CSS restyle to prove the overlay story.
