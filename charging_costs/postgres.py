"""Transactional TeslaMate adapter. No schema changes, Tesla calls, or scheduling.

connect() must return a NEW owned DBAPI connection (psycopg/psycopg2), with
transactions supported. It is closed after each operation. sessions_for(history)
retains a deep copy of the original complete <=1000-record history batch on this
instance; apply(row) MUST use the same instance/batch. Dashboard rows alone are
never write authorization. The parent may persist the original history privately
(JSON), call sessions_for(restored_history), then apply(saved_row) after restart.
Never expose that original history (VIN/invoice metadata) in dashboard responses.

Persist a pending audit BEFORE apply. A successful result includes an in-transaction
readback and is returned only after commit. A connection/commit error propagates:
commit outcome may be unknown. Same-amount existing costs remain protected, NOT
claimed imported; cross-database recovery must retain explicit ownership evidence
or mark the outcome uncertain for review. No atomic SQLite/Postgres commit is claimed.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from .importer import build_preview, history_records, normalize_bill

COLUMNS = ('id', 'vin', 'start_date', 'end_date', 'charge_energy_used',
           'charge_energy_added', 'cost', 'geofence_id', 'car_id')
SELECT = '''SELECT cp.id, c.vin, cp.start_date, cp.end_date,
                   cp.charge_energy_used, cp.charge_energy_added, cp.cost, cp.geofence_id, cp.car_id
            FROM charging_processes cp JOIN cars c ON c.id = cp.car_id'''


def _rows(cursor):
    return [dict(zip(COLUMNS, row)) if not isinstance(row, dict) else dict(row)
            for row in cursor.fetchall()]


class PgRepository:
    def __init__(self, connect, *, policy=None):
        self.connect = connect
        self.policy = policy
        self._history = None

    def _sessions(self, cursor, history):
        clauses, params = [], []
        for raw in history_records(history):
            try:
                bill = normalize_bill(raw)
            except (ValueError, TypeError, KeyError, ArithmeticError, OverflowError):
                continue
            clauses.append('(c.vin = %s AND cp.start_date BETWEEN %s AND %s)')
            start = bill['start'].replace(tzinfo=None)
            params.extend((bill['vin'], start - timedelta(seconds=300), start + timedelta(seconds=300)))
        if not clauses:
            return []
        cursor.execute(SELECT + ' WHERE ' + ' OR '.join(clauses) + ' LIMIT 10001', tuple(params))
        records = _rows(cursor)
        if len(records) > 10000:
            raise ValueError('candidate session limit exceeded; narrow history batch')
        return records

    def sessions_for(self, history):
        """Bounded parameterized READ ONLY lookup; privately retain original batch."""
        records = deepcopy(history_records(history))
        self._history = None
        conn = self.connect()
        try:
            conn.autocommit = False
            cursor = conn.cursor()
            try:
                cursor.execute('SET TRANSACTION READ ONLY')
                result = self._sessions(cursor, records)
                conn.rollback()
            finally:
                cursor.close()
        finally:
            conn.close()
        self._history = records
        return result

    def apply(self, row):
        """Rebuild from original batch, lock target/car, recheck, CAS, readback, commit.

        Returns {status, session_id, external_id, reason, readback}, all JSON safe.
        Refusals never update. Non-NULL costs are protected unless an explicit
        date-bounded policy permits replacement with an unchanged preview snapshot.
        Throws on DB failures; caller must not mark them imported.
        """
        outcome = {'status': 'invalid', 'session_id': row.get('session_id'),
                   'external_id': row.get('external_id'), 'reason': 'Missing original evidence or ready row',
                   'readback': None}
        if self._history is None or row.get('status') != 'ready' or type(row.get('session_id')) is not int:
            return outcome
        conn = self.connect()
        try:
            conn.autocommit = False
            cursor = conn.cursor()
            try:
                cursor.execute('SET TRANSACTION ISOLATION LEVEL SERIALIZABLE')
                cursor.execute(SELECT + ' WHERE cp.id = %s FOR UPDATE OF cp, c', (row['session_id'],))
                locked = _rows(cursor)
                if not locked:
                    outcome.update(status='unmatched', reason='Target disappeared')
                    conn.rollback()
                    return outcome
                fresh = build_preview(self._history, self._sessions(cursor, self._history), policy=self.policy)
                matches = [candidate for candidate in fresh if candidate['external_id'] == row.get('external_id')]
                if len(matches) != 1:
                    outcome.update(status='conflict', reason='Billing ID is no longer unique')
                    conn.rollback()
                    return outcome
                candidate = matches[0]
                if candidate['status'] != 'ready':
                    outcome.update(status=candidate['status'], reason=candidate['reason'])
                    conn.rollback()
                    return outcome
                if (candidate['session_id'] != row['session_id'] or candidate['amount'] != row.get('amount')
                        or candidate['currency'] != row.get('currency')):
                    outcome.update(status='conflict', reason='Preview no longer agrees with original evidence')
                    conn.rollback()
                    return outcome
                if self.policy is not None and (
                        candidate.get('previous_cost') != row.get('previous_cost')
                        or candidate['evidence'].get('session_snapshot') != row.get('evidence', {}).get('session_snapshot')
                        or candidate['evidence'].get('policy_since') != row.get('evidence', {}).get('policy_since')):
                    outcome.update(status='conflict', reason='Cost or identity changed since preview; no write')
                    conn.rollback()
                    return outcome
                amount = Decimal(candidate['amount'])
                if self.policy is None:
                    cursor.execute('''UPDATE charging_processes SET cost = %s
                                      WHERE id = %s AND cost IS NULL
                                        AND geofence_id IS DISTINCT FROM %s RETURNING id''',
                                   (amount, row['session_id'], 1))
                else:
                    previous = candidate['previous_cost']
                    cursor.execute('''UPDATE charging_processes SET cost = %s
                                      WHERE id = %s AND cost IS NOT DISTINCT FROM %s
                                        AND geofence_id IS DISTINCT FROM %s RETURNING id''',
                                   (amount, row['session_id'], None if previous is None else Decimal(previous), 1))
                updated = cursor.fetchone()
                if updated is None:
                    outcome.update(status='protected', reason='Cost or home protection prevented write')
                    conn.rollback()
                    return outcome
                cursor.execute('SELECT id, cost FROM charging_processes WHERE id = %s', (row['session_id'],))
                readback = cursor.fetchone()
                if isinstance(readback, dict):
                    readback = (readback['id'], readback['cost'])
                if readback is None or readback[0] != row['session_id'] or readback[1] != amount:
                    raise RuntimeError('cost readback failed')
                outcome.update(status='imported', reason='Committed cost from corroborated paid billing',
                               readback={'session_id': readback[0], 'cost': format(readback[1], '.2f')})
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise
            finally:
                cursor.close()
        finally:
            conn.close()
