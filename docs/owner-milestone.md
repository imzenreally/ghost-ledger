# Owner milestone: guarded importer + private review dashboard

## Scope

Home remains **$0.10/kWh**, flat rate, managed by TeslaMate. Ghost Ledger imports evidenced Tesla-billed Supercharger costs only. It never changes home rates, wakes/polls a vehicle, changes the HA grant, or overwrites an existing cost (including zero). Existing-cost disagreements and weak matches remain review conflicts.

The dashboard is deliberately read-only. Importing is an explicit owner CLI operation, not an HTTP action. No scheduler, automatic fetching, or unattended token refresh is enabled in this milestone.

## Private dashboard

- Owner-selected access: **LAN/VPN only with login**.
- Installed origin: `http://10.0.0.10:8897/` on the M1.
- Login is password-only. The generated password is stored privately on that machine in `~/.local/share/ghost-ledger/dashboard-access.txt`; it is not in Git, a URL, or logs.
- Signing key/password hash: mode-0600 `~/.local/share/ghost-ledger/dashboard-config.json` in the mode-0700 private state directory. Preserve this file across restarts. Never paste it into an issue or chat.
- No public DNS, reverse proxy, Cloudflare tunnel, or WAN forwarding has been created.
- This is HTTP on the trusted LAN. A password does not encrypt LAN traffic. Use the VPN for offsite access; private HTTPS can be a separate enhancement. Do not expose this listener publicly.
- Anonymous session API requests return 401; login/logout are CSRF protected. Strict Host/Origin checks, no-store, CSP, HttpOnly/SameSite cookies and bounded login throttling apply. Dashboard responses exclude raw VIN, billing IDs, invoice references, private operation evidence, DB credentials and tokens.

### Installed process

Native Gunicorn, one worker/four threads, under LaunchAgent `org.example.ghost-ledger`. It restarts on failure and starts with the user's login. This is not a billing scheduler.

Paths:
- checkout: `~/gitea/github-work/ghost-ledger`
- stable runtime: `~/.local/share/ghost-ledger/venv`
- ledger: `~/.local/share/ghost-ledger/ledger.sqlite3`
- process configuration: `~/Library/LaunchAgents/org.example.ghost-ledger.plist`
- private process logs: `~/.local/share/ghost-ledger/dashboard.{stdout,stderr}.log`

Health: `curl --noproxy '*' http://10.0.0.10:8897/healthz`

Restart after code changes:
```sh
launchctl kickstart -k gui/$(id -u)/org.example.ghost-ledger
```
Stop without deleting data:
```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/org.example.ghost-ledger.plist
```
The Dockerfile is an alternative tested packaging artifact, **not** the installed deployment. Do not launch a duplicate listener on port 8897.

## Write boundary and recovery

1. Load saved private history (manual fetch is a separate explicit action).
2. Query a bounded same-VIN/time window from TeslaMate over an owner SSH tunnel.
3. Require final paid USD billing/invoice references; verify fee totals and unique identity/start/end/charger-energy corroboration. Unsupported refunds/credits/tax-component interpretations fail closed.
4. Before applying, take a private custom-format PostgreSQL backup of charging processes, cars and geofences. This is a scoped backup, not a complete TeslaMate disaster-recovery backup.
5. Persist operation evidence and a pending SQLite audit event before any update.
6. Rebuild the preview in a serializable transaction with fresh target locks, protect home and any non-NULL cost, and use `cost IS NULL` compare-and-set.
7. Read back the exact amount, commit, journal the result, then independently re-query sessions for the dashboard.
8. Never retry an uncertain commit automatically. A pending/uncertain journal blocks further writes for that bill until manually resolved. Same-amount pre-existing costs are not claimed as our imports. Changed bills/costs after an earlier import become review conflicts.

Private operation evidence is stored under `~/.local/share/ghost-ledger/operations/` for recovery. It contains personal billing data and must not be published. The UI never serves it.

Rollback is an explicit maintenance operation: inspect the successful audit event, current target and original backup, then restore only the affected cost if it still equals our imported amount. Do not restore whole charging/car/geofence tables over current production data merely to reverse two costs.

## Current limitation

The initial history sample contains only five records; it is not a complete historical import. Additional pages, older backfill, automatic catch-up, token refresh and changes to existing costs are not silently enabled. The original one-call probe allowance remains consumed and is never reset by the importer.
