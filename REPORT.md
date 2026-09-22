# Computer-Use Automation System

## 1. Architecture

A goal becomes a reviewed task contract. Discovery uses an LLM to choose actions
from live observations, verifies their effects, and compiles the successful run;
later invocations replay the artifact with zero model calls.

```text
goal → accepted contract → observe / decide / act → verified artifact → deterministic replay
                             ↑             ↓
                        live surface   independent policy
```

**Perception and permission are separate.** In live-observation mode, the browser
adapter finds controls in reviewed UI scopes on each turn. The model sees a bounded
projection of their roles, permitted interface text and structure, and selects a
live candidate rather than a pre-authored business-control key. The runtime grounds
that choice, checks its current scope and action permission, and records the target
for replay. The model cannot authorize an action, supply an executable selector,
change a success condition, or choose an arbitrary source of output data.

This is structured browser perception, not vision. Export scopes identify interface
chrome that the application owner has reviewed as safe to disclose; input values and
business data are excluded. The privacy boundary depends on those scopes remaining
appropriate for the application. It is not a claim that a general-purpose redactor
can recognize every name or account number in arbitrary page text. Page-derived text
is observation data, never instructions or execution authority.

The older reviewed-catalog mode remains supported: a person locates and names the
controls, and the model sequences them. Its existing recordings establish replay and
integration behavior, not automatic control identification. Live perception removes
that requirement for ordinary navigation and input controls within approved scopes;
product permissions, identity checks, recovery rules and output definitions still
require review. Each scope must contain only reviewed reversible operations;
human-only operations need a separate destination or stable exclusion. POST body
fields are independently allowlisted. Onboarding is reduced, not eliminated.

`computer-use-replay` is one Python process with a planner, compiler, policy layer,
interpreter and surface adapter. The fictional Juniper workstation supplies controlled
failures and branching tasks. Meridian Core, the hosted hiring sample, checks a
legacy server-rendered interface; unmodified ERPNext checks autocomplete, dropdowns,
an editable grid and asynchronously calculated totals. Their existing integrations
use reviewed catalogs; they are not evidence that live grounding works on arbitrary
applications. See the [Meridian](integrations/meridian/README.md) and
[ERPNext](integrations/erpnext/README.md) guides for the exact supported scope.

The caller supplies typed inputs, output sources and success conditions through a
request, or accepts a model-proposed task contract. The contract defines success;
the English goal guides choices. Genuine local-model discovery is tested separately
from scripted-planner regression tests. An irrelevant goal succeeding against a fixed
contract in the older mode is a limitation, not evidence of general goal understanding.

The [selected live-form runs](evidence/live_forms/summary.json) cover both tasks
and a relabeled layout with genuine local Qwen calls (6, 8 and 6 respectively).
Changed-input replay took 0.94–1.09 seconds with zero model calls; discovery took
20–29 seconds on this machine. These three recordings demonstrate the path,
not a statistical reliability or cross-provider benchmark.

## 2. Artifact schema

Schema 2 preserves reviewed-catalog artifacts; schema 3 adds grounded live targets.
Both declare a capability name, typed inputs/outputs, ordered discriminated
actions (`fill`, `click`, `read`), logical control keys, success conditions, product
fingerprint and discovery provenance. Fill actions reference parameters; read
actions must match the request's output sources; clicks require an observed
postcondition; money parses to a decimal amount and currency, never a float.

The caller's goal request owns the contract. Product policy supplies allowed
input types, permitted parameter/control pairs, actions, risk and identity invariants.
In reviewed-catalog mode, recorded `targets` are review hints and the current
presentation supplies locators. Live-discovered actions instead carry a grounded
locator and its scope/strategy provenance. Replay resolves that locator only after
checking the current permission grant; recording a locator does not approve it.
Artifacts are local executable plans, not cryptographically signed discovery
attestations. Provenance supports inspection; current policy and runtime checks
enforce authority even if a plan is edited.

Each locator kind trades off differently. `role` plus accessible name is what
screen readers depend on, so restyling rarely breaks it. `label` binds only to
the control a real `<label>`/`aria-label` governs. `row_value`/`row_input`
resolves a cell by exact header text within its row, refusing an identical
header in an enclosing layout table. `css`, the weakest kind, is used only
where markup gives no role, label or header -- Meridian's `name`-attribute
inputs, ERPNext's undecorated grid/dropdown fields -- and still needs an exact,
unique, reviewed match. Frame lineage walks reviewed names, never indices; no
kind falls back to position.

The fingerprint excludes reviewed-catalog presentation locators but includes policy
semantics. Catalog artifacts can reuse a reviewed tenant overlay after a button is
renamed. Live groundings instead retain their discovered locators: layout changes
can preserve them, but a changed accessible name requires rediscovery. That is the
deliberate trade-off between the two modes: a reviewed vocabulary buys cross-tenant
reuse at the cost of onboarding, while live perception buys adaptability to unknown
labels at the cost of pinning the labels it saw, so a live artifact is tenant-specific
until a person promotes its groundings into the reviewed vocabulary. A permission
change needs a new artifact; schema-1 artifacts are rejected. Schema-3 groundings
are not silently reinterpreted as schema-2 presentation hints.

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
in any signature. The browser perception adapter still relies on Playwright and DOM semantics; it
does not establish operation on a pixel-only application. Perception and grounding
are the parts that must change for OS accessibility, screenshots or a terminal.
A second, deliberately basic execution surface makes the replay seam concrete:
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
update check failed closed the same way until its request rule was reviewed.

Persistence is a bounded projection: model reasoning, raw DOM, invocation values
and extracted business values are excluded, though stdout returns requested
outputs. Live-mode interface labels may reach both the model and grounded target
metadata under the reviewed text-export policy. That is a deliberate change from
the older catalog-only mode, not a claim that no page text ever leaves the browser. Screenshot CSS conceals text, inputs and media across
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
task *contract*. Product permissions, text-export scopes, input bindings, identity
invariants and output sources remain hand-reviewed. Live form discovery identifies
ordinary controls within those permissions. It does not infer business risk,
automatically approve a new application, or implement general screenshot grounding.

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