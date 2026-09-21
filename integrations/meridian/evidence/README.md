# Meridian Core evidence bundle

Recorded 2026-09-18 against the hosted target
`https://web-sample.interface-hiring.com` (interface.ai's own legacy-HTML
hiring sample, "MERIDIAN CORE — Member Services Platform v4.2.1"), using real
Chromium and a live Ollama `qwen3.6:35b-a3b` model for discovery. Credentials
were the app's own publicly listed demo operators (`teller1` / `password`,
branch `MAIN-001`), supplied at invocation and never stored — see the
[integration README](../README.md) for the exact commands.

| Run | Result | Model calls |
| --- | --- | --- |
| [Discovery](discovery/events.jsonl) | Savings lookup learned and saved as `../read_savings.json` | 9 |
| [Changed-input replay](replay_changed/events.jsonl) | Same capability, a different member, correct balance | 0 |
| [Not-found replay](replay_notfound/events.jsonl) | Business outcome `member_not_found`, no artifact needed | 0 |

`../read_savings.json` is the exact capability this discovery run emitted and
both replays consumed unmodified. Each run directory holds only its
structured `events.jsonl` and `result.json`, in the same allowlisted,
withheld-value format used by the repository's own root
[`evidence/`](../../../evidence/README.md) bundle: parameter *names* appear
(`member_id`, `operator`, `password`, `branch`), never values; `result.json`
reports `"<withheld>"` for the savings balance rather than the real figure.
No raw model messages, member numbers, names, balances or credentials appear
anywhere in this directory — verified with a plain `grep` over every value
used during capture (all five sample member numbers, both demo operator
names, the literal word "password" as a value, all three branch labels, and
every balance figure observed during exploration).

Two more genuine discovery attempts were made (different members/operators/
branches) beyond the one captured here; all three succeeded, 9 model calls
each. A third replay (same artifact, same member, run again) also succeeded
with zero model calls, confirming stability — its receipt is not duplicated
here since it is byte-for-byte the same shape as the changed-input replay.

Discovery cost 9 model calls because the site has no client-side JavaScript
at all (confirmed empty during exploration) and no accessible labels on its
form fields or table headers, so the model chose one action per real page
transition: fill operator, fill password, fill branch, sign on, open Member
Inquiry, fill the member number, search, select the one matching row, read
the balance. Nothing was retried or repeated.
