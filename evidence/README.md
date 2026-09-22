# Required demonstration evidence

This is the small submission bundle required by assignment sections 4 and 6.
The discovery run was recorded on 2026-09-18 against the fictional banking
workstation, using real Chromium and a live Ollama `qwen3.6:35b-a3b` model; the
replay runs below were re-verified against the current source with
`uv run python scripts/capture_evidence.py --extend-submission`.

| Run | Result | Model calls |
| --- | --- | --- |
| [Discovery](discovery/events.jsonl) | Savings lookup learned and saved | 6 |
| [Changed-input replay](replay/events.jsonl) | Same capability completed successfully | 0 |
| [Expired-session replay](failure/events.jsonl) | Intervention requested; no operator attached | 0 |
| [Business-outcome replay](business_outcome/events.jsonl) | Unknown member: `member_not_found` | 0 |
| [Same-session handoff](handoff/events.jsonl) | Operator renews the live session; run resumes and completes | 0 |
| [Tenant B replay](tenant_b/events.jsonl) | Same capability succeeds under tenant B's presentation overlay; one `presentation_drift` event | 0 |
| [Text-terminal replay](terminal/events.jsonl) | Same capability succeeds on a second surface (screen buffer, no browser); one `presentation_drift` event | 0 |
| [Verified-fallback replay](fallback/events.jsonl) | No overlay, no reviewed alternate: a decoration-only, unreviewed relabel ("Find member" -> "FIND MEMBER…") is rescued by the ladder's `normalized` rung -- the ONLY rung that ever acts; one `fallback_resolved` event, a `warnings` entry, and [fallback_review.json](fallback/fallback_review.json) (the reviewed target and a fixed instruction, never the observed text) for a person to review | 0 |
| [Goal-only onboarding](goal_only/events.jsonl) | Model proposes `lookup_member_savings` from the goal sentence alone, the draft is accepted (`--accept-draft`), then discovered and saved | 1 proposal + 6 discovery |

The goal-only run is the one place this bundle shows the *system* drafting a
contract rather than a human authoring `requests/*.json` by hand: `qwen3.6:35b-a3b`
sees only the goal sentence and the reviewed product vocabulary (control keys,
labels, operations, declared input types, readable outputs, screen headings --
never page content or invocation values) and calls one `propose_contract` tool.
[goal_only/request.json](goal_only/request.json) is that draft, still carrying its
`"review": {"status": "draft", ...}` marker; the `contract_accepted` event (code
`flag`) right after it in [events.jsonl](goal_only/events.jsonl) is what let
discovery proceed. [goal_only.json](goal_only.json) is the resulting capability --
same reviewed product, same `member_id`/`balance` semantics as the hand-authored
[read_savings.json](read_savings.json), differing only in the name and output key
the model chose for itself (`--name` on `computer-use-replay propose` pins that if wanted).
See "Onboarding a new task or product" in the root README and REPORT.md §1.

[read_savings.json](read_savings.json) is the exact capability emitted by the
recorded discovery and consumed by every recorded replay. Each run includes its
sanitized structured events and result. The expired-session failure also includes a
[masked screenshot](failure/failure-masked.png) and the state snapshot referenced
by its failure result; it demonstrates escalation with no operator attached, not a
completed handoff. The handoff run demonstrates the completed handoff: on the
**same live session** as the failure run, a **scripted agent operator** (not a
person) claims the paused lease, clicks the real "Renew session" control inside the
workbench frame of the actual browser page, waits for the real "Account directory"
heading, and then explicitly resumes — after which automation finishes the original
request with zero further model calls. Its `intervention_requested` /
`control_claimed` / `control_resumed` events and terminal `success` are what
distinguish it from an ordinary replay.

The tenant B run replays the exact same saved capability against
`profiles/tenant_b.json`, whose overlay relabels three controls and renames one
frame. Replay resolves every target through that current presentation and
succeeds with the same outputs; before that, the run logs one `presentation_drift`
event naming each reviewed target key whose recorded discovery-time hint no
longer matches the current presentation — informational, since replay never uses
the recorded hint to act. See §4 of [REPORT.md](../REPORT.md) for how this and the
`target_drift` failure code differ.

