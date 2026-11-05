# Ghost Ledger

**Reconcile actual Tesla Supercharger bills with TeslaMate charging costs—without polling, waking, or commanding the vehicle.**

Ghost Ledger combines a conservative billing matcher, an auditable PostgreSQL writer, a private dashboard, and optional event-driven automation. It can fill missing costs or, with an explicit date-bounded opt-in, replace manually entered estimates with corroborated Tesla bills.

This is a self-hosted, deployment-specific tool—not a hosted service, payment processor, or drop-in TeslaMate plugin. Review the database adapter and financial safeguards before connecting it to your installation.

## Features

- **Actual billing, not guessed tariffs.** Match finalized, supported USD Tesla billing records using vehicle identity, session start/end times, and charger-side energy.
- **Safe defaults.** Existing costs—including zero—are protected unless the operator explicitly enables Tesla-bill authority for a specified date range.
- **Optional estimate replacement.** Replace eligible existing amounts with actual bills, preserving the original amount, replacement, difference, and billing reference in the private audit.
- **Event-driven reconciliation.** Observe TeslaMate charging-completion states over MQTT, corroborate them against PostgreSQL, then check for a matching bill on a bounded schedule.
- **Daily safety net.** Find recent completed non-home sessions with missing costs, including sessions missed during subscriber downtime.
- **Private dashboard.** Review matched bills, current costs, import results, and discrepancies. Optional receipt-total entry supports non-Tesla providers without pretending the receipt came from Tesla.
- **Separate authorization.** Obtain and renew this application's own Fleet grant; never reuse Home Assistant or TeslaMate access/refresh tokens.
- **Bounded requests and queues.** Durable request reservations, finite retry slots, bounded pagination, and bounded completion queues.
- **Optional Pushover alerts.** Report new imports, saved receipt totals, and actionable problems, with deduplication and failure isolation.

## How it works

```text
TeslaMate MQTT state ──> passive subscriber ──> database-confirmed completion
                                                    │
                                                    v
                                            durable billing schedule
                                                    │
Tesla charging history ─────────────────────────────┤
                                                    v
                                      identity/time/energy validation
                                                    │
                                                    v
                                  backup + pending audit + locked recheck
                                                    │
                                                    v
                                      guarded TeslaMate cost update
                                                    │
                                                    v
                                   independent readback + private ledger
                                                    │
                                           dashboard / notifications
```

The subscriber and billing worker are separate processes so billing/database operations do not block MQTT callbacks. Neither process publishes MQTT commands. A non-home or unknown-geofence session is only a candidate: it is **not** assumed to be a Supercharger session without a corroborated bill.

## Cost policy

| Situation | Default | Explicit Tesla-bill authority |
|---|---|---|
| Matched completed session, missing cost | Import supported actual bill | Import if within cutoff |
| Matched session, existing cost differs | Preserve and flag discrepancy | Replace if within cutoff and unchanged since inspection |
| Existing cost equals bill | No write; no invented ownership | No write; no invented ownership |
| Existing zero estimate / finalized zero-dollar bill | Existing value protected / valid missing-cost import | Supported under the same matching safeguards |
| Session before authority cutoff | Existing costs protected | Preview-only, including missing costs |
| Home charging | Never changed | Never changed |
| Ambiguous, incomplete, unmatched, or unsupported record | No write | No write |
| Uncertain prior commit or changed previously imported evidence | Requires review | Requires review |

Automatic work is also restricted to the **exact due session IDs**. Other bills returned on the same page do not authorize extra writes. Daily discovery remains missing-cost-only; enabling authority does not launch historical replacement or discover every old estimate.

### Financial safeguards

Every production import uses the stronger `charging_costs/importer.py` and `charging_costs/postgres.py` path:

1. Validate supported finalized billing semantics, currency, totals, identity, timestamps, and energy basis. Charger energy is compared with energy drawn, not battery energy added.
2. Reject duplicate billing IDs, multiple plausible matches, and multiple bills targeting the same session.
3. Take a validated, scoped PostgreSQL backup before applying changes.
4. Persist private operation evidence and a pending audit event **before** writing.
5. Rebuild the full matching batch under serializable transaction locks. Replacement additionally validates the exact previous cost and session-identity snapshot.
6. Use a compare-and-set update and read back the target amount inside the transaction.
7. Independently verify committed state before recording success.

A timeout or crash around commit is **not** permission to retry. Pending/uncertain operations block further writes until reviewed. Backups and audit records contain sensitive data and stay outside Git.

## Prerequisites and integration limits

