"""Daily bounded DB reconciliation; importing performs no external work.

DailyReconciler(root, clock=time.time, timezone_name='UTC', hour=8).run_due(check,
 database_factory=OwnerDatabase) calls check(jobs) in batches of at most twenty.
The callback is responsible for shared HTTP budgets and guarded import writes.
Jobs carry actual DB start/end epochs, not fabricated MQTT receipt times.

A local date is durably reserved before discovery, and each batch is pending
before its callback. Exceptions/partial results and recovered pending work become
needs_review, never blind write retries. snapshot() and non-due runs preserve
last_run/state/counts/jobs for monitoring; no raw DB evidence or exception text
is exposed. NULL geofence is eligible, not proof of a Supercharger.
"""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import fcntl
import math
import os
import sqlite3
import time

from charging_costs.importer import timestamp
from manual_reader import private_path
from owner_database import OwnerDatabase


COLUMNS = ('id', 'car_id', 'start_date', 'end_date', 'geofence_id', 'cost')
RECENT_MISSING = (
    'SELECT cp.id, cp.car_id, cp.start_date, cp.end_date, cp.geofence_id, cp.cost '
    'FROM charging_processes cp WHERE cp.cost IS NULL '
    'AND cp.geofence_id IS DISTINCT FROM 1 '
    'AND cp.end_date >= %s AND cp.end_date <= %s '
    'AND cp.start_date IS NOT NULL AND cp.start_date <= cp.end_date '
    'ORDER BY cp.end_date, cp.id LIMIT 1001')
BLOCKED = frozenset({'manual_review', 'auth_blocked', 'conflict', 'needs_review',
                     'pending', 'imported', 'protected', 'unmatched'})
OUTCOMES = frozenset({'waiting', 'imported', 'protected', 'conflict', 'unmatched',
                      'manual_review', 'auth_blocked', 'budget_paused'})


def _discover(database, now):
    """Own the new connection and read-only transaction, including failure cleanup."""
    conn = database.connect()
    try:
        conn.autocommit = False
        cursor = conn.cursor()
        try:
            cursor.execute('SET TRANSACTION READ ONLY')
            bounds = tuple(datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None)
                           for t in (now - 7 * 86400, now - 7200))
            cursor.execute(RECENT_MISSING, bounds)
            return cursor.fetchall()
        finally:
            cursor.close()
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def _jobs(rows, now):
    jobs = []
    seen = set()
    for raw in rows:
        try:
            row = dict(raw) if isinstance(raw, dict) else dict(zip(COLUMNS, raw))
            start = timestamp(row['start_date'], db=True).timestamp()
            end = timestamp(row['end_date'], db=True).timestamp()
            if (row['cost'] is not None or str(row['geofence_id']) == '1'
                    or any(type(row[k]) is not int or not 0 < row[k] <= 9223372036854775807
                           for k in ('id', 'car_id'))
                    or not 0 <= start <= end <= now - 7200 or end < now - 7 * 86400):
                continue
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if row['id'] in seen:
            raise ValueError('ambiguous session identity')
        seen.add(row['id'])
        jobs.append(dict(session_id=row['id'], car_id=row['car_id'],
                         started_at=start, observed_at=end,
                         source='daily_db_reconciliation'))
    return jobs


