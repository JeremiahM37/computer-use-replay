# computer-use-replay

[![verify](https://github.com/JeremiahM37/computer-use-replay/actions/workflows/ci.yml/badge.svg)](https://github.com/JeremiahM37/computer-use-replay/actions/workflows/ci.yml)

`computer-use-replay` lets an LLM *discover* a UI workflow once -- clicking, filling and
reading a real (or fixture) web app under a reviewed product vocabulary and a
caller-owned task contract -- and saves what it learned as a typed, inspectable
capability artifact. Every later call *replays* that artifact deterministically
against the current live page with **zero model calls**; an unexpected page state
escalates to a person on the same live session instead of guessing. Everything the
engine may do is governed by a reviewed policy, and every run leaves a sanitized
evidence trail with no secret or page value.

The bundled banking workstation is a controlled lab: it can produce every runtime
failure on demand (expired session, service notice, slow load, wrong member,
permission denial, a relabeled second tenant) to test replay robustness and human
handoff. The same engine also runs on two applications the author did not write
-- interface.ai's hosted Meridian Core sample and unmodified ERPNext -- with a
reviewed profile and task contract per application and no target-specific code:
the check against overfitting to one app.

## Try it in 60 seconds

```bash
uv sync --frozen && uv run playwright install chromium && uv run computer-use-replay demo
```

No model, no server to start by hand, no JSON. This starts the fictional banking
fixture in-process and replays the committed capabilities end to end -- lookups,
an unknown member, an expired-session escalation, a second tenant's relabeled
presentation, a sub-account stopped at confirmation, the same `read_savings`
artifact on a text-terminal surface, and a decoration-only relabel rescued by
normalization -- printing a result table. On minimal Linux use
`uv run playwright install --with-deps chromium`; `COMPUTER_USE_REPLAY_CHROMIUM`
optionally selects an existing Chromium instead.

## Watch it

```bash
uv run computer-use-replay demo --present
```

Same tour, headed browser, a highlight box and caption over each control before it
acts, paced at roughly one action per second. The overlay is presentation only:
it changes nothing about targeting, evidence or results. Add `--pace 0.3` to
speed up, or run under `xvfb-run -a` / `COMPUTER_USE_REPLAY_HEADLESS=1`.

## Learn it live

```bash
export OLLAMA_URL=http://127.0.0.1:11434 OLLAMA_MODEL=qwen3.6:35b-a3b
uv run computer-use-replay demo --discover   # genuine discovery, then the tour
uv run computer-use-replay discover --goal "Look up a member and read their available savings balance." \
  --input member_id=00123 --accept-draft --artifact runs/goal-only.json  # needs `computer-use-replay serve`
```

`--provider` defaults to Ollama (`OLLAMA_URL`/`OLLAMA_MODEL`); with `OPENAI_API_KEY`
set, add `--provider openai --model YOUR_MODEL_ID` for OpenAI instead.

## On interface.ai's Meridian Core sample app

```bash
MERIDIAN_PASSWORD=password uv run computer-use-replay invoke read_savings \
  --integration meridian --input operator=teller1 --input-env password=MERIDIAN_PASSWORD \
  --input branch="MAIN-001 - Main Office" --input member_id=100234
```

No profile path, no JSON: `--integration` fills in the target, profile and
saved-capability location from
[`integrations/meridian/integration.json`](integrations/meridian/integration.json).
See [the integration guide](integrations/meridian/README.md).

## Further validation: a complex real application (ERPNext)

```bash
ERPNEXT_PASSWORD=admin uv run computer-use-replay invoke prepare_quotation --integration erpnext \
  --input username=Administrator --input-env password=ERPNEXT_PASSWORD \
  --input customer_id=CP-CUSTOMER-012 --input item_id=CP-VALVE-100 \
  --input quantity=2 --input order_type=Sales
```

Unmodified [ERPNext](https://github.com/frappe/erpnext) 16 is the complex-interaction
target: two autocomplete pickers, a native dropdown, an editable grid row, and an
asynchronously priced grand total. See
[the integration guide](integrations/erpnext/README.md).

## Discover a goal, then replay it

Start the fixture (`uv run computer-use-replay serve`), then in another terminal with a
reachable Ollama service:

```bash
export OLLAMA_URL=http://127.0.0.1:11434
export OLLAMA_MODEL=qwen3.6:35b-a3b
uv run computer-use-replay discover --request requests/read_savings.json \
  --goal 'Look up the requested member and read their current savings balance' \
  --input member_id=00123 --artifact runs/savings.json
uv run computer-use-replay replay --artifact runs/savings.json --input member_id=00456
```

The goal request contains the capability name, typed inputs/outputs and success
conditions, **not a sequence of actions** -- the model chooses that from the
visible controls. `--input key=value` is a repeatable, typed-string alternative to a
JSON `--inputs` blob; `--input-env key=ENVVAR` reads a value from the environment.

To replay on tenant B, restart with `uv run computer-use-replay serve --scenario tenant_b`
and add `--presentation profiles/tenant_b.json` to the replay command: three labels
and one frame name change, and the saved capability stays unchanged. See
`docs/DESIGN_CHOICES.md` for onboarding a goal without hand-writing a request file.

## Locator drift: a verified fallback, never a fuzzy match

A step's own acting target may resolve through one more rung, only after its
primary and every reviewed alternate have zero matches, and only by
normalization (case/Unicode/whitespace/decoration, never an added or changed
word) -- a postcondition confirms the intended effect but cannot rule out an
extra, unintended one. States, postconditions and checkpoints never consult
it. A rescue is logged and reviewable (`fallback_review.json`: the reviewed
target and a fixed instruction, never on-screen text); `--fallback off` on
`replay`/`invoke`/`discover`/`demo` restores strict `target_drift`. See
REPORT.md §2-4.

## Human handoff

```bash
uv run computer-use-replay serve --scenario expired
uv run computer-use-replay replay --artifact capabilities/read_savings.json \
  --input member_id=00123 --human
```

On a graphical display: when the terminal prints an intervention prompt, click
**Renew session** in the same browser window (or Tab to it and press Enter), then
type `resume`. State must re-validate before automation regains control; `cancel`
exits instead. Without `--human`, the same scenario fails with an intervention ID.

Exit codes: success/business outcome `0`, execution failure `1`, bad config `2`.
Stdout JSON is the caller response; persisted evidence withholds output values.

## Evidence

[`evidence/README.md`](evidence/README.md) documents the required bundle: one
genuine discovery, model-free replays, a business outcome, a masked failure, a
completed handoff, a tenant-B drift replay, a verified-fallback rescue and a
genuine goal-only run.

## Tests and verification

```bash
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run pytest --basetemp=runs/pytest-local --cov=computer_use_replay --cov-branch --cov-fail-under=100 -q
uv run python scripts/preflight.py
verify run .verify.yaml
```

996 tests, 100% statement and branch coverage, real Chromium and local HTTP
servers -- no model service or cloud key needed. `verify` is an external wrapper
for the same checks. Preflight also verifies the tracked `evidence/` bundle
against the current source.

`tests/unit/` covers contracts, conditions, policy, ownership and the fallback
helpers; `tests/integration/` covers CLI processes, terminal handoff and provider
protocols; `tests/e2e/` covers real-browser discovery, replay, targeting, fields,
checkpoints, faults, handoff and the verified fallback ladder. Run a layer with
`uv run pytest tests/unit -q`.

## Repo map

Six modules are the core: `contracts.py` (wire/artifact schema), `policy.py`
(reviewed binding: controls, actions, network, risk), `discovery.py` (model-driven
learning loop), `engine.py` (deterministic, model-free interpreter), `browser.py`
(Playwright surface adapter) and `control.py` (ownership/handoff on one session).
`onboarding.py` lets a person accept a model-proposed contract instead of writing
one; `providers.py` is the Ollama/OpenAI wire format; `tour.py` is the shared
scenario harness behind `demo` and `scripts/capture_evidence.py`; `terminal.py`
-- a basic second surface proving the seam (REPORT.md §4): the same committed
`read_savings` artifact replays on a text-mode screen buffer, no Playwright.

Two deliberate extras go beyond that core: `catalog.py` + `invoke` turn saved
artifacts into agent-facing tool definitions runnable by name with zero model
calls; `demo`/`--present` is a guided, zero-setup replay with an optional caption
overlay. `fallback.py` holds the normalize-only helper behind the verified
locator ladder above; `capabilities/` runs without a model;
`integrations/` holds Meridian Core and ERPNext, each with its own profile and
guide; new discovery/browser output belongs under private `runs/`.
