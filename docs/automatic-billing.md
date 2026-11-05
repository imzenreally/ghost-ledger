# Event-driven billing reconciliation

The automatic worker connects completed TeslaMate sessions to the existing guarded importer. **It never requests vehicle data, wakes a car, issues a command, or initiates payment.**

## Flow

1. The passive subscriber sees a fresh charging-to-noncharging transition.
2. A read-only database query corroborates one completed same-car session.
3. Home is skipped. A completed non-home session with a known away geofence **or an unknown geofence** can be checked; unknown is not assumed to be a Supercharger.
4. The first charging-history check is due five minutes after the completion receipt. If a successful response lacks a finalized matching bill, checks are due at 30 minutes and two hours.
5. A paid USD bill must pass the same identity, time, energy, duplicate/ambiguity and fee validations as a manual import.
6. Only exact due targets may be written, after a validated scoped backup, durable pending evidence and transactional revalidation/readback. Other returned bills may be shown for review but are not automatically written merely because they appeared on the page.
7. Existing costs (including zero) are protected by default. Explicit [Tesla-bill authority](tesla-bill-authority.md) can replace an existing estimate within an approved cutoff, preserving old/new amounts and revalidating the previous cost and identity. Home, ambiguous matches, uncertain commits, and changed previously imported evidence remain protected.

There is no idle periodic Tesla history poll. With no eligible pending job, the worker makes no history requests. Local queue checks and authentication renewal are not vehicle polling.

## Bounds

- Finite 5/30/120-minute slots, not a guarantee of Tesla bill availability. A late restart uses the latest elapsed slot instead of bursting all missed checks.
- Due sessions are grouped, at most 20 per round; minimum one minute between account rounds.
- At most three history pages per round, 50 records/page; no invoice PDF downloads. Pagination uses the documented endpoint and community-observed `pageSize`/`pageNo` convention; no arbitrary URLs or redirects are accepted.
- **Combined manual/automatic allowance remains four history requests per UTC day and 100 per UTC month.** Each page and failed request consumes one reservation before transport. These are request caps, not guaranteed dollar amounts, and cannot limit Home Assistant's independent usage.
- If the allowance is exhausted, work pauses rather than bypassing the cap. A pause can defer work beyond the nominal slot time; a busy trip may require an explicitly approved cap adjustment.
- Queue limited to 1,000 session jobs and seven days, with storage reclamation. Full capacity is an explicit error, not silent live-job eviction.
- No blind retry after a possible database commit. Crashes with a pending round leave jobs needing review. Authentication/unsupported data/conflicts stop for review; missing bills exhaust the finite schedule.
- Transport/rate-limit failures stop the current round and may pause until the next UTC day, still subject to the same durable limits. No immediate retry loop.

## Worker supervision example

A native macOS installation can use LaunchAgent `org.example.ghost-ledger-billing`, separate from the MQTT subscriber so HTTP/database operations cannot starve MQTT callbacks. The worker checks its local queue every 30 seconds. It uses the same private state directory, interpreter and guarded importer as the manual CLI. The label and commands below are illustrative; supply your own process configuration.

```sh
launchctl print gui/$(id -u)/org.example.ghost-ledger-billing
launchctl kickstart -k gui/$(id -u)/org.example.ghost-ledger-billing
```

Stop:
```sh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/org.example.ghost-ledger-billing.plist
```

Private operational files:
- `billing_scheduler.sqlite3`: bounded jobs/slot state.
- `billing-status.json`: sanitized current worker status, counts and next due time.
- `automatic-history.json`: latest bounded private response, not served by the dashboard.
- `manual-budget.sqlite3`: shared request reservations.

The private dashboard refreshes from the same import ledger after writes. Login is required by default; deliberately disabling it does not alter network-scope, signing, or write-path safeguards. See the deployment runbook.

## Daily safety net and notifications