- Python **3.12+** and the pinned dependencies in [`requirements.txt`](requirements.txt).
- An existing TeslaMate PostgreSQL database and MQTT broker, plus authorized read/write database access for the narrow charging-cost operation.
- A Tesla developer application, supported charging-history entitlement, and a separate consented grant for this tool. Charging API requests can incur Tesla fees.
- A persistent private state directory and an operator-managed process supervisor for unattended operation.

**Inspect the adapter first.** `owner_database.py` currently expects an SSH alias named `dockerhost`, a Docker container named `teslamate-database-1`, and the database role `teslamate`; it discovers the database name and transient container address. Home protection currently uses **geofence ID 1**. Those are adapter assumptions, not universal TeslaMate conventions. Verify or adapt them before enabling writes. Never assume your home geofence has the same ID.

macOS process examples use illustrative `org.example.*` LaunchAgent labels. They are not installed automatically. Docker packaging is an alternative dashboard artifact, not a complete orchestrated deployment of all components.

## Local setup

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Edit `.env` locally with your own developer application credentials. Keep it untracked. Do not put secrets in command arguments, screenshots, issues, or documentation.

Follow [sign-in and authorization](docs/sign-in.md) to register the callback, obtain a separate grant, and understand the optional one-shot access probe. The probe has its own lifetime allowance; it is not the normal recurring import interface.

Runtime state defaults to `~/.local/share/ghost-ledger/`, with private-directory/file permissions. It includes authorization, cached billing history, audit state, request reservations, and backups. Do not publish or casually synchronize this directory.

### Preview first: cached history

```sh
python ledger.py preview
```

This reads cached `history.json`, queries TeslaMate, and updates the local review ledger. It does **not** contact Tesla or change charging costs. Use `--history /path/to/private/history.json` when reviewing a different saved response.

For default missing-cost imports, after inspecting the preview:

```sh
python ledger.py import --apply
```

That is a real database write operation, not a dry run. Take and test an independent database recovery backup in addition to the tool's scoped backup.

### Explicit billing fetch

```sh
python ledger.py preview --fetch
```

Manual `--fetch` requests **one latest history page**, with no immediate retry or pagination. It consumes the same durable manual/automatic history allowance. Preview still makes no charging-cost writes. Fetching and applying are separate decisions; do not add `--apply` merely to retrieve data.

### Opt-in: actual Tesla bills override estimates

For an operator-approved cutoff and an exact session, use cached evidence:

```sh
# Set these locally to the approved timezone-aware boundary and session ID.
CUTOFF='<approved ISO timestamp with timezone>'
SESSION_ID='<approved session ID>'
HISTORY="$HOME/.local/share/ghost-ledger/automatic-history.json"

python ledger.py preview --history "$HISTORY" \
  --prefer-tesla-bills --since "$CUTOFF" --session-id "$SESSION_ID"

# Only after reviewing the proposed previous amount, actual amount and difference:
python ledger.py import --apply --history "$HISTORY" \
  --prefer-tesla-bills --since "$CUTOFF" --session-id "$SESSION_ID"

# Read back the result from a fresh preview:
python ledger.py preview --history "$HISTORY" \
  --prefer-tesla-bills --since "$CUTOFF" --session-id "$SESSION_ID"
```

These commands make **no Tesla requests**. The policy forbids combining `--prefer-tesla-bills` with `--fetch`. Repeat `--session-id` only for an explicitly approved set. Do not omit the ID filter when repairing one session.

For future automatic due jobs, set **both** variables on the billing worker:

```text
GHOST_LEDGER_PREFER_TESLA_BILLS=true
GHOST_LEDGER_TESLA_BILLS_SINCE=<approved ISO timestamp with timezone>
```

Default is OFF. Invalid flags, missing/naive timestamps, and inconsistent settings fail closed. Both the bill and TeslaMate session start must meet the cutoff. Remove both variables and restart the worker to return to default missing-cost-only behavior. This does not undo previously verified writes.

See [Tesla-bill authority and recovery](docs/tesla-bill-authority.md) for the full contract.

## Automatic operation and request limits

Run the components under your chosen supervisor after completing private configuration:

| Component | Entrypoint | Responsibility |
|---|---|---|
| Passive service | `python passive_service.py` | Own-token renewal, read-only MQTT subscription, bounded DB confirmation |
| Billing worker | `python billing_worker.py` | Due-job history checks/imports, daily reconciliation, optional notifications |
| Dashboard | `python dashboard_server.py` | Private Gunicorn dashboard using private configuration |

These are not setup scripts; configure broker/database access, authorization, signing material, state paths, network binding, and supervision first. Do not start duplicate workers against the same state.

