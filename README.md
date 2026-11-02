# Ghost Ledger

Evidence-first Tesla charging bill reconciliation for TeslaMate. Sentry USB support is planned, not implemented.

## Development status

Early development. Nothing deployed; no live Tesla authorization or database writer is enabled.

Implemented the offline reconciliation preview (`charging_costs/reconcile.py`) and a runnable, owner-specific Fleet sign-in plus single-request billing probe (`auth/ghost_ledger.py`). No live Tesla authorization, successful billing pull, database writer, or deployment is claimed yet.

[Sign-in/test instructions](docs/sign-in.md) describe the additional callback, private credentials, laptop-to-M1 loopback tunnel, and durable one-call guard. Existing HA authorization is never borrowed. Authentication tests use synthetic responses and real local loopback HTTP; the billing probe requires explicit invocation.

Qwen use for this project is stopped at the owner's request; further work is direct implementation.

Run the tests with Python 3.12:

```sh
python3 -m unittest discover -s charging_costs -v
```

The matcher is an initial preview only. Preserve unknowns and confirmed zero costs, require exact vehicle identity, refuse ambiguous matches, and protect existing costs. A time-only preview is not sufficient evidence for production writes.

## Boundaries

- No vehicle polling, wake requests, or commands.
- No extraction of Home Assistant or TeslaMate tokens.
- No guessed tariffs presented as invoices.
- Manual/home costs protected; revisions and provenance explicit.
- TeslaMate first; Sentry USB adapter later after interface verification.
- No third telemetry warehouse or indefinite invoice archive.

See [implementation plan](docs/plans/implementation.md).
