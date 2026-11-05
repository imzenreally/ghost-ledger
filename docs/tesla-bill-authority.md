# Explicit Tesla-bill authority

Default behavior is unchanged: existing TeslaMate costs (including zero) are
protected. Finalized, paid, corroborated USD Tesla bills can be made authoritative
within an **explicit owner-approved cutoff**. This does not establish any new
provider, estimate, credit/refund, invoice-download, vehicle, or historical-write
permission.

## Automatic policy (OFF by default)

Set these two environment variables on the billing worker only, after review:

```text
GHOST_LEDGER_PREFER_TESLA_BILLS=true
GHOST_LEDGER_TESLA_BILLS_SINCE=<approved timezone-aware ISO timestamp>
```

The flag accepts only literal `true` or `false`. `true` requires the cutoff;
providing a cutoff with `false`/an absent flag is rejected. Malformed timestamps,
naive dates, empty values, and unrecognized flags fail closed before worker
startup/database/request activity. Leave **both variables absent** to retain the
original missing-cost-only behavior. For rollback, remove both variables and
restart the worker through your existing deployment procedure.

Do not move the cutoff backwards to repair older costs. Use the approved
local-day boundary converted to an explicit UTC offset; the program does not
guess a timezone. Both Tesla's billed start and TeslaMate's recorded start must
be at/after this inclusive cutoff. Sessions before it remain preview-only,
including matched disagreements. No new historical discovery/backfill is added;
the daily discovery query remains missing-cost-only.

Only existing, due, completed/corroborated target jobs can trigger billing work.
An opted-in eligible target with an existing estimate may now pass the precheck;
no idle polling, extra rounds/pages, invoices, new budget, or Fleet endpoints are
added. Bills from the same fetched batch for other sessions remain visible but
cannot authorize writes beyond the exact due-ID allowlist.

## Cached-only scoped correction

These commands make **no Tesla/Fleet requests**. Database reads, a local ledger
preview publication, and (for import only) a verified prewrite database backup
are still required. Run with the existing installed interpreter/database access.
Set private shell variables `CUTOFF` and `SESSION_ID` to the approved values,
not the illustrative synthetic dates/IDs used in tests:

```sh
PYTHON="$HOME/.local/share/ghost-ledger/venv/bin/python"
HISTORY="$HOME/.local/share/ghost-ledger/automatic-history.json"
STATE="$HOME/.local/share/ghost-ledger/ledger.sqlite3"

"$PYTHON" ledger.py preview --history "$HISTORY" --state "$STATE" \
  --prefer-tesla-bills --since "$CUTOFF" --session-id "$SESSION_ID"

# Inspect ready state, previous_cost, billed amount, and delta before authorizing:
"$PYTHON" ledger.py import --apply --history "$HISTORY" --state "$STATE" \
  --prefer-tesla-bills --since "$CUTOFF" --session-id "$SESSION_ID"

# Independent cached readback; a repeated import should report imported: 0:
"$PYTHON" ledger.py preview --history "$HISTORY" --state "$STATE" \
  --prefer-tesla-bills --since "$CUTOFF" --session-id "$SESSION_ID"
```

Repeat `--session-id` for an explicitly approved set. Supplying IDs restricts
writes **and CLI output** to exactly that set; full original bill/session batches
are still checked for duplicate IDs, shared targets, and ambiguity. Neither
`--since` without `--prefer-tesla-bills` nor `--fetch` combined with this policy is
accepted. CLI policy is explicit and independent of worker environment settings.
Do not run a broad import to fix a single session.

## Financial and concurrency safeguards

- Home, unmatched/non-Tesla, invalid, ambiguous, unsupported currency/credit/refund,
  and pending/incomplete bills never become replacement authorization.
- Existing zero estimates can be replaced; actual finalized zero-dollar bills
  remain valid. Equal costs cause no write and claim no new import ownership.
- Preview snapshots include exact previous cost and a redacted identity hash
  (car, VIN, session dates, energy fields, geofence, cost). Under SERIALIZABLE
  target/car locks the complete original batch is rebuilt, snapshot rechecked,
  and `IS NOT DISTINCT FROM` cost CAS applied. Even an identity change within
  ordinary matching tolerances refuses replacement.
- The existing private import lock, backup, durable pending evidence, transaction
  readback, independent postcommit database verification, and no-blind-retry
  commit guards remain. Unresolved pending/uncertain audit events block the same
  session even under a different billing reference.
- Private operation evidence retains the original batch and preview. Final audit
  detail contains bounded JSON `old_cost`, `new_cost`, `delta`, `billing_ref`,
  `source`, and `reason`. The billing session reference is already the audit's
  external ID; VIN/tokens/invoice filenames are not added to output or public
  fixtures. Ledger HTTP projection remains unchanged and excludes evidence/audit.
- If independent verification fails, report uncertain (not imported), retain the
  pending/uncertain evidence, and stop. A matching cost alone never proves prior
  write ownership. Prior imported evidence changing still requires manual review.

## Isolated verification

Use a new disposable local container, never an existing TeslaMate database:

```sh
docker run --name ghost-ledger-verify-cost-policy \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=synthetic \
  -p 127.0.0.1:55439:5432 -d postgres:17-alpine
docker exec ghost-ledger-verify-cost-policy pg_isready -U postgres -d synthetic
# Wait for readiness if needed before starting tests.
export GHOST_LEDGER_TEST_DSN='host=127.0.0.1 port=55439 dbname=synthetic user=postgres'
export GHOST_LEDGER_TEST_POSTGRES_DSN='postgresql://postgres@127.0.0.1:55439/synthetic'
export GHOST_LEDGER_TEST_CONTAINER=ghost-ledger-verify-cost-policy
.venv/bin/python -m unittest discover -s . -q
.venv/bin/python -m unittest discover -s charging_costs -q
.venv/bin/python -m unittest discover -s auth -q
docker rm -f ghost-ledger-verify-cost-policy
```

Dependencies are the project's existing Flask, Gunicorn, psycopg and paho-mqtt
requirements. Tests use only synthetic transport/credentials/rows. New real-PG
coverage includes existing/zero/free replacement, snapshot mutation refusals,
full-batch collisions, audit/readback/idempotency, real `pg_dump`, and automatic
exact-due-target writes while a second matched non-NULL cost remains untouched.
TDD observed the replacement preview failing `protected != ready`, then the real
Postgres apply failing `protected != imported`, before implementing those paths.
