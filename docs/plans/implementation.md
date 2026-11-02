# Charging Cost Reconciliation Implementation Plan

> **Execution:** Direct implementation. the owner stopped Qwen use for this project because latency threatens the Wednesday road-trip deadline. Automated tests remain the primary verification; do not dispatch further Qwen tasks for Ghost Ledger.

**Goal:** Import evidenced Tesla charging bills into the correct TeslaMate charging sessions, later supporting Sentry USB.

**Architecture:** Separate enrichment utility, not bridge functionality. Offline pure reconciliation first, supported Fleet reader second, dry-run session adapter third, approved narrow writer last. Keep existing home costs and unknown-origin/manual costs protected. Actual receipts beat tariff guesses; never conflate unknown with zero.

**Tech Stack:** Python 3.12, Decimal, timezone-aware datetime, stdlib unittest initially; existing isolated pytest interpreter available.

## Scope and boundaries
- Cost utility is top priority; do not change unfinished bridge files.
- Local code/tests and private GitHub commits/pushes authorized. Separate Fleet authorization, one history response and one invoice are verified. the owner now authorizes building the narrow TeslaMate missing-cost importer and simple dashboard. Preserve existing/manual/home costs, require backup and transactional readback for production writes. No HA token extraction or app permission/billing-limit changes.
- Current milestone includes a small review dashboard, explicitly LAN/VPN only WITH login. No public proxy or DNS exposure. Home remains fixed $0.10/kWh, no changes. No automatic scheduling, additional Fleet pagination/retries, tariff warehouse, FX, Sentry USB, or Traccar work in this milestone.
- Reuse audited MIT supported-Fleet reader patterns where useful; do not copy unlicensed projects. Official endpoint/account semantics must be verified before API implementation.
- Test fixtures are explicitly synthetic, not claimed to be Tesla API responses.

## Task 1: Test-first pure matcher (initial slice implemented)
Create standalone `charging_costs/reconcile.py` and `charging_costs/test_reconcile.py` without importing bridge modules.
Contract: `preview(bill: dict, sessions: list[dict], *, currency='USD', tolerance_seconds=300) -> dict`.
Bill normalized fields: `external_id`, `vin`, `started_at` (aware datetime), `amount` (Decimal), `currency`, `finalized` (bool).
Session normalized fields: `id` (positive integer), `vin`, `started_at` (aware datetime), `cost` (Decimal or None).
Return exactly `{'status': str, 'session_id': int or None, 'amount': Decimal or None}`.
Validate all inputs: malformed raises ValueError; finite nonnegative Decimal only (bool/float disallowed), nonblank identifiers, aware datetime, explicit currency, finalized bool, tolerance nonnegative finite number excluding bool. Duplicate session IDs reject. Inputs bounded to 1000 sessions; pure function, no IO.
Statuses: pending (not finalized), currency_mismatch, no_match, ambiguous, protected_existing, proposed. Non-proposed amount=None. A unique same-VIN start within inclusive tolerance can propose. Multiple qualifying sessions are ambiguous, never choose nearest automatically. Existing cost including zero is protected. Confirmed zero bill may propose. This minimal match is preview ONLY; production writes need independent end/location/energy evidence and revision policy.
RED: run tests before module exists; GREEN: implement incrementally; run full standalone tests and existing bridge regression suite.

## Task 2: Strengthen matching evidence
Add completed start/end interval, normalized billing/session identifiers, available energy/site corroboration, conflict and split-session refusal. Missing evidence blocks writes, not silently accepted. Add table-driven synthetic tests first. Keep original amount/currency and no FX guessing.

## Task 3: Supported reader and authorization plan
Recheck Tesla Fleet history/invoice documentation and current MIT readers; verify HA supported reuse without token extraction. Present minimal consent, request budget and read-only sample plan for approval. Never enable vehicle commands despite bundled permission.

## Task 4: Read-only adapter / preview CLI
After separate read-only DB authorization, bounded session query by explicit car mapping/time window. Preview produces redacted summary and evidence, no SQL mutation. Account sample validated against matching invoice before fee semantic assumptions.

## Task 5: Approved writer
After explicit write approval and backup: transactional compare-and-set on intended session/cost, protect manual/home costs, durable bounded idempotence/provenance, read back exact target. Revisions/refunds/fee inclusion decisions first. No core schema redesign or indefinite invoice warehouse.

## Task 6: Sentry USB
Inspect supported import/API interface independently. Add second adapter only after TeslaMate workflow is correct. No inferred compatibility.

## Road-trip critical path (the planned trip)
1. Finish runnable sign-in and single-request billing probe, with loopback callback, separate grant, private token state, strict Tesla endpoint allowlist, no redirects/proxies/retries, and durable request reservation before transmission.
2. User registers additional callback and signs in; validate one real history response against one invoice before assuming financial semantics. Existing HA app and grant stay intact. Localhost callback requires browser on runner machine or a loopback SSH tunnel; never expose callback listener to LAN.
3. Read-only TeslaMate session comparison, then narrow approved writer with backups, compare-and-set, idempotence and readback. Protect existing/manual/home costs; ambiguous or pending bills remain unresolved.
4. Configure conservative bounded billing catch-up using existing local charging state; independent request allowance below shared account budget. Dashboard usage remains authoritative. Do not claim this controls HA's other consumption.
5. Exercise installed process/restart behavior before claiming road-trip readiness. Sentry USB, modular adapters and bridge completion remain deferred. The simple private review dashboard is now part of the approved current milestone.

## Verified owner milestone completion

The narrow importer and LAN/VPN login-protected dashboard are implemented and exercised. Real missing-cost updates, independent full cost-table comparison, repeat no-op import, dashboard authenticated/anonymous boundaries, mobile layout and LaunchAgent restart have been verified. Existing disagreements remain conflicts and home stays fixed10c/kWh. No scheduler or unattended catch-up was enabled. Manual `ledger.py` supports cached preview/import and an explicit single-page `--fetch` with separate durable4/day100/month UTC request guards; expired tokens require separate sign-in until refresh is implemented later. See `docs/owner-milestone.md` and README for the exact owner runtime.
