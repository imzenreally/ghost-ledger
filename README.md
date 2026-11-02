# Ghost Ledger

Evidence-first Tesla charging bill reconciliation for TeslaMate. Sentry USB support is planned, not implemented.

## Development status

Early development. Nothing deployed; no live Tesla authorization or database writer is enabled.

Implemented first slice: a pure, offline reconciliation preview (`charging_costs/reconcile.py`) using explicitly synthetic billing records and charging sessions. Its 35 unit tests pass; no live reader or writer exists yet.

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
