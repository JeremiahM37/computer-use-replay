# Computer-Use Automation System

## 1. Architecture

`computer-use-replay` is one Python process: a constrained discovery loop, capability
compiler, deterministic interpreter, policy layer and Playwright surface adapter.
Its primary target is a fictional banking workstation with named frames,
header-relative table values and no access to backing data -- a controlled lab
producing every runtime failure mode on demand. The same engine, with no
target-specific code, also runs on two applications this author did not
write -- Meridian Core and unmodified ERPNext.

A second target is interface.ai's hosted hiring sample, Meridian Core: a
server-rendered, JavaScript-free legacy workstation with no `<label>` or
`<th>` anywhere, and a real redirecting login (its genuine `302` needed one
opt-in, per-rule `redirect_to` grant, §6). Discovery used `qwen3.6:35b-a3b`, 9
native calls, 3/3 attempts; three replays all matched.
[Integration guide](integrations/meridian/README.md).

A third target is unmodified ERPNext 16.34.2: the fixture and Meridian exercise
text fields, buttons and one dropdown, so ERPNext is the complex-interaction
check -- two autocomplete pickers, a dropdown, an editable grid row and an
asynchronously priced grand total. The committed artifact of record is a
genuine `qwen3.6:35b-a3b` discovery (13 native calls), re-learned twice as
the profile changed (business states, then a discard rule, §6), never
hand-edited; the original 12-call `gemma4:e4b` recording stays in
`integrations/erpnext/evidence/original/` for history. Replayed live it reproduces the documented
USD 371.00 total and, with a different customer/item/quantity, USD 503.50,
zero model calls either way. An unrecognized customer now returns a
`customer_not_found` business outcome, exit `0`, zero model calls -- same
`business` kind as the fixture's `member_not_found`, added without touching
the engine, since both pickers auto-select a fixed trailing option only when
no real match precedes it, covering an unrecognized item (`item_not_found`)
too. Not modeled: identifier shapes outside `input_types`, other ERPNext
pickers, or scope beyond English/USD/one line item/unsaved quotation.
[Integration guide](integrations/erpnext/README.md).

A person supplies a goal and a target. From the goal and the reviewed
vocabulary alone -- never page content -- the system proposes the task contract
(`computer-use-replay propose`, or `discover --goal` with no `--request`); the person
accepts the draft (`--accept-draft`, or interactively) or hand-authors
`requests/*.json` directly. The reviewed vocabulary -- controls, operations,
risk, routes, invariants -- stays authored once by a person: the safety
boundary the model proposes inside, never past. Measured (`qwen3.6:35b-a3b`,
live): 6/6 proposals schema-valid, 6/6 discoveries succeeded, every replay
zero model calls (`docs/DESIGN_CHOICES.md`). Each turn the model sees the
goal, contract and visible controls and picks one offered action; the engine
executes and verifies it. Parameter and extracted values never reach the model.

The goal steers choices but cannot lower the bar: holding the contract fixed,
the real goal and an irrelevant one produced identical steps, while a
conflicting goal reached a control that could not satisfy it and wrote no
artifact. The contract defines success; English selects among legal paths.

## 2. Artifact schema

Schema 2 declares a capability name, typed inputs/outputs, ordered discriminated
actions (`fill`, `click`, `read`), logical control keys, success conditions, product
fingerprint and discovery provenance. Fill actions reference parameters; read
actions must match the request's output sources; clicks require an observed
postcondition; money parses to a decimal amount and currency, never a float.

The caller's goal request owns the contract. Product policy supplies allowed
input types, permitted parameter/control pairs, actions, risk and identity invariants.
Recorded `targets` are discovery-time locator hints for review -- runtime targeting
resolves their keys through the current presentation.

Each locator kind trades off differently. `role` plus accessible name is what
screen readers depend on, so restyling rarely breaks it. `label` binds only to
the control a real `<label>`/`aria-label` governs. `row_value`/`row_input`
resolves a cell by exact header text within its row, refusing an identical
header in an enclosing layout table. `css`, the weakest kind, is used only
where markup gives no role, label or header -- Meridian's `name`-attribute
inputs, ERPNext's undecorated grid/dropdown fields -- and still needs an exact,
unique, reviewed match. Frame lineage walks reviewed names, never indices; no
kind falls back to position.

The fingerprint excludes presentation locators but includes policy semantics: a
renamed button can reuse an artifact, a permission change needs a new one, and
schema-1 artifacts are rejected outright.

## 3. Determinism & error handling

Replay uses exact, unique visible controls in named frame lineages; missing or
duplicate targets stop, no positional fallback. Each click's effect is observed
before the next action; an uncertain click never repeats. Waiting and
reversible recovery have explicit budgets.

Business outcomes include missing member, validation and permission denial. A
service notice is dismissed through approved recovery; a transient load is
awaited; expiry and unexpected dialogs escalate. Results distinguish success,
business outcome and failure; invalid caller arguments fail before browser
creation, with no artifact.

A failure names its step, expected/observed condition, logical target,
configured frame/label and match count. Structured snapshots preserve
reviewed locator metadata and geometry; a masked screenshot preserves layout
while concealing text and media. Zero matches with no declared state present
reports `target_drift`, not `checkpoint_failed` (§4).

## 4. Heterogeneity & multi-tenant

