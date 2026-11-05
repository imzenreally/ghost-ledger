# Deployment, security, and recovery

This is a **generic deployment runbook**. Addresses, paths, account names, and service labels below are examples, not an operator's infrastructure inventory. The [README](../README.md) describes the current feature set; the original implementation plan is historical context.

## Review integration assumptions first

The current database adapter expects the SSH alias `dockerhost`, Docker container `teslamate-database-1`, and PostgreSQL role `teslamate`. It discovers the database name and transient container address. Home protection uses geofence ID **1**. Verify/adapt all of those assumptions for your installation before granting write access.

Home tariff configuration stays with TeslaMate. Ghost Ledger does not set a home rate. Default imports protect existing costs, including zero. Optional [Tesla-bill authority](tesla-bill-authority.md) permits evidenced replacements only within an explicit date cutoff; home protection, due-target scope, and uncertain-commit safeguards remain.

## Separate components

- `passive_service.py`: renews the application's own grant, subscribes to state-only MQTT, and confirms completions against the database.
- `billing_worker.py`: processes confirmed due jobs, daily missing-cost discovery, guarded imports, and optional notifications.
- `dashboard_server.py`: launches the private dashboard. Standard review routes are read-only; explicitly enabling manual receipt entry adds a separate reviewed write path.

Supervise each process separately. Stop them before moving the checkout/runtime/state directory. Do not run duplicate workers against the same state. Process installation is operator-managed, not performed by the Python entrypoints.

## Private dashboard configuration

The native launcher reads `GHOST_LEDGER_CONFIG`, defaulting to `~/.local/share/ghost-ledger/dashboard-config.json`. Supply private configuration with:

- `bind_host`: a specific private/loopback IPv4 address; wildcard/public binding is rejected.
- `password_hash` and `secret_key`: operator-generated private authentication/signing material, never published examples or production values copied from another installation.
- `state`: the private ledger SQLite path.
- `manual_cost_entry`: optional, off by default.

Keep the directory private and files owner-readable only. Preserve signing configuration across restarts. Consult `dashboard_server.py` and `dashboard.py` for validation requirements; do not paste private configuration into an issue.

Login is required by default. `GHOST_LEDGER_AUTH_REQUIRED=false` explicitly disables it: anyone able to reach the listener can then view charging data. Manual receipt entry still requires signing protection. Host/Origin checks, CSP, and no-store headers do not replace network access control or encryption.

Use a trusted LAN/VPN. HTTP does not encrypt traffic; arrange private TLS where appropriate. Public exposure is not part of this runbook. Review responses exclude raw VIN, billing references, operation evidence, and credentials, but session locations/times/costs remain sensitive.

## Example macOS supervision

Use your own LaunchAgent definitions and paths. These labels illustrate the three independent processes:

```text
org.example.ghost-ledger
org.example.ghost-ledger-passive
org.example.ghost-ledger-billing
```

For an already installed example dashboard agent:

```sh
launchctl print gui/$(id -u)/org.example.ghost-ledger
launchctl kickstart -k gui/$(id -u)/org.example.ghost-ledger
```

Private dashboard health example, substituting your configured listener:

```sh
curl --noproxy '*' http://127.0.0.1:8897/healthz
```

The Dockerfile is alternative dashboard packaging, not a complete billing-service installer. Do not create a second listener or worker accidentally.

## Write boundary

1. Load cached evidence, or perform an explicitly budgeted billing fetch.
2. Query a bounded vehicle/time window through the database adapter.
3. Require supported finalized billing, unique identity, start/end timestamps, charger-energy corroboration, and valid financial totals.
4. Take a private custom-format PostgreSQL backup of charging processes, cars, and geofences. This scoped backup is **not** complete disaster recovery.
5. Persist pending operation evidence and audit before any update.
6. Rebuild matching in a serializable transaction under target/car locks. Preserve existing costs by default. Authority mode also checks the approved cutoff and unchanged exact previous-cost/identity snapshot.
7. Perform compare-and-set, transaction readback, commit, and independent postcommit verification.
8. Journal the outcome. Never infer import ownership merely because the current amount matches a bill.

Private evidence in `~/.local/share/ghost-ledger/operations/` contains billing data and must not be published. Pending/uncertain outcomes require manual investigation; do not delete journal entries or retry blindly. Previously imported evidence changing remains a review condition.

## Rollback and recovery

- **Disable future replacement:** remove both authority environment variables and restart the billing worker. Do not leave an orphaned cutoff setting. This does not revert past writes.
- **Reverse a verified cost:** inspect the audit, current target, and backup; restore only the affected cost if identity and expected imported amount still agree. Do not restore whole tables over newer sessions to undo one cost.
- **Uncertain commit:** inspect private pending evidence and independently query the exact target before deciding how to resolve the audit. A matching amount alone is insufficient ownership proof.
- **Authorization ambiguity:** use the separate sign-in recovery flow; do not borrow another service's tokens or reset reservation markers to force retries.
- **Request allowance exhausted:** pause until the applicable period or make an explicitly reviewed policy change. Do not erase request accounting.

Keep a tested independent TeslaMate backup and protect all local credentials, logs, billing caches, SQLite state, and database dumps. Use synthetic/redacted reproductions for public bug reports.
