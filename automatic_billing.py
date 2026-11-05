"""Bounded, owner-only automatic billing callback. Importing performs no work.

Jobs are hints, never write authorization. Fresh database evidence and the guarded
importer's full-batch collision checks remain authoritative. No token refresh,
vehicle endpoints, invoice requests, retries, or exception text leave this module.
"""
from pathlib import Path
from decimal import Decimal
import json
import math
import os
import time
import uuid

from auth import runtime
from charging_costs.importer import timestamp, build_preview, normalize_bill, corroborates
from charging_costs.postgres import COLUMNS, SELECT, PgRepository
from charging_costs.policy import from_environment
from ledger import import_lock
from ledger_store import LedgerStore
from manual_reader import ManualReaderError, private_path, reserve_manual, _decode, _records
from owner_database import OwnerDatabase
from workflow import run_import


HISTORY_URL = 'https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/dx/charging/history?pageSize=50'


TARGET_COLUMNS = COLUMNS
TARGET_SELECT = SELECT


def _targets(database, ids):
    """One fresh read-only, parameterized exact-ID query (at most twenty)."""
    conn = database.connect()
    try:
        conn.autocommit = False
        cursor = conn.cursor()
        try:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute(TARGET_SELECT + ' WHERE cp.id IN (' +
                           ','.join('%s' for _ in ids) + ')', tuple(ids))
            rows = [dict(r) if isinstance(r, dict) else dict(zip(TARGET_COLUMNS, r))
                    for r in cursor.fetchall()]
            conn.rollback()
        finally:
            cursor.close()
    finally:
        conn.close()
    if len(rows) > len(ids) or len({r['id'] for r in rows}) != len(rows):
        raise ValueError('ambiguous target identity')
    return {r['id']: r for r in rows}


def _eligible(row, job):
    try:
        started, observed = job['started_at'], job['observed_at']
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (started, observed)):
            return False
        start = timestamp(row['start_date'], db=True).timestamp()
        end = timestamp(row['end_date'], db=True).timestamp()
        return (row['car_id'] == job['car_id']
                and type(job['car_id']) is int and job['car_id'] > 0
                and isinstance(row['vin'], str) and bool(row['vin'].strip())
                and start <= end and started <= observed
                and start <= started + 5 and end >= started - 5
                and observed - 300 <= end <= observed + 60)
    except (ValueError, KeyError, TypeError, OverflowError):
        return False


def _prior_import(row, job, previous_rows, latest_events):
    owned = [e for e in latest_events.values() if e['session_id'] == row['id']
             and e['outcome'] == 'imported']
    if not owned:
        return None
    if not _eligible(row, job) or str(row.get('geofence_id')) == '1':
        return 'conflict'
    for event in owned:
        matches = [r for r in previous_rows if r.get('session_id') == row['id']
                   and r['external_id'] == event['external_id']]
        if len(matches) != 1:
            return 'manual_review'
        previous = matches[0]
        try:
            energy = Decimal(previous['evidence']['billed_kwh'])
            actual = Decimal(str(row['charge_energy_used']))
            if (not energy.is_finite() or not actual.is_finite() or energy < 0 or actual < 0
                    or abs(actual - energy) > max(Decimal('0.25'), energy * Decimal('0.02'))
                    or previous['status'] != 'imported' or previous['amount'] != event['amount']
                    or Decimal(str(row['cost'])) != Decimal(event['amount'])
                    or abs((timestamp(row['start_date'], db=True) - timestamp(previous['started_at'])).total_seconds()) > 300
                    or abs((timestamp(row['end_date'], db=True) - timestamp(previous['ended_at'])).total_seconds()) > 60):
                return 'conflict'
        except (ValueError, TypeError, KeyError, ArithmeticError):
            return 'conflict'
    return 'imported'


