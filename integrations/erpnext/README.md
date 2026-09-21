# ERPNext quotation preparation

This target is the existing, unmodified [ERPNext](https://github.com/frappe/erpnext)
application, version 16.34.2. It is the complex-interaction check: two autocomplete
pickers with option selection (customer, item), a native `<select>` order-type
dropdown, opening an editable grid row, filling item + quantity inside it, closing
the row editor, and reading an asynchronously priced grand total including tax.

The capability prepares **one unsaved quotation** and reads its grand total. It
never saves or submits a quotation. Its scope is English, USD, a single line item,
one of the seeded positive-price items, and a fresh browser session.

## Disposable target

Use the official [Frappe Docker disposable deployment](https://github.com/frappe/frappe_docker/blob/a0c52135d4d41c4b8acf7adfdfc5bbcba46dd4d0/pwd.yml).
The tested ERPNext image is
`frappe/erpnext@sha256:2feeb8973c3581726b4abd451ccb69c18426ec547bd04f7a4fdee47f17d1c3f1`.

Download that compose file, pin its ERPNext images to the digest above, and bind
the frontend to `127.0.0.1:18080:8080`. Run its setup to completion. Its disposable
credentials are `Administrator` / `admin`; this is not a production deployment.

From this repository, seed the running deployment (adjust the compose file path):

```bash
docker compose -f /path/to/pwd.yml cp integrations/erpnext/seed.py backend:/tmp/computer-use-replay-seed.py
docker compose -f /path/to/pwd.yml exec -T backend /home/frappe/frappe-bench/env/bin/python /tmp/computer-use-replay-seed.py
```

The seed initializes a US manufacturing company, 24 synthetic customers and four
non-stock items. The standard US setup supplies the 6% sales tax used below.

If the disposable deployment runs on a different host than this repository,
any local port forward onto `127.0.0.1:18080` works the same way; nothing here
depends on how that port got there.

## Replay the learned artifact

```bash
ERPNEXT_PASSWORD=admin uv run computer-use-replay replay --integration erpnext \
  --input username=Administrator --input-env password=ERPNEXT_PASSWORD \
  --input customer_id=CP-CUSTOMER-012 --input item_id=CP-VALVE-100 \
  --input quantity=2 --input order_type=Sales
```

Expected grand total: **USD 371.00**, with **zero model calls**. Credentials and
identifiers are supplied at invocation time; the artifact stores symbolic input
references, not values.

An unrecognized `customer_id` (e.g. `CP-CUSTOMER-999`, never seeded) replays
the same artifact to a business outcome instead, still with zero model calls:
`{"status": "business_outcome", "code": "customer_not_found"}`, exit `0`. See
*Scope and limits* below.

## Background traffic

A real, unmodified application keeps issuing requests the learned flow never
asked for, and that traffic can change over time: this deployment started
sending Frappe's own periodic "an update is available" check,
`POST /api/method/frappe.utils.change_log.show_update_popup`, only days after
onboarding. It was not in `profile.json`'s `request_rules`, so the network
policy correctly failed closed rather than silently allowing an unreviewed
request; the reviewed fix is one discard rule for that exact path and method
(`profile.json`, alongside the existing `/socket.io/`, `/undefined` and
`/desk/quotation/undefined` entries) -- discarding aborts the request before
it is ever sent, and the quotation flow does not depend on its response.

## Learn a new artifact

`computer-use-replay discover --integration erpnext --request integrations/erpnext/request.json`
with the same inputs, a configured model, and a fresh `--artifact` path. The
committed artifact of record was genuinely learned by `qwen3.6:35b-a3b`
through native Ollama tool calls in 13 model calls, against the current
profile -- which has, over two rounds of change, gained
`customer_not_found`/`item_not_found` and then the update-check discard rule
above; each profile change moves the reviewed binding's digest, so each time
the capability was genuinely re-learned rather than hand-editing the prior
artifact's `binding_sha256`. The recorded
[discovery transcript](evidence/README.md) is tracked alongside it. An earlier
12-call `gemma4:e4b` recording (and its own fresh-browser replay/negative-case
validation run) predates those changes and is kept under
[`evidence/original/`](evidence/original/) for history, not as evidence for
the current artifact. A successful development run does not establish a
general discovery success rate.

## Scope and limits

Customer and item selection can redraw the page and close an open item editor;
the editable-grid control is available only once the customer name resolves, and
the quantity field only once a positive item price is displayed. Autocomplete
fills wait for the exact requested option -- these are observed application
states, not fixed sleeps (see `docs/DESIGN_CHOICES.md`). Save is human-only and
is not in the network allowlist.

An unrecognized `customer_id` or `item_id` is now a modeled business outcome,
not a checkpoint failure. Both autocomplete pickers always render a fixed
trailing option beside real matches -- "Create a new Customer" for the party
picker, an "applied filters" note for the item picker -- so presence alone
cannot tell a miss from a hit: it shows up in the known-record case too.
ERPNext auto-selects (`aria-selected="true"`) whichever option renders
*first*, and that fixed option is first only when zero real matches precede
it, so `customer_not_found`/`item_not_found` key off that option being the
selected one, not merely present. Declared as ordinary `business` states
(`profile.json`, same shape as `profiles/juniper.json`'s `member_not_found`)
resolved entirely from existing target/state kinds -- no engine change was
needed. A `replay` with an unrecognized identifier now exits `0` with
`{"status": "business_outcome", "code": "customer_not_found"}` (or
`item_not_found`) instead of stopping on `target_drift`; see
[`evidence/replay_not_found/`](evidence/replay_not_found/). This closes the
limitation for both pickers this capability actually drives; it is specific
to their markup and to `customer_id`/`item_id`'s declared, closed-shaped
patterns (`input_types` in `profile.json`) -- a differently shaped value is
already rejected as `invalid_input` before a browser ever opens, and a picker
elsewhere in ERPNext with different autocomplete markup would need its own
review, not an automatic guarantee.

See [evidence/README.md](evidence/README.md) for what was captured and when.
