# Design choices

This document explains the implementation choices behind `computer-use-replay`. `REPORT.md`
is the short assignment submission; this page records the boundaries a maintainer
needs when changing the system.

## Authority is split across three inputs

`computer-use-replay` keeps product policy, a caller's goal, and tenant presentation separate:

| Input | Owns | Cannot change |
| --- | --- | --- |
| Product binding | Logical controls, permitted operations, risk, routes, states, input types and identity rules | A task sequence or capability name |
| Goal request | Capability name, natural-language goal, typed inputs/outputs and final checkpoint | Execution authority or policy |
| Tenant presentation | Locator descriptions and frame mappings for existing controls | Actions, risk, recovery, outputs or checkpoints |

A request contains a contract, not a sequence of browser actions: the model chooses
from the controls visible in the current observation. Each fill control declares its
allowed input names; the model, compiler, saved-artifact validator and browser
runtime all enforce the target/parameter pair, and a tenant overlay cannot add a
control, route, action or permission. The product binding digest excludes
presentation locators but includes control keys, operations, risk, descriptions,
state definitions, input types, identity invariants, routes and budgets, so
presentation changes never invalidate an artifact while the logical product
contract is unchanged -- a changed meaning, navigation sequence or permission set
requires a new reviewed capability, and schema 2 rejects schema-1 artifacts rather
than reinterpreting their binding digest.

## Capabilities are typed plans, not model transcripts

A capability names its typed inputs and outputs, ordered discriminated actions,
logical targets, completion conditions, product fingerprint and discovery
provenance. `targets` are review hints from discovery, with no authority to bypass
policy -- runtime execution always resolves logical keys through the current
reviewed binding instead.

Input validation happens before browser creation. Identifiers use configured
full-match patterns and preserve leading zeroes; integers and booleans reject
coercion; text is bounded and canonical. Invalid caller data returns `invalid_input`
with no screenshot or intervention; configuration failures use a separate exit code.

Output sources are declared by the goal request. Reads are offered only at the
requested checkpoint and are type checked; money is parsed as a decimal amount and
currency. Product identity remains mandatory for tasks using a member identifier;
sub-account review also requires the requested nickname and `Ready for confirmation`.
Replay checks the complete checkpoint group before reading and again before success --
a model `finish` call cannot skip a missing output or condition. The shipped banking
capabilities stop before the final creation action; it is present in the fixture so
the boundary is testable, but its route is outside the allowlist and human-only.

## Discovery and replay have different responsibilities

Discovery observes the live surface, offers only currently visible and permitted
controls, verifies each action's effect, and compiles a capability only after all
outputs and checkpoints pass. It never sends caller values or extracted business
data into the model context; model decisions carry symbolic parameter names,
reviewed control keys and coarse state information. Replay does not import the
model client or planner at all -- it resolves each logical target, requires one
visible match, performs the action, observes its effect and applies bounded waits
and approved reversible recovery, with an uncertain action observed rather than
blindly repeated. Output collection is terminal: automated actions after a read
are rejected.

Business outcomes such as missing records, validation rejection and permission
denial are distinct from technical failures and return a machine-readable result.
Missing or duplicate controls, unexpected dialogs, failed effects and unrecognized
requests stop or enter handoff; diagnostics identify the step, expected and
observed condition, logical target, configured frame/label and match count without
retaining caller data. Live discovery writes attempts, model responses and
screenshots only to a private `runs/` directory; the two committed capabilities are
functional examples, not a model reliability estimate.

## Saved capabilities double as an agent-facing catalog

