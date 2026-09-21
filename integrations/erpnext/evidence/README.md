# ERPNext capture evidence

Captured against a disposable ERPNext **16.34.2** deployment (the
`frappe/erpnext` image pinned in the integration README), reached over a local
port forward, using its documented disposable `Administrator`/`admin`
credentials. Nothing here is special or offline: any reviewer can bring up the
same disposable deployment (see the *Disposable target* section of the
[integration README](../README.md)) and reproduce this evidence the same way
it was captured, with `uv run computer-use-replay discover` / `replay`.

- `discovery.jsonl` / `discovery-result.json` — the genuine discovery
  referenced by [`../prepare_quotation.json`](../prepare_quotation.json), the
  artifact of record: `qwen3.6:35b-a3b`, 13 native tool calls, run
  `b89a1a5ad884461ebf84e604b3140e1a`, learned against the profile after it
  gained one discard rule for Frappe's periodic update-check request (see
  *Why this evidence was refreshed* below).
- `replay_current/` — one fresh replay of the committed artifact, zero model
  calls, matching the documented scenario in the integration README.
- `replay_not_found/` — one fresh replay with an unrecognized `customer_id`
  (`CP-CUSTOMER-999`, never seeded), zero model calls:
  `{"status": "business_outcome", "code": "customer_not_found"}`. Captures the
  `customer_not_found` business state added to
  [`../profile.json`](../profile.json) (see REPORT.md and the integration
  README's *Scope and limits* section). Repeated live (not just this captured
  run) for stability; the events log stops at the `customer` fill, so no
  identifier, credential, or total ever reaches it.
- `original/` — the artifact and evidence this integration shipped with
  before `profile.json` gained `customer_not_found`/`item_not_found`: a
  12-call `gemma4:e4b` discovery (`discovery.jsonl` / `discovery-result.json`).
  Superseded, not wrong: it is kept so the earlier development history stays
  inspectable, not as evidence for the current artifact. (A `validation.json`
  once sat alongside it -- a 12-fresh-browser-replay plus 4-case-negative-matrix
  run pinned to a specific past source tree via `source_sha256`. That snapshot
  was already unreproducible against current source before this rename, since
  several of the modules it hashed no longer exist, and the rename would only
  have made its recorded paths stale too; it has been removed rather than
  patched.)

## Why this evidence was refreshed

The disposable ERPNext deployment started issuing a periodic background
request days after onboarding -- Frappe's own "an update is available" check,
`POST /api/method/frappe.utils.change_log.show_update_popup` (no query or
body fields). It was not in `profile.json`'s `request_rules`, so the network
policy correctly failed closed on it (`network_policy` at the `customer` fill
step); this was not a code regression. The fix is one reviewed discard rule
for that exact path/method (see `../profile.json` and `../README.md`); a spy
run confirmed it is the only request ERPNext's login-through-quotation flow
ever issues that was not already covered, and that discarding it does not
newly deny any other request.

A network-rule change moves the reviewed binding's own digest, so the
committed artifact's `binding_sha256` stopped matching and the capability was
genuinely re-learned (not hand-edited) against the updated profile -- same
model and call count as before (`qwen3.6:35b-a3b`, 13 native tool calls), so
this is the second time this integration's evidence has been re-pinned for
that reason (see `../../../tests/unit/test_erpnext.py`). The previous
`qwen3.6:35b-a3b` discovery this superseded (run
`9e907712d99b4283bacda067d747ffaf`, captured 2026-09-19) is not kept
separately: its events, result and replay evidence were identical in every
way that matters -- same model, same 13 calls, same step sequence, same
outcomes -- and differed only in `binding_sha256`, `run_id` and timestamps,
so a `superseded/` copy would duplicate this file rather than add
information. `original/` above already covers the integration's actual
development history (the different, 12-call `gemma4:e4b` recording); nothing
else needed a second copy.
