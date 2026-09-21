# Meridian Core savings lookup

This target is interface.ai's own hosted "Computer-Use Automation System"
hiring sample application, **MERIDIAN CORE — Member Services Platform v4.2.1**,
at `https://web-sample.interface-hiring.com` (redirects to `/signon`). It is a
deliberately legacy HTML 4.01 table-layout app: server-rendered, no
JavaScript anywhere (confirmed empty during exploration), no `<label>` or
`<th>` elements anywhere, a real `Set-Cookie`/302 login instead of a
single-page app, and F-key hints (`F3`/`F5`/`F7`/`F12`) that are cosmetic
text only — there is no keyboard handler to wire them to.

The capability signs on, looks up **one member by member number**, and reads
their **current Regular Shares (savings) balance** from the member record
screen. It never submits a state-changing form: transfer, open-share, update
and hold are all real links this profile deliberately never onboards.

## Rules of engagement

This is a **shared public demo** used by multiple candidates. Everything run
against it for this integration was: signed on with the app's own publicly
listed demo operators (`teller1` / `password`, `super1` / `password`), kept
to a handful of member lookups from the numbers the app itself suggests
(`100234`, `100987`, `101555`, `102777`, `103001`, plus one number chosen to
be absent, `999999`), read-only throughout (no transfer, no share opened, no
hold placed, no confirmation screen for anything state-changing), paced at
roughly one page load per second during manual exploration, and signed off
at the end of each session. Total page loads against the hosted app across
exploration, three genuine discovery attempts and three replays: well under
100, against the assignment's ~150 cap. Fault-injection states (validation,
permission-denied, session-timeout) were observed only through the single-
request `?inject=` query parameter the app's own `/settings` panel
documents as being **for exactly this purpose** ("simulate real runtime
conditions for testing automation robustness") — never through the panel's
persistent, shared `POST /settings` form, which would have changed behavior
for every other candidate using the same demo.

## Why a core change was needed

MERIDIAN's login is a genuine `POST /signon` → `302 Found` → `GET /menu`
(with a real `Set-Cookie`), and its unauthenticated entry `/` also 302s to
`/signon`. The engine's browser adapter refused **every** redirect
unconditionally, on purpose: `context.route()` cannot reliably re-intercept
a hop Chromium follows on its own, so letting Chromium auto-follow a redirect
would reach content outside policy review. That is correct for the two
existing targets, which are both single-page applications that never redirect
in normal operation.

A classic multi-page, form-posting site cannot be onboarded at all under a
blanket "no redirects" rule, so `RequestRule` gained one small, opt-in field:
`redirect_to`, a tuple of exact destination paths a specific reviewed rule may
redirect to. It defaults to empty, so every existing profile keeps failing
closed exactly as before (verified: `profiles/juniper.json` digests
identically before and after, and the existing
`test_allowed_destination_redirect_still_not_followed` test
is unchanged and still passes). A binding-level validator additionally
requires every declared `redirect_to` destination to independently be an
allowed GET route — a redirect grant cannot itself smuggle in a route that
was never reviewed. At runtime, a granted redirect is fetched and
policy-checked here, not handed to Chromium: exactly one hop, always as a
plain GET, still same-origin, still checked against the full route/query
allowlist. Only MERIDIAN's own `POST /signon → /menu` rule opts in
(`redirect_to: ["/menu"]`); every other request rule, and both other
bindings, opt in to nothing. See `docs/DESIGN_CHOICES.md` for how this fits
the rest of the policy model, and `src/computer_use_replay/browser.py`/`network.py`/
`policy.py` for the change itself.

## Why everything else needed no core change

The rest of the site's hostility — no `<label>`, no `<th>`, form fields
identified only by their `name` attribute, a table row identified only by an
exact `Type` cell's text, a whole-page nested layout table where a naive
`:has()` selector matches ancestor rows too — is expressed entirely with the
existing `css` target kind -- already part of the schema and exercised by the
unit/e2e suite, not invented for this target. The two read
targets that stand in for a "row header" this app doesn't actually have are:

```
"member_identity": {"kind": "css", "name": "td.lbl:text-is(\"Member No.:\") + td"}
"balance":         {"kind": "css", "name": "tr:has(> td:text-is(\"Regular Shares\")) > td:nth-child(3)"}
```

The `> td` (direct-child) form of `:has()` is deliberate: the whole page is
one outer layout table, and a plain `tr:has(td:text-is(...))` also matches
that outer table's own wrapper row, since `:has()` otherwise matches a
descendant at any depth — the exact "nested layout tables intentionally
contain the same descendants as inner data rows" hostility the repository's
own `tests/fixtures/legacy_vendor.py` fixture calls out. Verified directly
against saved snapshots of all five sample members before ever using it in a
live run (see Testing below).

## Discover and replay

```bash
export OLLAMA_URL=<your Ollama endpoint>
export OLLAMA_MODEL=qwen3.6:35b-a3b
uv run computer-use-replay discover \
  --target https://web-sample.interface-hiring.com \
  --binding integrations/meridian/profile.json \
  --request integrations/meridian/request.json \
  --inputs '{"operator":"teller1","password":"password","branch":"MAIN-001 - Main Office","member_id":"100234"}' \
  --artifact integrations/meridian/read_savings.json \
  --evidence runs/meridian-discovery

uv run computer-use-replay replay \
  --target https://web-sample.interface-hiring.com \
  --binding integrations/meridian/profile.json \
  --artifact integrations/meridian/read_savings.json \
  --inputs '{"operator":"teller1","password":"password","branch":"MAIN-001 - Main Office","member_id":"102777"}' \
  --evidence runs/meridian-replay
```

`branch` must be the option's **displayed label**, not its value attribute —
`select_option` in this codebase matches by label (`MAIN-001 - Main Office`,
not `MAIN-001`), because that is what the runtime checks after filling to
confirm the fill took.

The committed `read_savings.json` was genuinely learned by `qwen3.6:35b-a3b`
through native Ollama tool calls in **9 model calls**: fill operator, fill
password, fill branch, sign on, open Member Inquiry, fill the member number,
search, select the one matching result row, read the balance. Two further
discoveries on the same profile (different operator/branch/member) also
succeeded, 9 calls each — 3/3. All three replays (a different member, a
member number that does not exist, and the original member again for
stability) are in [`evidence/`](evidence/README.md).

## Scope and limits

- **Search is by member number only.** The app also offers "Search by: Last
  Name", which can return more than one row; that mode, and the ambiguity it
  would introduce, is out of scope and not in the reviewed vocabulary at all.
- **Savings means the `Regular Shares` row.** Every sampled member has
  exactly one; a member with none, or more than one, is not modeled — the
  `:has()` selector requires exactly one match and stops rather than
  guessing.
- **Nothing state-changing is onboarded.** Funds Transfer, Open New Share,
  Update Member Information and Place Account Hold are real links on the
  member record screen; none of their routes are in the allowlist, and none
  of their controls exist in this profile at all — not marked `human_only`,
  simply absent, since the read-only goal never needs them.
- **Business/human states reflect what the app actually shows**, not an
  invented taxonomy: a genuinely absent member number, when searched
  naturally, lands on `No member records matched your search.` on the same
  search screen (`member_not_found`, business outcome — no separate page).
  `validation_error` (`TRANSACTION REJECTED`) and `permission_denied`
  (`SUPERVISOR OVERRIDE REQUIRED`) were only reachable through the app's own
  single-request `?inject=` fault-injection feature, not through the
  read-only flow itself; they are still declared, matching the reviewed
  vocabulary's other bindings, which also declare more states than any one
  capability exercises. `session_expired` (`YOUR SESSION HAS TIMED OUT`) is
  declared as a human state whose only recovery is the real "Return to Sign
  On" link — resuming a lapsed session means re-entering credentials, which
  this engine correctly treats as requiring a person, not an automated retry.
- **No unexpected native dialog was ever observed** — there is no
  `confirm()`/`alert()`/`prompt()` anywhere on this surface (consistent with
  there being no JavaScript at all).

## What was hard

The two genuinely hard parts were: (1) realizing the site has **no** `<th>`
or `<label>` anywhere, so the repository's `row_value`/`row_input` target
kind (which requires a real `<th>`) could not be used at all, and (2)
discovering — the slow way, via a failed first discovery attempt with
`llm_calls: 0` — that the engine refused the login's own 302 redirect
outright, which is a real behavior of the target the engine had no vocabulary
to express, not a targeting mistake. Everything else (unlabeled `name`-only
inputs, a header row rendered as `<td>` instead of `<th>`, a nested layout
table where a naive `:has()` over-matches) was solvable with the existing
`css` target kind once verified offline against saved HTML, with zero
additional requests against the live site.