`computer-use-replay catalog --capabilities <dir>` turns every saved artifact into a typed
tool/function definition: name, a neutral description built only from the declared
name plus typed input/output names (a `Capability` never persists the discovery goal
text, so there's nothing to invent prose from), a JSON-Schema `parameters` object
per `Input`, an `output` schema (money as `{amount, currency}`, text with
`allowed_values` as an enum where declared), the possible `result_statuses`
(`success`, `business_outcome` with the product's declared codes, `failure`), and
the artifact's own digest. Every input/output is marked `x-sensitive: true` and none
is ever given an example value. `computer-use-replay invoke <name> --capabilities <dir>
--args '<json>'` looks a capability up by name and runs the *identical* replay path
`replay` already uses -- invoke never forks the engine, only adds a name-to-path
lookup ahead of the same code; a name resolving to zero or more than one saved
artifact fails as a configuration error, before any browser opens.

### What the English goal does, measured

The task contract defines success; the goal selects among legal paths. A development
check held `requests/read_savings.json` fixed and varied only `--goal`, three live
local-model runs each:

| Goal text | Result |
| --- | --- |
| The request's own savings goal | 3/3 success, identical six-step capability |
| An irrelevant sentence | 3/3 success, the same six steps |
| The sub-account goal | 3/3 stopped: the model chose the sub-account control, could not satisfy the savings contract there, requested help; no artifact |

So an irrelevant goal is harmless because the contract and visible controls carry
the task, the goal does steer the model at a genuine fork, and a goal that conflicts
with the contract fails closed instead of producing a wrong capability -- a small
sample on one fixture, not a general claim. Reproduce with `discover --request
requests/read_savings.json --goal '<a conflicting goal>' --inputs
'{"member_id":"00123"}' --artifact runs/goal-check.json`.

## Targeting favors explicit observation, with a verified fallback ladder

The browser adapter resolves exact accessible roles/names, labels or value cells in
the same row as an exact reviewed header. Named frame lineages prevent a matching
control in another frame from being selected, every match must be unique and
visible, and the model cannot generate a CSS or coordinate fallback. A reviewed
presentation may supply one installation-owned CSS selector for a field without an
accessible name, still requiring the expected frame, visibility and uniqueness; a
`match_input` target can select the exact requested text within that reviewed scope,
with invocation values escaped and never inserted into model context, observations
or saved selectors. Fills support text fields and native single-selects (selects
require one enabled option with the exact displayed label, checked afterward); reads
use input/textarea values, selected labels or ordinary element text rather than an
entire control catalog. Navigation invalidates observations crossing a document
change, so the adapter refreshes them within a deadline.

A control's reviewed presentation may also declare an ordered list of **alternate**
targets, same strict kinds as the primary. Resolution tries the primary first; only
on ZERO visible matches does it try each alternate in order, and every rung still
requires exactly one visible match, so an ambiguous rung stops immediately rather
than trying the next one -- nothing weaker than the same strict kinds is ever tried.
A resolved alternate is recorded, not silently absorbed (`alternate_resolved`
target/rank, a rank-drift signal alongside `presentation_drift`). Alternates are
presentation, excluded from the fingerprint and never copied into a saved artifact's
`targets`; only `Control` (policy) and `Presentation` (overlay) carry the field, and
an overlay still cannot touch actions, risk or checkpoints through it.
`count_equals_input` stays primary-only, since consulting alternates there would
leave it ambiguous which rung's count is asserted. See REPORT.md §2 and §4, and
`profiles/tenant_b.json` for a worked example.

The owner found strict-only targeting too brittle for a small relabel that
never reaches a human reviewer before the next run: a rename as trivial as
"Find member" -> "FIND MEMBER..." used to stop the whole run as
`target_drift`. The fix is not a weaker match kind everywhere -- it is a
**verified fallback ladder**, reached only at the exact moment the engine
would otherwise raise `target_drift` for the CURRENT step's own acting
target (`Execution.step()` awaits `Condition(target=step.target)` before
`perform()`; see `Execution.settle()`'s `acting_op` parameter and
`Execution._rescue()`), and only after the primary and every reviewed
alternate already have zero visible matches. It is never consulted by
`observe()`, by state classification (business/human/interstitial/transient/
failure), by a general `condition()` check, by a click's own `after`
postcondition, or by a checkpoint -- those all call `settle()`/`condition()`
without `acting_op` set, so the ladder is architecturally unreachable from
them, not merely unused by convention. This matters because those are exactly
the code paths that decide whether a run is a *success*: a fuzzy match could
otherwise turn an ambiguous or wrong control into a manufactured business
outcome or a manufactured checkpoint pass. The ladder only ever helps the
engine find the SAME control it was already told to act on; it can never
change what counts as having acted on it correctly.

### Resolution order, and why the ladder is normalization-only

Targeting resolves in one fixed order: the **primary** reviewed target, then
each **reviewed alternate** in turn (a person's own decision, §"Reviewed
alternate" above), and only once every one of those has zero visible matches,
the **verified fallback ladder** -- and the ladder itself has exactly one
rung, `normalized`, for kinds `role`/`label`/`row_value`/`row_input` (never
`css`, which has no accessible-name convention to match at all, and never the
terminal surface's own screen-only concerns beyond its own `find_fallback`).
`normalized` means: Unicode NFKC, casefold, strip purely decorative
punctuation (an ellipsis "…"/"...", a colon, an asterisk, guillemets "»"/"›",
parentheses), collapse whitespace, then an exact string match -- see
`fallback.normalize()`/`fallback.names_equal()`. It runs for any operation
(`click`, `fill`, `read` alike). Critically, normalization can **never** drop
a letter, a digit or a whole word, so it can only absorb *decoration*: "Find
member 00123", "Find a member", "Do not Find member", "Find member and
delete" and "Find members" all still differ from "Find member" after it runs,
exactly as they did before.

**An earlier version of this ladder also tried a second, looser `similar`
rung** (token-subset or Jaccard similarity) for clicks, reasoning that a
click's own observed postcondition would catch a wrong guess. An external
review of the shipped code found this reasoning was wrong, in real Chromium:
given two buttons "Do not Find member" and "Find member and delete", both
token-subset-match the reviewed "Find member" and get clicked, and the
replay reports success once the expected postcondition appears -- because **a
postcondition verifies the effect it was told to look for, it cannot see an
ADDITIONAL, unintended one that a wrong click also caused.** "Find member" and
"Do not Find member" are opposite actions; "Find member" and "Find member and
delete" is the wanted action plus an unwanted destructive one -- silently. That
is a correctness and safety bug, not a tuning problem, and no similarity
threshold fixes it: any rung that can select a DIFFERENT action must never be
allowed to act automatically. The `similar` rung is therefore removed
entirely, not narrowed. A diagnostic-only version -- detect a similar
candidate, never act on it, only flag it for human review -- was considered,
but doing that correctly on both surfaces (a same-role/same-frame/
not-owned-by-another-control query, a new failure/event field, and the
coverage this repository requires for every new branch on both the browser
and terminal adapters) cost well past a reasonable size for what it bought;
a relabel wide enough to fail `normalized` already gets a `target_drift`
failure naming the drifted control, which is enough for a person to go look.
Normalization is now the only rung the ladder has, on both surfaces.

Every match still requires exactly one visible candidate in the same role and
frame lineage as the primary -- more than one match on the ladder stops the
run as `ambiguous_target`, exactly like an ambiguous reviewed alternate --
and a candidate that coincides with what any OTHER reviewed control's own
primary or alternate currently resolves to is refused outright, never
stolen. `policy.check_action` still gates the whole attempt, so
`human_only`/`blocked` controls and disallowed operations never reach the
ladder at all.

This is safe to leave on by default (`fallback: "verified"` on `Binding`/
`Presentation`, same authority boundary and fingerprint exclusion as
`alternates`) precisely because it changes nothing about how the engine
decides success: the click's own postcondition, a fill's read-back, output
parsing and every checkpoint are unmodified and stay exactly as strict as
before, and policy is keyed by control key, not by locator, so a rescued
control still needs its declared operation and risk. A rescue is never
silent: it logs one `fallback_resolved` event (target, rung, op -- never the
matched text), adds a `warnings` entry to the result (empty-list omitted, so
every previously committed result stays byte-identical), and writes
`fallback_review.json` into the run's evidence. That file is an allowlisted
projection, same rule as every other persisted artifact (`evidence.py`'s
module docstring): for each rescued control, its own control key, the op and
rung that rescued it, the **REVIEWED** target already present in the profile
(kind/role/frames/name), and a fixed instruction string asking a person to
open the application and, if the current wording is acceptable, add it as a
reviewed alternate by hand -- never the text a surface actually observed, its
length, a hash of it, or any statistic derived from it. Nothing is ever
written back to a profile automatically. `--fallback off` (CLI:
replay/invoke/discover/demo) restores today's strict behavior for callers who
want it. See REPORT.md §2-4 for the caller-facing summary and
`tests/e2e/test_fallback_targets.py`/`tests/unit/test_fallback.py`/
`tests/unit/test_terminal.py` for the guarantees above as executable tests,
including a canary that greps every evidence file and the caller's own
response for an observed candidate's text and finds none.

The runtime also exposes native form readiness without field values, re-evaluating
required/validity checks at dispatch so a form change during model inference can't
produce an empty submit. Optional request rules use full-match RE2 paths, explicit
methods, parameter names and (for POST bodies) nested-document constraints; requests
to another origin, unlisted routes, unexpected windows, downloads, native
confirmations and WebSockets all fail closed. Redirects are the one further
exception, only when explicitly reviewed: a server-rendered target's own redirect is
followed by the policy layer itself, never the browser. `RequestRule.redirect_to`
lists specific, same-origin relative destinations one rule may hop to, empty by
default so every existing binding keeps failing closed exactly as before (Meridian
Core's `/signon` login is the one rule that grants `/menu`). The destination must
independently clear full policy as an ordinary request first, and the adapter
fetches it itself rather than letting Chromium follow `Location`, so no unreviewed
hop in a longer chain is ever observed -- at the cost of one known fidelity gap: the
page shows the destination content while the browser's own address stays the
original URL. See REPORT.md §6 and `tests/e2e/test_redirect_grant.py`.

ERPNext's quotation profile is a worked example of state-gated waiting rather than
fixed sleeps. Its editable-grid `edit_item` target only exists in the DOM once the
customer name has resolved (`body:has([data-fieldname=customer_name]:visible) ...`);
its `quantity` target is scoped to the grid row whose price cell no longer reads
`$ 0.00`, so a fill can never race ERPNext's own asynchronous pricing call and land
on the wrong row; and each autocomplete pick (`customer_option`, `item_option`) is
an ordinary `click` whose `after` condition is the resolved field's value, polled to
its own timeout like every other condition in this document -- never a delay. These
are observed application states, exercised against a real, asynchronously-updating
third-party UI instead of a fixture built to be observed.

## Heterogeneity is explicit and bounded

The adapter depends on a `Surface` interface rather than one DOM shape. The included
legacy fixture exercises framed and inline content, unlabelled inputs, misleading
siblings, nested layout tables, changed defaults, reversed row order and repeated
records, with exact row headers and direct-child cells avoiding enclosing-table
ambiguity. The same logical capabilities replay on the second tenant presentation
after label and frame changes -- a bounded reuse case, not a claim of arbitrary
vendor compatibility or native desktop support.

A screen-buffer surface (`terminal.py`, `ScreenSurface`) was the cheapest honest
way to prove the `Surface` seam extends past one adapter, for the reasons in
REPORT.md §4: it needs no new external dependency, no real terminal-protocol
stack to fake, and no plausible route to accidentally reusing browser code --
so a passing replay demonstrates the abstraction, not a shared implementation
detail. It also keeps the claim honest by construction: the committed artifact
either replays byte-unchanged through a genuinely different `Target.kind`
(`screen`, resolved against an in-process screen buffer instead of a DOM), or
it does not, with nothing in between to fudge. A fuller adapter (OS
accessibility tree, a real 3270/5250 client, or a vision model) would answer a
different, harder question -- production readiness on that surface -- which
this brief asks be designed, not built.

Genuine local-model check (`qwen3.6:35b-a3b` via Ollama, live, never scripted):
one discovery attempt of `read_savings` run directly against `ScreenSurface`
(no browser, no HTML) succeeded first try, 6 native tool calls, and the
learned steps and checkpoint were identical to the committed web-learned
`capabilities/read_savings.json` -- the same evidence the scripted-planner
test in `tests/unit/test_terminal.py` already gives deterministically, this
time from a real model choosing actions over the terminal's own catalog. Not
part of the tracked evidence bundle; a local, one-off sanity check.

Drift between a recorded target hint and the current reviewed presentation gets a
signal, not a fallback: right after policy checks pass, `Policy.presentation_drift`
diffs every recorded hint against the current presentation and replay logs one
`presentation_drift` event naming the differing keys, informational on the success
path. A step's own target or click postcondition resolving to zero matches, with no
declared state, instead reports `target_drift` -- the usual locator diagnostics plus
whether that hint differs from the current presentation -- so a reviewer checks one
presentation entry rather than a weaker locator silently acting on the wrong control.
`--present` captions a `presentation_drift` event when one fires (REPORT.md §4).

Repeated business rows use a reviewed container selector and identity anchor: the
anchor names the invocation parameter used to select a row, and the target resolves
inside that row without an encoded row index. Hidden or duplicate anchors are errors,
and the parameter reference and permitted operations remain product policy, so a
tenant cannot broaden the scope. Numeric checkpoints are explicit too:
`equals_integer_input` compares formatted display values to an integer input using
decimal arithmetic, accepting only zero-valued fractional tails and bounded
grouping, and rejecting scientific or malformed forms; `count_equals_input` checks
the number of visible matches without weakening the one-target rule -- it describes
the current rendered page, and pagination or a remote total needs a separate
contract.

## Completion and recovery are state machines

When several declared states are visible in the same observation, `Execution.settle`
picks which one to act on by **kind**, not by where it sits in the binding's `states`
mapping: failure outranks human, which outranks interstitial, which outranks
business, which outranks transient. The binding's own declaration order only breaks
a tie within a kind -- authoring order, not a safety ranking -- so it never decides
whether a run reports a business outcome, a hard failure, a human escalation or an
interstitial recovery: never a business outcome while the app also shows a hard
failure or asks for a person, and always clear a known interstitial before trusting
what's behind it. Discovery and replay both observe through the same `settle`, so
this precedence is one rule, not two.

Ownership has automation, paused, human and closed states. An intervention
identifies the capability, step, reason, session and evidence reference; claim
issues a lease, resume requires that lease and a freshly valid complete checkpoint
group, and stale claims, concurrent automation and early resume all fail. The
operator works in the original browser context, claimed via `--human`'s terminal
prompt, and only the declared recovery action for the blocking state is offered -- a
human cannot turn a business action into a recovery or silently produce a replayable
capability. `computer-use-replay demo`'s scripted same-session operator exercises the same
claim/resume path with no person present, and `--present` labels it as such
("operator (scripted) renewed the session") rather than claiming person-operated
usability. Recovery validates every final condition in one polling group, never
reuses a condition that passed on an earlier poll, and never resumes while an
unexpected dialog or in-flight action remains.

## Privacy and persistence are allowlisted

Events and snapshots use typed, allowlisted fields, and exceptions become stable
codes. Parameter values, raw model messages, output values, raw DOM, URLs and
operator tokens are excluded from persisted receipts; caller stdout may return
requested outputs. Failure screenshots apply masking across frames, including
generated content, media, background images, text shadows and form controls -- if
masking cannot be captured, JSON diagnostics remain and no raw screenshot fallback
is used. The runtime writes receipts only to the caller's chosen output directory;
the source tree contains the two functional capability JSONs and no generated
browser, model or operator receipts. Business outcomes, caller errors and technical
failures stay distinct result kinds in every run's own `result.json`; drift is
visible as events and failure codes per run, not aggregated across runs.

## Provider and retry boundaries

Ollama is the model-backed discovery path; OpenAI is a separately tested cloud wire
adapter. Both use the same offered-tool validation, response-size limits, bounded
deadlines, cancellation and selective transport retries; a transport retry never
repeats a browser action, and cloud endpoints/credentials are never logged. Replay
needs neither provider configuration nor planner imports. A malformed model tool
response may receive a bounded replacement prediction when the caller enables
decision retries: the replacement sees the same observation and offered schemas
plus a fixed rejection reason, and no argument or action is repaired locally. This
retry budget is separate from transient HTTP retries, shares the enclosing deadline
and still passes the normal parser and policy checks; exhaustion remains an
`invalid_model_response` failure.

## Product-specific extensions

Some date widgets require leaving a field before the application commits it. A
reviewed fill may set `commit_key: "Tab"`; the runtime sends one Tab and still
requires the application checkpoint -- never applied universally, never Enter.
Reusable editors can expose several symbolic parameters as alternatives; the runtime
reports which previously assigned references still match the current field without
exposing their values -- a prior fill does not certify the value remains, and replay
independently verifies the full checkpoint group regardless.

The implementation deliberately omits queues, dashboards, code generation,
model-assisted replay, native desktop execution, vision-based grounding, a genuine
frontier-model discovery run and automatic cross-version migration. `computer-use-replay
catalog` is a directory listing, not a registry, and does not chain capabilities
together. The reviewed vocabulary still costs onboarding time to author once per
product; goal-only onboarding (below) only removes the cost of hand-typing each task
*contract* against it.

## Goal-only onboarding, measured

`computer-use-replay propose` sends the model exactly two things: the goal sentence and a
catalog built from the reviewed binding -- control keys, labels, permitted
operations, declared input types, which controls are readable, their declared
`allowed_output_values` when any, and screen headings; no routes, no risk, no
invariants, no page content, no invocation values. It must call one native tool,
`propose_contract`, whose `name`/`inputs`/`outputs[].source`/`success_screen`
arguments are constrained to enums built from that same catalog; product policy's
mandatory identity invariants are appended afterward, by `onboarding.draft_request`,
never left to the model. The result runs through the identical `Policy.check_request`
a hand-written `requests/*.json` goes through, then is written as a draft request
(`"review": {"status": "draft", ...}`) -- refused everywhere until `--accept-draft`
or an interactive TTY prompt records acceptance.

Genuine local-model check (`qwen3.6:35b-a3b` via Ollama, live, never scripted), three
attempts per goal, propose -> accept -> discover -> replay with a changed member each
time:

| Goal | Proposal valid | Matches hand-written contract | Discovery | Replay (different member) | Calls |
| --- | --- | --- | --- | --- | --- |
| g1: look up a member, read their savings balance | 3/3 | 3/3 same inputs/output-source/checkpoint; model's own capability/output names differ (`--name` pins this) | 3/3 success, 6 steps | 3/3 success, 0 model calls | 1 proposal + 6 discovery |
| g2: prepare a sub-account, stop at confirmation | 3/3 | 3/3 identical to `requests/prepare_subaccount.json` (minus provenance) | 3/3 success, 8 steps | 3/3 success, 0 model calls | 1 proposal + 8 discovery |

6/6 proposals were schema-valid on the first attempt, against the full 13-control
catalog with no filtering -- the nested `outputs[]` array-of-objects tool argument
that looked like the main reliability risk during design was not one in practice.
One genuine goal-only run (g1, propose + accept + discover) is the tracked sample
in `evidence/goal_only/`; every other run above is local-only.
