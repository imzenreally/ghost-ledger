"""Manual saved-bill import orchestration. Never fetches Tesla or schedules work."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import uuid

from charging_costs.importer import build_preview, history_records, timestamp


def _review_rows(rows, sessions, history, events):
    by_id = {s['id']: s for s in sessions}
    originals = {str(b['sessionId']): b for b in history_records(history)
                 if isinstance(b, dict) and type(b.get('sessionId')) is int}
    latest = {e['external_id']: e for e in events}
    blocked_sessions = {e['session_id'] for e in latest.values() if e['outcome'] in ('pending', 'uncertain')}
    for row in rows:
        session = by_id.get(row['session_id'])
        if session is None and row['status'] == 'unmatched':
            bill = originals.get(row['external_id'])
            candidates = []
            if bill:
                for s in sessions:
                    try:
                        if (s['vin'] == bill['vin'] and str(s.get('geofence_id')) != '1'
                                and abs((timestamp(s['start_date'], db=True)-timestamp(bill['chargeStartDateTime'])).total_seconds()) <= 300):
                            candidates.append(s)
                    except (ValueError, TypeError, KeyError):
                        continue
            if len(candidates) == 1:
                session = candidates[0]
                row.update(session_id=session['id'], status='conflict',
                           reason='Possible session, but end/energy corroboration failed; no write')
        if session is not None:
            cost = session.get('cost')
            row['current_cost'] = format(Decimal(str(cost)), '.2f') if cost is not None else None
            if row['status'] == 'protected' and row['current_cost'] != row['amount']:
                note = 'Existing cost differs from Tesla bill; existing cost preserved'
                if row.get('evidence', {}).get('policy_since'):
                    note += '; ' + row['reason']
                row.update(status='conflict', reason=note)
        event = latest.get(row['external_id'])
        if event and event['outcome'] == 'imported':
            if (row['session_id'] == event['session_id'] and row['amount'] == event['amount']
                    and row.get('current_cost') == event['amount']):
                row.update(status='imported', reason='Previously imported; current TeslaMate cost verified')
            else:
                row.update(status='conflict', review_required=True,
                           reason='Previously imported evidence or current cost changed; manual review required')
        elif event and event['outcome'] in ('pending', 'uncertain'):
            row.update(status='conflict', review_required=True,
                       reason='Previous commit outcome uncertain; manual verification required, no retry')
        if row['session_id'] is not None and row['session_id'] in blocked_sessions:
            row.update(status='conflict', review_required=True,
                       reason='Session has an unresolved commit outcome; manual review required, no retry')
        if row['external_id'] is None:
            # Malformed records still have a stable redacted review identity.
            import hashlib
            row['external_id'] = 'invalid-' + hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:24]
    return rows


def _pending_evidence(store, history, row):
    root = store.path.parent / 'operations'
    if root.is_symlink():
        raise ValueError('unsafe operation path')
    root.mkdir(mode=0o700, exist_ok=True)
    path = root / (uuid.uuid4().hex + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'history': history, 'row': row}, f, default=str, allow_nan=False)
        f.flush(); os.fsync(f.fileno())
    fd = os.open(root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return path.name


def run_import(history, repo, store, *, apply=False, allowed_session_ids=None, policy=None):
    """Fresh preview, durable pending evidence, guarded writes, fresh DB readback.

    Caller must take a backup before apply=True. Any unknown commit is NOT retried.
    Dashboard is a review surface, never a DB writer. No tokens enter the ledger.
    """
    events = store.events()
    sessions = repo.sessions_for(history)
    rows = _review_rows(build_preview(history, sessions, policy=policy), sessions, history, events)
    store.publish(rows, source='saved Fleet billing history')
    results = []
    if apply:
        for row in rows:
            if row['status'] != 'ready' or (allowed_session_ids is not None and row['session_id'] not in allowed_session_ids):
                continue
            evidence = _pending_evidence(store, history, row)
            store.record_event(row['external_id'], row['session_id'], row['amount'], 'pending', evidence)
            try:
                result = repo.apply(row)
                if policy is not None and result['status'] == 'imported':
                    from charging_costs.policy import session_snapshot
                    verified_sessions = repo.sessions_for(history)
                    verified = build_preview(history, verified_sessions, policy=policy)
                    matches = [r for r in verified if r['external_id'] == row['external_id']
                               and r['session_id'] == row['session_id'] and r['amount'] == row['amount']
                               and r['status'] == 'protected']
                    target = next((s for s in verified_sessions if s['id'] == row['session_id']), None)
                    if (len(matches) != 1 or target is None
                            or target.get('cost') is None or Decimal(str(target['cost'])) != Decimal(row['amount'])
                            or session_snapshot(dict(target, cost=row['previous_cost']))
                            != row['evidence']['session_snapshot']):
                        result = dict(status='uncertain', reason='Independent verification failed; manual review required')
            except Exception:
                result = dict(status='uncertain', reason='Database operation failed; commit outcome requires review; not retried')
            detail = result['reason']
            if policy is not None:
                detail = json.dumps({'old_cost': row.get('previous_cost'), 'new_cost': row['amount'],
                                     'delta': row.get('delta'), 'billing_ref': row['external_id'],
                                     'source': 'finalized Tesla USD bill', 'reason': str(result['reason'])[:256]},
                                    allow_nan=False, sort_keys=True)
            store.record_event(row['external_id'], row['session_id'], row['amount'], result['status'], detail)
            results.append(result)
            if result['status'] == 'uncertain':
                break
        sessions = repo.sessions_for(history)
        rows = _review_rows(build_preview(history, sessions, policy=policy), sessions, history, store.events())
        store.publish(rows, source='saved Fleet billing history')
    return {'imported': sum(r['status'] == 'imported' for r in results),
            'uncertain': sum(r['status'] == 'uncertain' for r in results),
            'statuses': {state: sum(r['status'] == state for r in rows) for state in sorted({r['status'] for r in rows})},
            'results': results}