- Event checks use finite **5/30/120-minute** slots after completion. Missing final bills can be checked at a later slot; errors/ambiguity do not trigger an unbounded retry loop.
- Up to **20 due jobs** can be coalesced per round, with a minimum account-round cooldown and at most **three pages of 50 records** each.
- Manual, event, and daily work share **4 history requests per UTC day / 100 per UTC month**. Every page and failed/ambiguous attempt counts. These are request caps, **not guaranteed dollar caps** and not controls over another application's Tesla usage.
- Exhausted allowances pause work. A busy travel day can exceed the default capacity; do not delete reservation files or silently increase limits.
- The daily safety net runs at **08:00 in `GHOST_LEDGER_TIMEZONE`** (default UTC), discovering recent completed non-home missing costs over seven days, excluding the newest two hours.
- Completion/job queues are bounded to seven days / 1,000 entries. Overflow is explicit rather than silently dropping pending work.
- The live detector may miss transitions during downtime or startup mid-charge. The daily missing-cost pass helps with gaps, but is not an unlimited historical importer.

See [passive maintenance](docs/passive-maintenance.md) and [automatic billing, notifications, and receipt entry](docs/automatic-billing.md).

## Dashboard, manual receipts, and privacy

The dashboard defaults to requiring login and binds through its launcher to a specific private IPv4 address, not a public/wildcard listener. Its review pages exclude raw VINs, billing references, credentials, and private operation evidence. **Times, locations, costs, and session history are still sensitive.**

- Keep it on a trusted LAN/VPN. No public exposure is part of the supplied deployment guidance.
- Disabling login with `GHOST_LEDGER_AUTH_REQUIRED=false` is an explicit trust decision: anyone with network access can view the dashboard. HTTP does not encrypt traffic; use appropriate private-network or TLS protection.
- The standard dashboard is read-only. Optional `manual_cost_entry` in private dashboard configuration enables `/manual`, a deliberate receipt-total write form for completed non-home sessions.
- Manual receipt entry requires signing material even without login, a signed short-lived review, explicit existing-cost replacement confirmation, one-use submission protection, a backup, locked revalidation, and independent readback.
- Receipt saves use `manual_saved` provenance, not a fabricated Fleet import.
- Optional notifications can contain amounts and local session IDs; enable them only if that disclosure is acceptable. Notification failures cannot stop reconciliation.

**Never commit:** `.env`, tokens, MQTT credentials, dashboard secrets, real VINs, billing/invoice records, SQLite state, database dumps, logs, screenshots of real sessions, or private host configuration. Public examples and test fixtures must remain synthetic. A secret scanner alone cannot detect all personal information.

## Testing

```sh
python -m unittest discover -s . -v
python -m unittest discover -s charging_costs -v
python -m unittest discover -s auth -v
```

Database tests intentionally skip unless explicitly pointed at a disposable **loopback** PostgreSQL database named `synthetic`. They create/drop tables. **Never use a production DSN.**

For full coverage, including real backups and automatic due-target writes, follow the [isolated PostgreSQL recipe](docs/tesla-bill-authority.md#isolated-verification). Its container name must start with `ghost-ledger-verify-`; otherwise some integration tests skip. The verified suite currently contains **315 tests** across the three commands when all database prerequisites are enabled.

Tests use synthetic billing and credentials. Passing tests do not establish your account's Tesla entitlement, local connectivity, or production behavior; independently verify deployment status and read back every authorized cost change.

## Limitations and non-goals

- Supported billing semantics are conservative and currently USD-only. Refunds/credits, foreign exchange, and unsupported fee/tax representations require review or additional implementation.
- No payment initiation, charging control, vehicle wake/data polling, MQTT command publishing, or borrowing another application's tokens.
- No automatic historical replacement of older estimates; date cutoff and exact due-target authorization remain separate safeguards.
- No duplicate home-tariff engine, GPS warehouse, indefinite invoice archive, or Sentry USB integration.
- Default home identification and SSH/container access are deployment-specific adapter assumptions that must be reviewed.
- Previously imported evidence changing, or an uncertain commit, requires operator review rather than a blind overwrite/retry.

## Documentation

- [Deployment and recovery](docs/owner-milestone.md)
- [Separate authorization and optional access probe](docs/sign-in.md)
- [Token renewal and passive detection](docs/passive-maintenance.md)
- [Automatic billing, daily safety net, notifications, and manual receipts](docs/automatic-billing.md)
- [Date-bounded Tesla-bill authority](docs/tesla-bill-authority.md)
- [Original implementation plan](docs/plans/implementation.md) — historical design context, not the current feature list

## Licensing

No license file is currently included. Public repository visibility alone does not grant an open-source license.
