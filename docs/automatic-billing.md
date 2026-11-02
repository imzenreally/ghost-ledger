# Event-driven billing reconciliation

The automatic worker connects completed TeslaMate sessions to the existing guarded importer. **It never requests vehicle data, wakes a car, issues a command, or initiates payment.**

## Flow

1. The passive subscriber sees a fresh charging-to-noncharging transition.
2. A read-only database query corroborates one completed same-car session.
3. Home is skipped. A completed non-home session with a known away geofence **or an unknown geofence** can be checked; unknown is not assumed to be a Supercharger.
4. The first charging-history check is due five minutes after the completion receipt. If a successful response lacks a finalized matching bill, checks are due at 30 minutes and two hours.
5. A paid USD bill must pass the same identity, time, energy, duplicate/ambiguity and fee validations as a manual import.
6. Only the due target's missing cost may be written, after a validated scoped backup, durable pending evidence and transactional revalidation/readback. Other returned bills may be shown for review but are not automatically written merely because they appeared on the page.
7. Existing costs (including zero), home costs, conflicts and uncertain commits remain protected.

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

## Installed worker

The owner deployment uses native LaunchAgent `org.example.ghost-ledger-billing`, separate from the MQTT subscriber so HTTP/database operations cannot starve MQTT callbacks. The worker checks its local queue every 30 seconds. It uses the same private state directory, interpreter and guarded importer as the manual CLI.

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

The read-only LAN dashboard refreshes from the same import ledger after writes. Its network scope and owner-selected no-login mode are unchanged.

## Verification and limitations

Tests exercise the complete synthetic completion → read-only DB confirmation → durable schedule → injected Tesla response → real disposable PostgreSQL update/readback, including backup and repeat no-op behavior. Synthetic API responses are not represented as live Tesla billing data.

A real future charging completion is still needed to validate natural publication timing and actual billing delay. No fake completion is published to the production broker to manufacture this proof. The live-only detector may miss sessions during downtime; historical backfill is not enabled. Exhausted, conflicted or unconfirmed work requires review.