class DailyReconciler:
    def __init__(self, root, clock=time.time, timezone_name='UTC', hour=8):
        if type(hour) is not int or not 0 <= hour <= 23:
            raise ValueError('invalid reconciliation hour')
        self.zone = ZoneInfo(timezone_name)
        self.hour = hour
        self.clock = clock
        self.path = private_path(Path(root) / 'daily_reconcile.sqlite3', create=True)
        self.root = self.path.parent
        self.lock_path = private_path(self.root / 'daily_reconcile.lock', create=True)
        with self._connect() as db:
            db.execute('PRAGMA auto_vacuum=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS runs (local_date TEXT PRIMARY KEY, '
                       'reserved_at REAL, state TEXT, discovered INTEGER DEFAULT 0, overflow INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (session_id INTEGER PRIMARY KEY, observed_at REAL, state TEXT)')

    @contextmanager
    def _connect(self):
        private_path(self.path)
        for suffix in ('-journal', '-wal', '-shm'):
            private_path(Path(str(self.path) + suffix))
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA secure_delete=ON')
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self):
        """Read-only safe summary. Pending recovery happens only under run_due's flock."""
        with self._connect() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT local_date,reserved_at,state,discovered,overflow FROM runs '
                             'ORDER BY reserved_at DESC LIMIT 1').fetchone()
            jobs = [dict(zip(('session_id', 'observed_at', 'state'), r))
                    for r in db.execute('SELECT * FROM jobs ORDER BY session_id')]
        last = dict(zip(('local_date', 'reserved_at', 'state', 'discovered', 'overflow'), row)) if row else None
        return {'status': last['state'] if last else 'not_due', 'last_run': last,
                'jobs': jobs, 'counts': dict(Counter(j['state'] for j in jobs))}

    def _finish(self, day, state):
        with self._connect() as db:
            db.execute('UPDATE runs SET state=? WHERE local_date=?', (state, day))
        return self.snapshot()

    def _scheduler_blocked(self, jobs):
        """Read only exact candidate IDs; never mutate the finite event scheduler."""
        path = private_path(self.root / 'billing_scheduler.sqlite3')
        if not path.exists() or not jobs:
            return set()
        for suffix in ('-journal', '-wal', '-shm'):
            private_path(Path(str(path) + suffix))
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            blocked = set()
            for offset in range(0, len(jobs), 20):
                ids = [j['session_id'] for j in jobs[offset:offset + 20]]
                rows = db.execute('SELECT session_id,state FROM jobs WHERE session_id IN (' +
                                  ','.join('?' for _ in ids) + ')', ids)
                blocked.update(sid for sid, state in rows if state in BLOCKED)
            return blocked
        finally:
            db.close()

    def run_due(self, check_jobs_callable, database_factory=OwnerDatabase):
        private_path(self.lock_path)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(self.snapshot(), status='busy')
            try:
                return self._run_locked(check_jobs_callable, database_factory)
            except sqlite3.Error:
                # Even maintenance/recovery may fail before a round is reserved.
                try:
                    return dict(self.snapshot(), status='needs_review')
                except Exception:
                    return {'status': 'needs_review', 'last_run': None, 'counts': {}, 'jobs': []}
        finally:
            os.close(fd)

    def _run_locked(self, check, database_factory):
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 253402214399:
            raise ValueError('clock must return a finite nonnegative UNIX epoch')
        local = datetime.fromtimestamp(now, self.zone)
        day = local.date().isoformat()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            recovered = db.execute("UPDATE runs SET state='needs_review' WHERE state='pending'").rowcount
            db.execute("UPDATE jobs SET state='needs_review' WHERE state='pending'")
            db.execute('DELETE FROM jobs WHERE observed_at < ?', (now - 7 * 86400,))
            db.execute('DELETE FROM runs WHERE reserved_at < ?', (now - 7 * 86400,))
            already = db.execute('SELECT 1 FROM runs WHERE local_date=?', (day,)).fetchone()
        if recovered or local.hour < self.hour or already:
            return self.snapshot()
        try:
            # Commit must succeed BEFORE database discovery or any callback.
            with self._connect() as db:
                db.execute('INSERT INTO runs (local_date,reserved_at,state) VALUES (?,?,?)', (day, now, 'pending'))
            with database_factory() as database:
                rows = _discover(database, now)
            with self._connect() as db:
                db.execute('UPDATE runs SET discovered=?,overflow=? WHERE local_date=?',
                           (len(rows), int(len(rows) > 1000), day))
            if len(rows) > 1000:
                return self._finish(day, 'overflow')
            jobs = _jobs(rows, now)
            with self._connect() as db:
                blocked = {sid for sid, state in db.execute('SELECT session_id,state FROM jobs') if state in BLOCKED}
            blocked.update(self._scheduler_blocked(jobs))
            jobs = [j for j in jobs if j['session_id'] not in blocked]
            with self._connect() as db:
                for job in jobs:
                    db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?)',
                               (job['session_id'], job['observed_at'], 'scheduled'))
            for offset in range(0, len(jobs), 20):
                batch = jobs[offset:offset + 20]
                with self._connect() as db:
                    for job in batch:
                        db.execute("UPDATE jobs SET state='pending' WHERE session_id=?", (job['session_id'],))
                results = check([dict(j) for j in batch])
                if (not isinstance(results, dict) or set(results) != {j['session_id'] for j in batch}
                        or any(type(k) is not int for k in results)
                        or any(not isinstance(v, str) or v not in OUTCOMES for v in results.values())):
                    raise ValueError('invalid callback outcomes')
                with self._connect() as db:
                    for job in batch:
                        db.execute('UPDATE jobs SET state=? WHERE session_id=?',
                                   (results[job['session_id']], job['session_id']))
                if 'budget_paused' in results.values():
                    return self._finish(day, 'budget_paused')
                if all(value in {'manual_review', 'auth_blocked', 'conflict'} for value in results.values()):
                    return self._finish(day, 'needs_review')
            return self._finish(day, 'completed')
        except Exception:
            # If persistence is unavailable, no further discovery/callback is attempted.
            try:
                with self._connect() as db:
                    db.execute('INSERT OR IGNORE INTO runs (local_date,reserved_at,state) VALUES (?,?,?)',
                               (day, now, 'needs_review'))
                    db.execute("UPDATE jobs SET state='needs_review' WHERE state='pending'")
                return self._finish(day, 'needs_review')
            except Exception:
                return {'status': 'needs_review', 'last_run': None, 'counts': {}, 'jobs': []}