The fallback run replays the same saved capability with no overlay at all: the
live page's own "Find member" button has been renamed, in-place, to
"FIND MEMBER…" -- a decoration-only relabel (case and an added ellipsis, no
added or changed word) that never appears in any `profiles/*.json`. Only once
the reviewed primary target and its (zero) reviewed alternates have no visible
match does the verified locator fallback ladder try its one rung,
`normalized`, against candidates of the same role in the same frame; a click's
own postcondition, fill read-back and the run's checkpoints stay exactly as
strict as every other run in this bundle. A broader relabel -- an added,
removed or changed word -- is never something this ladder acts on: it stops
as `target_drift` instead, because a postcondition can confirm the intended
effect but cannot rule out an additional, unintended one. See REPORT.md §2-4
and `docs/DESIGN_CHOICES.md` for the guard against stealing another control's
element, why states/postconditions/checkpoints never use it, and why the
`similar` rung this bundle used to demonstrate was removed.

Replays use a different synthetic member from discovery; the capture harness
checked the expected balance in memory. Persisted outputs are deliberately
`<withheld>`. Every `model_response` event records provider, model, call number, latency, prompt
and output token counts and a SHA-256 of the raw response, so a genuine run is
checkable without storing the transcript. Events contain actions, reason codes and
that model-call metadata, not raw
model messages, credentials or customer values. No account-changing action ran.

[manifest.json](manifest.json) pins the validation runtime, model digest, source-file
hashes and every selected data file. `catalog_discovery_provenance` preserves the
older catalog capture provenance; refreshing replay checks does not turn those
original model recordings into new discoveries. The live-form cases are separate
genuine captures. Run `uv run python scripts/preflight.py` to
check integrity, current-runtime compatibility, event schemas and call counts.
Discovery's directory label is shortened for navigation; the manifest retains its
original run identifier. `scripts/capture_evidence.py` produces and validates the
whole bundle rather than assembling it by hand — see its `--discover` and
`--extend-submission` flags. Captured data files are otherwise unchanged.

To reproduce discovery and a changed-input replay, start `uv run computer-use-replay serve`
in one terminal. In another, with `OLLAMA_URL` pointing at your model service:

```bash
uv run computer-use-replay discover --request requests/read_savings.json \
  --provider ollama --model qwen3.6:35b-a3b \
  --inputs '{"member_id":"00123"}' --artifact runs/fresh-savings.json
uv run computer-use-replay replay --artifact runs/fresh-savings.json \
  --inputs '{"member_id":"00456"}'
```

To reproduce the exceptional state, restart the fixture with
`uv run computer-use-replay serve --scenario expired` and repeat the replay without
`--human`. To reproduce the business-outcome, handoff, tenant B and text-terminal
runs exactly as tracked here (replaying the committed `read_savings.json`, no model needed),
copy the selected bundle to a fresh local directory, then refresh its replays:

```bash
mkdir -p runs
cp -a evidence runs/my-evidence
uv run python scripts/capture_evidence.py --extend-submission --output runs/my-evidence
```

New output stays in ignored `runs/`; this directory contains only the reviewed
submission sample. See the root README for interactive human recovery.

## Live form perception

[live_forms/summary.json](live_forms/summary.json) records three genuine Qwen
runs using `profiles/juniper_live.json`: savings lookup, sub-account preparation,
and savings lookup with four unfamiliar button labels and extra layout wrappers.
Ordinary input/button locators are discovered at runtime in reviewed form scopes.
Each case includes the schema-3 artifact, sanitized discovery events/result, and a
changed-input replay with zero model calls. Both business branches stop before any
account-changing action. These are three successful examples, not a reliability
benchmark or evidence of general desktop/vision support.

The capture checks known private fixture strings before sending each model context,
and asserts the replay's expected result in memory. Those assertions are not a
universal PII detector. Public interface labels and grounded locators are deliberately
retained; raw model messages, page captures and caller values are not included.

Reproduce all three cases against a reachable Ollama endpoint:

```bash
uv run python scripts/capture_live_evidence.py --output runs/my-live-evidence \
  --model qwen3.6:35b-a3b
```

The output directory must be new so a failed rerun cannot overwrite prior evidence.
`live_forms/capture.json` pins the runtime, profile, requests and capture script;
the capture rejects source changes between its start and finish.
`preflight.py` verifies artifact provenance, event call counts, artifact links,
withheld outputs, permissions and file hashes for this selected sample.
