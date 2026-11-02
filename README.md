# Ghost Ledger

Evidence-first **Supercharger billing** reconciliation for TeslaMate, with a private review dashboard. Home charging stays with TeslaMate at the owner's confirmed flat **$0.10/kWh**. Sentry USB support is not implemented.

## Working owner milestone

- Separate Fleet authorization, a small real history response and an invoice comparison verified.
- Manual importer with exact vehicle identity, unique start/end/charger-energy corroboration, paid USD billing validation and conservative unsupported-record rejection.
- Transactional missing-cost writes with a fresh scoped backup, pending evidence/audit, in-transaction revalidation, `cost IS NULL` protection and exact readback.
- Real owner-authorized updates independently verified against the complete charging-cost table; repeat import performed no further writes.
- LAN/VPN-only, read-only dashboard with login disabled at the owner's request installed on the owner's M1. Shows billed/current costs, imported sessions and unresolved conflicts. No public route.
- Own-token renewal and passive TeslaMate completion detection are installed; see [maintenance and detection](docs/passive-maintenance.md). No billing scheduler, unattended billing fetching/imports, vehicle polling/wake/commands, or HA credential reuse.

See [owner deployment, security and recovery](docs/owner-milestone.md).

## Manual commands

Use the Python 3.12 environment with `requirements.txt` installed. Cached history is the default:

```sh
python ledger.py preview
python ledger.py import --apply
```

Only an explicit `--fetch` makes one new latest-page Fleet request:

```sh
python ledger.py preview --fetch
# Or fetch, then explicitly import safely:
python ledger.py import --apply --fetch
```

Manual request allowance: **4/day, 100/month, UTC**, persistent and reserved before transport; failed requests count. This is a request cap, not a guaranteed dollar estimate or a controller for HA's separate usage. No retries or pagination. The original lifetime test-probe allowance is never reset. The passive service renews Ghost Ledger authorization automatically. If rotation is blocked/ambiguous or consent is revoked, separate sign-in/recovery is required; it never borrows HA tokens.

[Sign-in instructions](docs/sign-in.md) describe the registered loopback callback, private credentials and browser tunnel. Those commands are the original one-shot access probes, not the ongoing manual import interface.

## Verification

```sh
python -m unittest discover -s auth -v
python -m unittest discover -s charging_costs -v
python -m unittest test_ledger test_manual_reader test_owner_database test_workflow test_dashboard test_ledger_store test_dashboard_server -v
```

PostgreSQL integration tests require `GHOST_LEDGER_TEST_DSN` explicitly pointing to a **disposable loopback database named `synthetic`**. They create/drop synthetic tables. Never point them at production. Without it, those tests skip rather than touch a real database.

The original `charging_costs/reconcile.py` remains a minimal pure time-window preview. Production writes use the stronger `importer.py`/`postgres.py` path, not that early matcher.

## Boundaries

- Existing costs, including zero, are protected. Disagreements are visible, not silently corrected.
- Home charging and tariff configuration are untouched.
- Refunds/credits, foreign exchange, older pagination/backfill and unsupported billing semantics require separate work.
- A same-amount existing cost is not proof that this application imported it.
- Unknown commit outcomes block automatic retries; changed evidence after import requires review.
- No third GPS warehouse or indefinite invoice archive.
- Direct implementation; Qwen use remains stopped at the owner's request.

See [implementation plan](docs/plans/implementation.md).