The seam is the `Surface` protocol: `navigate`, `observe`, `perform`, `condition`,
`check_health` and `failure_screenshot` -- six operations, no DOM or Playwright type
in any signature. A second, deliberately basic surface makes it concrete:
`ScreenSurface` resolves a new `screen` target kind (`heading | field | command |
value`) against an in-process text-mode screen buffer, the kind of API a terminal
emulator already exposes. The committed, web-learned `read_savings.json` replays
byte-unchanged on it, zero model calls, because the fingerprint excludes
presentation (§2) and only a `profiles/terminal.json` overlay changes. This does
not prove a real 3270/5250 stack, an OS accessibility tree, a vision adapter, or
a handoff UI -- only that the seam sits where the brief asks.

Product policy, task request and tenant presentation have separate
responsibilities. Tenant B changes three labels and a frame name through a
small overlay; both saved capabilities replay unmodified on tenants A and B.
Overlays cannot supply actions, risk, state classifiers or checkpoints; the
identity invariant stays mandatory.

Drift gets a signal: replay diffs each recorded target hint against the
current presentation and logs one `presentation_drift` event -- informational
only; `--present` captions it.

**Targeting resolves in order: primary, reviewed alternates, then a verified
fallback ladder.** Alternates are the same strict kinds, tried after zero
matches on the primary; more than one match on any rung stops as
`ambiguous_target` (`alternate_resolved`, excluded from the fingerprint;
`profiles/tenant_b.json` gives `search` one, the pre-rollout label).

The ladder acts for a step's OWN acting target -- never a state,
postcondition or checkpoint -- once primary and alternates are all zero,
under `fallback: "verified"`: NORMALIZATION ONLY (case/Unicode/whitespace/
decoration-insensitive exact match), any op, one visible match, same role
and frame, never another control's element. Nothing broader acts: a
postcondition confirms the intended effect but cannot rule out an unintended
extra one, so "Do not Find member" and "Find member and delete" both stop as
`target_drift` beside "Find member". Policy stays keyed by control, not
locator (`human_only`/`blocked` never rescue). A rescue logs
`fallback_resolved`, adds a `warnings` entry, and writes
`fallback_review.json` -- the reviewed target and a fixed instruction, never
on-screen text. `--fallback off` restores strict `target_drift`.

## 5. Escalation & handoff

Ownership has automation, paused, human and closed states. A request carries the
capability, step, reason, session identity and evidence reference; claim issues a
lease; stale claims/resumes and concurrent automation are rejected. The operator
works the same browser, then resumes after validation, or cancels -- a human
performing business steps cannot turn an incomplete run into one.

Context-installed listeners record approved control keys and event kinds
across navigation, never entered values. The CLI's `--human` flag is the
one interactive path: an operator claims the lease, acts in the same open
browser, and types `resume`, re-validating the blocking condition before
returning control. `computer-use-replay demo`'s scripted operator demonstrates the
same mechanism with nobody present; `--present` labels it honestly.

## 6. Safety

The current trusted product policy governs discovery and replay alike. Browser
requests must match the exact origin, route, method and parameter rules.
Redirects fail closed by default; the sole exception is a per-rule `redirect_to`
grant permitting one same-origin GET hop, re-checked and fetched by this
layer, not the browser -- a fidelity gap. Unexpected windows,
downloads, confirmations, WebSockets and service workers all fail closed;
account closure and sub-account creation stay human-only. ERPNext's periodic
update check failed closed the same way until reviewed (§1).

Persistence is an allowlisted projection: model reasoning, raw DOM, invocation
and output values are absent from logs and artifacts, though stdout returns
requested outputs. Screenshot CSS conceals text, inputs and media across
frames without rewriting the DOM -- text paints as flat neutral blocks, so
structure stays visible with nothing legible.

## 7. Cuts

No queues, dashboard, code generator, model-assisted replay, or a genuine
frontier-model discovery run; the second surface (§4) is a basic proof, not a
real terminal or vision/accessibility stack, and native desktop stays
unimplemented. The fallback ladder never writes back or does fuzzy, semantic
or vision matching -- a person reviews `fallback_review.json`.
`computer-use-replay catalog` + `invoke <name>` expose saved artifacts as agent-callable
tools through the same replay path -- no registry or chaining. `computer-use-replay
demo`/`--present` replay committed artifacts with no setup or model, changing
nothing observed or recorded.

Goal-only onboarding (`computer-use-replay propose`, or `discover --goal` alone) drafts a
task *contract*, not the product vocabulary: controls, operations, risk, routes and
invariants stay hand-reviewed per product in `profiles/*.json`. Drafting that
vocabulary, and automatic risk classification, are both left to a person.

A signed-approval path (an Ed25519 receipt gating replay of one artifact) and
a loopback operator web console were prototyped and removed, to stay focused
on schema, replay and safety/escalation work.

Synthetic results do not prove vendor compatibility; live cloud (OpenAI)
quality is unverified beyond protocol and browser integration. Further
decisions live in `docs/DESIGN_CHOICES.md`.

The [evidence bundle](evidence/README.md) records one genuine discovery, a
changed-input replay, a business-outcome replay, a masked-diagnostics failure,
a completed handoff, a tenant B `presentation_drift` replay, the same replay
on the text-terminal surface, a decoration-only relabel rescued by
normalization, and a genuine goal-only run.