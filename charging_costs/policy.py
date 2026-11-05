"""Explicit, date-bounded Tesla-bill authority; absent configuration stays OFF."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re

from .importer import timestamp


@dataclass(frozen=True, init=False)
class TeslaBillPolicy:
    since: datetime

    def __init__(self, since: datetime | str):
        value = since
        if isinstance(value, str) and not re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
            raise ValueError('since requires an explicit timezone-aware ISO timestamp')
        object.__setattr__(self, 'since', timestamp(value))

    def permits(self, session, bill_start=None):
        try:
            return (timestamp(session['start_date'], db=True) >= self.since
                    and (bill_start is None or timestamp(bill_start) >= self.since))
        except (ValueError, TypeError, KeyError, OverflowError):
            return False


def from_environment(environ):
    enabled = environ.get('GHOST_LEDGER_PREFER_TESLA_BILLS', 'false')
    since = environ.get('GHOST_LEDGER_TESLA_BILLS_SINCE')
    if enabled not in ('true', 'false'):
        raise ValueError('Tesla bill policy must be true or false')
    if enabled == 'false':
        if since is not None:
            raise ValueError('since requires enabled Tesla bill policy')
        return None
    if since is None:
        raise ValueError('enabled Tesla bill policy requires since')
    return TeslaBillPolicy(since)


def session_snapshot(session):
    # Hash identity rather than carrying VIN into the public-facing ledger.
    fields = ('id', 'car_id', 'vin', 'start_date', 'end_date', 'charge_energy_used',
              'charge_energy_added', 'geofence_id', 'cost')
    values = {}
    for key in fields:
        value = session.get(key)
        if key in ('start_date', 'end_date') and value is not None:
            value = timestamp(value, db=True).isoformat()
        values[key] = None if value is None else str(value)
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