class TargetRepository:
    """Keep ALL candidates for collisions, but permit only due-target writes."""
    def __init__(self, database, targets, *, policy=None):
        self.database = database
        self.targets = targets
        self.repository = PgRepository(database.connect, policy=policy)

    def sessions_for(self, history):
        return self.repository.sessions_for(history)

    def apply(self, row):
        sid = row.get('session_id')
        original = self.targets.get(sid)
        if original is None:
            return {'status': 'protected', 'reason': 'Not an eligible due target'}
        current = _targets(self.database, [sid]).get(sid)
        fields = ('car_id', 'vin', 'start_date', 'end_date', 'charge_energy_used', 'cost', 'geofence_id')
        if current is None or any(current.get(k) != original.get(k) for k in fields):
            return {'status': 'conflict', 'reason': 'Target identity changed; no write'}
        return self.repository.apply(row)


def _outcomes(store, ids, history, targets):
    external_ids = {str(r.get('sessionId')) for r in history}
    rows = [r for r in store.snapshot()['rows'] if r['external_id'] in external_ids]
    ambiguous = set()
    ambiguous_ids = {r['external_id'] for r in rows if r['status'] == 'ambiguous'}
    for raw in history:
        if str(raw.get('sessionId')) not in ambiguous_ids:
            continue
        try:
            bill = normalize_bill(raw)
            ambiguous.update(sid for sid in ids if corroborates(bill, targets[sid]))
        except (ValueError, TypeError, KeyError, ArithmeticError):
            continue
    outcomes = {}
    for sid in ids:
        states = {r['status'] for r in rows if r.get('session_id') == sid}
        if sid in ambiguous or 'conflict' in states or 'ambiguous' in states:
            outcomes[sid] = 'conflict'
        elif 'uncertain' in states:
            outcomes[sid] = 'manual_review'
        elif 'imported' in states:
            outcomes[sid] = 'imported'
        elif 'protected' in states:
            outcomes[sid] = 'protected'
        elif 'ready' in states:
            outcomes[sid] = 'ready'
        else:
            outcomes[sid] = 'waiting'
    return outcomes


class _FetchStopped(Exception):
    def __init__(self, outcome):
        self.outcome = outcome


