# Sign-in and single-request test — the owner's setup

This is the runnable first milestone, **not the automatic TeslaMate updater yet**.
It uses the existing Tesla developer app, but obtains a separate Ghost Ledger grant.
No HA token file, HA integration configuration, vehicle endpoint, or billing limit is touched.

## 1. Register the extra redirect

In the existing Tesla developer application, add this **additional** Allowed Redirect URI:

```
http://localhost:8769/callback
```

Keep `https://my.home-assistant.io/redirect/oauth` and existing origins/permissions intact.
Tesla's portal acceptance of this localhost URI must be checked; if rejected, stop and report that, do not guess another URI.

## 2. Prepare local credentials, never in chat or git

Copy `.env.example` to `.env` in the Ghost Ledger checkout, set mode 0600, and edit locally with the existing app's client ID and client secret. Do not use HA's access/refresh tokens. Do not commit this file or put the secret directly in command arguments.

```
cp .env.example .env
chmod 600 .env
```

## 3. Browser on laptop, process on M1

The callback listener runs on `127.0.0.1:8769`, not the LAN. If browsing Tesla on another laptop, open an SSH loopback tunnel from that laptop to the M1 and keep it open:

```
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8769:127.0.0.1:8769 user@10.0.0.10
```

Do not run this command on the M1 itself. If port 8769 is already occupied or SSH access fails, stop and resolve that before starting sign-in. A browser on the M1 does not need the tunnel.

From the M1 checkout, using Python 3.12 or newer:

```
python3 auth/ghost_ledger.py --env-file .env login
```

Open the displayed Tesla authorize link in the browser on the M1 or tunneled laptop. Review the Tesla consent screen; do not remove permissions needed by HA. The listener expires after five minutes, stops after the callback, and exchanges the code once. No code/token is printed in command output. Invalid requests are not logged. Tokens are atomically stored mode 0600 under `~/.local/share/ghost-ledger/` (directory mode 0700).

No token refresh daemon exists in this milestone. Expired authorization stops the probe; it does not try HA credentials.

## 4. Exactly one billable probe

After successful sign-in, on the M1:

```
python3 auth/ghost_ledger.py history-once
```

This sends one GET for at most a small history page (`pageSize=5`). No pagination, invoice PDF request, retries, wake, vehicle data or commands. The command prints only HTTP status, returned record count, and `pagination_followed: false`; sensitive raw history stays in the private state directory as `history.json`.

The allowance is reserved durably before HTTP and is **one request total** across restarts and concurrent calls. Even a failure/timeout consumes it. Do not delete/reset the budget file to retry without discussing the result. The ledger is a request cap, not Tesla's account billing meter. HA's independent usage still consumes the shared developer allowance. Tesla's dashboard is authoritative; its account-level limit stays unchanged.

Authentication endpoints are not billed per Tesla's third-party authentication documentation. Charging history/error requests can be billable. Exact endpoint rate/account credit application is not inferred by this tool.

## Verified offline coverage

Actual loopback HTTP callback -> injected synthetic token endpoint -> private token store. Separate tests enforce denied endpoints, durable one-call allowance including failures, concurrent reservations, expiry, sanitized errors and malformed callbacks. These tests do not prove live Tesla entitlement or portal redirect acceptance.

## Next gate

Inspect the returned real history and validate financial fields against one invoice before implementing an approved session writer. No automatic TeslaMate cost updates or deployment are present yet.
