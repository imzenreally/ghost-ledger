# Separate Fleet authorization and optional single-request probe

Ghost Ledger obtains its **own** Fleet grant. It must not extract or reuse Home Assistant/TeslaMate access or refresh tokens. Later components provide token renewal and automatic reconciliation; this document covers initial consent and the optional tightly bounded access probe, not the whole deployment.

## 1. Register the callback

Configure your Tesla developer application with the additional Allowed Redirect URI:

```text
http://localhost:8769/callback
```

Preserve redirects, origins, and permissions used by other applications. Verify portal acceptance and required charging-history entitlement rather than guessing another callback. Review requested consent scopes: a scope permitting charging-history access may also cover charging commands, although Ghost Ledger itself does not issue vehicle commands.

## 2. Prepare private application credentials

```sh
cp .env.example .env
chmod 600 .env
```

Edit locally with your application's client ID and client secret. Do not commit this file, paste it in a support request, or put secrets in command arguments. These are developer application credentials, not another service's access/refresh tokens.

## 3. Sign in on the correct host

The callback listener binds `127.0.0.1:8769`, not the LAN. `localhost` means the machine running the browser. If the process runs on another host, open a private loopback SSH tunnel **from the browser's machine** to the process host:

```sh
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:8769:127.0.0.1:8769 user@example-host
```

Replace `user@example-host` with your own SSH destination. A browser on the process host needs no tunnel. Resolve occupied ports or forwarding failures before starting sign-in.

From the checkout with Python 3.12+ and dependencies installed:

```sh
python auth/ghost_ledger.py --env-file .env login
```

Open the displayed authorization link and review Tesla's consent screen. The listener expires after five minutes and exchanges the callback once. Invalid callback details, codes, and tokens are not logged. Tokens are stored atomically with mode `0600` under the private state directory, default `~/.local/share/ghost-ledger/` with mode `0700`.

The [passive service](passive-maintenance.md) can renew this grant. Ambiguous token exchange/rotation requires operator recovery, not borrowing another application's tokens or retrying a potentially consumed refresh token blindly.

## 4. Optional one-shot access probe

```sh
python auth/ghost_ledger.py history-once
```

This is an initial diagnostic, **not a command to run after every charge**. It requests at most one small history page (`pageSize=5`), with no pagination, invoice PDF download, immediate retry, or vehicle call. Output is limited to HTTP status, count, and pagination status; sensitive response data remains in private `history.json`.

The probe's separate allowance permits **one request total** across restarts and concurrent calls, reserved before transport. Failures and timeouts consume it. Never delete/reset the allowance to manufacture another attempt. Normal manual imports/fetches and automatic billing use their separately bounded shared budget; see the [README](../README.md#automatic-operation-and-request-limits).

Tesla authentication endpoints are documented separately from metered Fleet requests. Charging-history/error requests can be billable. Neither a local request cap nor another application's usage helpers establish an exact invoice amount or control all developer-account spending.

## Verification boundaries

Offline tests exercise real loopback callbacks with synthetic token/history responses, private persistence, concurrent reservations, expiry, endpoint restrictions, and sanitized failures. They do not establish your live Tesla entitlement, portal acceptance, consent, or connectivity.

Before enabling writes, inspect supported billing semantics, verify database/home-geofence adapter assumptions, review a cached preview, and configure backups. See [deployment and recovery](owner-milestone.md), [automatic reconciliation](automatic-billing.md), and [optional existing-cost replacement](tesla-bill-authority.md).