The finite 5/30/120-minute event checks are supplemented by a daily database reconciliation. It runs at **08:00 in `GHOST_LEDGER_TIMEZONE`** (default `UTC`); a restart after that time catches up once for the local date. Time-zone-aware local dates handle daylight-saving changes. The owner selects their actual time zone in private deployment configuration.

The daily pass discovers completed non-home sessions with missing costs from the preceding seven days, excluding the most recent two hours so it does not preempt the initial retry window. This catches late Tesla bills and sessions missed during subscriber downtime. Discovery is read-only and bounded; overflow stops for review rather than silently dropping rows. If there are no eligible gaps, there is **no Tesla request**. Actual DB start/end timestamps—not invented MQTT receipts—identify these jobs. Waiting sessions can be reconsidered on later days; conflicts, uncertain commits and other review states are not blindly retried.

Daily and event work share the same **four-request daily / 100-request monthly** history allowance and existing guarded importer. Daily work takes precedence when due; a busy day can exhaust the allowance and delay reconciliation. No cap is silently increased. The daily state file is private `daily_reconcile.sqlite3`.

Optional Pushover notifications are enabled by pointing `GHOST_LEDGER_PUSHOVER_KEYS_FILE` at a private, owner-readable regular file with these assignment names (supply your own values):

```text
PUSHOVER_API_TOKEN=<your-application-token>
PUSHOVER_USER_KEY=<your-user-key>
```

Require file mode `0600`, no symlinks/hardlinks. Never commit this file. The default without a credential path is disabled. Notifications use the official HTTPS message API, normal priority, bounded transport and durable deduplication in private `notifications.sqlite3`.

- New verified automatic imports and manual cost saves notify with the amount and local session ID.
- New conflicts, blocked authorization, exhausted request allowances and daily failures produce attention alerts, coalesced to at most once per issue/session per UTC day.
- Clean daily runs, ordinary waiting/retry slots, historical imports and old preview-only disagreements stay quiet.
- Notification failures cannot block charging-cost reconciliation. Ambiguous sends are not blindly repeated; API acceptance is not a device delivery/read receipt.

## Manual receipt totals (Blink and other non-Tesla charging)

The dashboard's `/manual` page lets the owner enter a receipt total for completed non-home sessions — Blink/work Level 2, grocery-store chargers, anything TeslaMate can't bill. Enabled per deployment via the private dashboard configuration; the public default stays off, and enabling it requires a real signing key even without login.

- The entry page lists completed non-home sessions from the last 30 days (read-only, ≤100 rows, unknown geofences included; home is always excluded).
- The owner selects a session, picks a provider (Blink/Other) and enters the USD total (0–10000, ≤2 decimals).
- Replacing an existing cost requires an explicit replacement checkbox; existing values including zero are otherwise protected.
- A signed five-minute review step re-reads the session, confirms the exact snapshot, and issues a one-use nonce.
- Saving takes a scoped backup first, then revalidates the full session identity under serializable locking and compare-and-swap before a single guarded write; an independent post-commit connection confirms the stored amount.
- Failures and ambiguity become durable audit states (`pending`, `uncertain`, `protected`) and never auto-retry; a session whose identity changed refuses to save.
- Manual entries never fabricate Tesla/Fleet provenance: audit outcomes use `manual_saved`, not `imported`, and do not create Fleet preview rows.

This is a deliberate write path: read-only dashboard routes are unchanged, and only this explicitly reviewed form writes a cost.

## Verification and limitations

Tests exercise the complete synthetic completion → read-only DB confirmation → durable schedule → injected Tesla response → real disposable PostgreSQL update/readback, including backup and repeat no-op behavior. Synthetic API responses are not represented as live Tesla billing data.

Each installation should verify natural completion timing, actual bill availability, and independent database readback without publishing personal trip data. Do not publish fake completions to a production broker to manufacture proof. The live-only detector may miss sessions during downtime; the bounded daily missing-cost safety net does not provide unlimited historical backfill or historical estimate replacement. Exhausted, conflicted or unconfirmed work requires review.