def _same_content(left, right):
    """JSON structural equality must not treat true as the number one."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_content(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_content(a, b) for a, b in zip(left, right))
    return left == right


def _history_pages(root, pending, repo, transport, clock):
    history = []
    seen = {}
    for page in range(3):
        try:
            token = runtime.load_tokens(root, clock=clock)['access_token']
        except ValueError:
            raise _FetchStopped('auth_blocked') from None
        reserve_manual(root, clock=clock)
        url = HISTORY_URL + (('&pageNo=' + str(page)) if page else '')
        try:
            status, body = transport(url, None, {'Authorization': 'Bearer ' + token}, 20)
        except Exception:
            raise _FetchStopped('budget_paused') from None
        if type(status) is not int or status != 200:
            raise _FetchStopped('auth_blocked' if status in (401, 403) else 'budget_paused')
        try:
            raw = _decode(body)
            records = _records(raw)
            if len(records) > 50:
                raise ValueError('page limit exceeded')
            for record in records:
                sid = record.get('sessionId')
                if type(sid) is int and sid in seen:
                    if not _same_content(record, seen[sid]):
                        raise _FetchStopped('conflict')
                    continue
                if type(sid) is int:
                    seen[sid] = record
                history.append(record)
            payload = raw.get('response', raw) if isinstance(raw, dict) else raw
            total = payload.get('totalResults') if isinstance(payload, dict) else None
            if total is not None and (type(total) is not int or total < 0):
                raise ValueError('invalid pagination')
        except ValueError:
            raise _FetchStopped('manual_review') from None
        rows = build_preview(history, repo.sessions_for(history))
        matched = {r['session_id'] for r in rows if r['session_id'] is not None}
        if set(pending) <= matched or len(records) < 50 or (total is not None and len(history) >= total):
            break
    return history


def check_jobs(root, jobs, *, transport=runtime.tesla_transport,
               database_factory=OwnerDatabase, clock=time.time):
    if not jobs:
        return {}
    results = {j['session_id']: 'manual_review' for j in jobs}
    ids = list(results)
    if (len(jobs) > 20 or len(ids) != len(jobs)
            or any(type(i) is not int or not 0 < i <= 9223372036854775807 for i in ids)):
        return results
    pending = []
    try:
        policy = from_environment(os.environ)
        root = Path(root)
        state = private_path(root / 'ledger.sqlite3')
        with import_lock(state):
            with database_factory() as database:
                targets = _targets(database, ids)
                store = LedgerStore(state)
                latest_events = {}
                for event in store.events():
                    latest_events[(event['session_id'], event['external_id'])] = event
                blocked = {e['session_id'] for e in latest_events.values()
                           if e['outcome'] in ('pending', 'uncertain')}
                previous_rows = store.snapshot()['rows']
                pending = []
                for job in jobs:
                    row = targets.get(job['session_id'])
                    if job['session_id'] in blocked:
                        continue
                    if row is not None:
                        prior = _prior_import(row, job, previous_rows, latest_events)
                        if prior is not None:
                            results[job['session_id']] = prior
                            continue
                    if row is not None and (str(row.get('geofence_id')) == '1'
                            or (policy is None and row.get('cost') is not None)
                            or (policy is not None and not policy.permits(row))):
                        results[job['session_id']] = 'protected'
                    elif row is not None and _eligible(row, job):
                        pending.append(job['session_id'])
                if not pending:
                    return results
                repo = TargetRepository(database, {sid: targets[sid] for sid in pending}, policy=policy)
                history = _history_pages(root, pending, repo, transport, clock)
                destination = private_path(root / 'automatic-history.json')
                encoded = json.dumps(history, default=str, allow_nan=False, separators=(',', ':')).encode()
                runtime._atomic_bytes(root, destination.name, encoded)
                run_import(history, repo, store, policy=policy)
                outcomes = _outcomes(store, pending, history, targets)
                ready = {sid for sid, outcome in outcomes.items() if outcome == 'ready'}
                if ready:
                    backups = runtime._private_dir(root / 'backups')
                    backup = backups / (uuid.uuid4().hex + '.dump')
                    database.backup(backup)
                    backup = private_path(backup)
                    with backup.open('rb') as source:
                        if source.read(5) != b'PGDMP' or backup.stat().st_size <= 5:
                            raise ValueError('backup validation failed')
                    fresh = _targets(database, sorted(ready))
                    stopped = {}
                    identity = ('car_id', 'vin', 'start_date', 'end_date', 'charge_energy_used', 'cost', 'geofence_id')
                    for sid in ready:
                        current = fresh.get(sid)
                        if current is not None and (str(current.get('geofence_id')) == '1'
                                or (policy is None and current.get('cost') is not None)
                                or (policy is not None and not policy.permits(current))):
                            stopped[sid] = 'protected'
                        elif current is None or any(current.get(k) != targets[sid].get(k) for k in identity):
                            stopped[sid] = 'conflict'
                    allowed = ready - stopped.keys()
                    if allowed:
                        report = run_import(history, repo, store, apply=True, allowed_session_ids=allowed, policy=policy)
                        outcomes = _outcomes(store, pending, history, targets)
                        if report['uncertain']:
                            outcomes = {sid: ('manual_review' if value not in ('imported', 'protected') else value)
                                        for sid, value in outcomes.items()}
                    outcomes.update(stopped)
                results.update({sid: ('manual_review' if value == 'ready' else value)
                                for sid, value in outcomes.items()})
    except _FetchStopped as error:
        results.update(dict.fromkeys(pending, error.outcome))
        return results
    except runtime.BudgetExceeded:
        results.update(dict.fromkeys(pending, 'budget_paused'))
        return results
    except ManualReaderError:
        return dict.fromkeys(ids, 'budget_paused')
    except Exception:
        return results
    return results
