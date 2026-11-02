# Own-token renewal + passive completion detection

This milestone enables **only** automatic Ghost Ledger token renewal and passive TeslaMate completion detection. It does not fetch charging bills, paginate history, schedule billing retries, or import costs automatically. The earlier manual CLI remains explicit.

## Installed service

M1 LaunchAgent: `org.example.ghost-ledger-passive`.

```sh
launchctl print gui/$(id -u)/org.example.ghost-ledger-passive
launchctl kickstart -k gui/$(id -u)/org.example.ghost-ledger-passive
```

Stop:
```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/org.example.ghost-ledger-passive.plist
```

Uses stable `~/.local/share/ghost-ledger/venv/bin/python` and repository `passive_service.py`. It starts at user login and restarts on failure. Stop it before deleting or moving the checkout/state/runtime. No Hermes cron job is required.

Private state under `~/.local/share/ghost-ledger/`:
- `mqtt-config.json`: existing broker settings captured privately; no Tesla/HA tokens.
- `tokens.json`: Ghost Ledger's own grant, never HA's.
- `refresh-state.json` and `token-rotation.lock`: durable refresh reservation and process lock.
- `charging_candidates.sqlite3`: bounded seven-day/1,000-event candidate queue, no GPS/VIN/raw telemetry.
- `passive-status.json`: sanitized renewal, MQTT, coverage and confirmation status.
- `passive.stdout.log`, `passive.stderr.log`: private process diagnostics, no credential output.

The status JSON is private local operational state, not a new public dashboard endpoint.

## Token renewal

Check local expiry every minute. When within five minutes of expiration, make one refresh POST to the official Tesla authentication endpoint and atomically save BOTH new tokens. Authentication endpoints are documented as not billed. No billing/vehicle endpoint is used.

A shared process lock serializes renewal and interactive login's exchange/save. Refresh tokens rotate; reserve before sending. If a request or save has an ambiguous outcome, do NOT blindly retry the same token. The service reports a blocked reason and requires operator recovery/new separate sign-in. A new token fingerprint resolves a reservation left after successful persistence. Never delete the reservation just to force another attempt.

Interactive login retains its registered loopback callback and separate grant. The renewal request uses client ID and refresh token, not HA credentials or the app client secret.

Official source: https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens

## Passive detection

Subscribe **only** to `teslamate[/namespace]/cars/+/state`, QoS1. Never publish. A live `charging` observation followed by a recognized live noncharging state creates a durable candidate. Retained startup packets never arm or finish; reconnect/restart drops arming rather than inventing a transition.

TeslaMate v4.2 publishes geofence/state topics independently and without order guarantees. Cached `Home` is therefore not authoritative. A candidate is corroborated with a bounded read-only TeslaMate DB query: same car, interval covering the first charging receipt, completed end near the state transition, and exactly one result.

- Home geofence ID1 => `skipped_home`.
- Other positive geofence => `confirmed_away`.
- No known geofence => `unknown_location`, NOT assumed home/away/free.
- Multiple/no matches => remains unconfirmed.

Away is not proof of Supercharging. Neither classification fetches a bill. An end-of-charging transition can also mean interrupted charging; the DB must show a completed charging_process before confirmation.

The live-only approach can miss sessions during service/broker downtime or if startup occurs mid-charge and no fresh charging state arrives. This limitation is reported in status; no historical completion backfill is claimed.

### Native macOS networking

Direct LAN broker connection from the LaunchAgent returned `OSError errno65` while the interactive subscription worked. Installed service uses a private, short-lived SSH loopback forward through existing `dockerhost` to the broker. It exposes no broker/DB port on LAN, includes no password in argv, and cleans up its SSH child on graceful exit. ServerAlive checks terminate broken tunnels; reconnect is local broker reconnection, not Tesla API retrying.

## Verification

Synthetic tests cover retained replay, reconnects, duplicates, rotation/crash recovery, process contention, queue retention, home/away/unknown and bounded read-only SQL. Real own-token rotation and persisted-token validity were checked once. Actual background MQTT SUBACK/retained receipt and service restart must be checked via `passive-status.json`. Read-only confirmation was exercised against existing home/away DB sessions using synthetic local receipt events; those were not published or presented as new live charges.

No real post-deployment charging completion has been observed yet. That will be the first natural end-to-end detection check; do not wake/charge the car solely to manufacture one.
