"""Durable, explicitly invoked billing queue. Importing performs no work.

API:
* Scheduler(root, clock=time.time).sync(detector_snapshot['rows']) accepts only
  completed DB-confirmed session IDs: confirmed_away or unknown_location. A NULL
  geofence is eligible, not proof of a Supercharger. Home is never eligible.
  Epochs must be finite, ordered, not future, and within seven days. sync is
  atomic, deduplicates by TeslaMate session_id, and raises CapacityError rather
  than evicting live jobs when the 1000-job queue is full.
* run_due(check_jobs) coalesces ALL currently due jobs into ONE callback. There
  are slots at completion receipt +300/+1800/+7200 seconds. An overdue run uses
  the latest elapsed slot and retires earlier ones, never bursts after restart.
  'attempts' is the consumed slot index (0..3), NOT an HTTP request counter.
* check_jobs(jobs) receives copies with session_id/car_id/observed_at/state/
  attempts/next_due. Return {integer_session_id: state}, covering every job.
  waiting means a valid bill response with no matching bill; only it advances
  to the next finite slot, ending exhausted after slot three. imported,
  protected, conflict, unmatched, manual_review, and auth_blocked are terminal.
  budget_paused refunds that job's slot and postpones the whole account until
  next UTC day (at least 60 seconds). It must mean no request/import occurred
  for that job. HTTP budgets/page limits are the CALLBACK's responsibility.
  Optional whole-round {'status': 'paused', 'retry_at': epoch} likewise refunds
  slots and pauses the account; use only before requests/import side effects.
* Every round durably writes pending/attempts and a 60-second account cooldown
  BEFORE calling out. Exceptions or malformed/partial results stop the entire
  round as needs_review. Abrupt process death leaves pending; the next run_due,
  under the process flock, retires it to needs_review without calling out.
  Never automatically retry an ambiguous DB commit.
* sync/run_due/snapshot return status, counts by state, effective next_due,
  and sanitized jobs. snapshot is read-only; it does not recover or prune.
  Maintenance during sync/run_due expires jobs after seven days and SQLite
  auto_vacuum reclaims pages. This is not a billing-history warehouse.

No network, vehicle, publishing, or billing import code lives here. Connections
are scoped to operations, so no close() is needed. A private 0700 root and 0600
owned regular files are required; symlinks/hardlinks are rejected on opening.
Like SQLite, this assumes no hostile same-uid process replaces validated paths.
"""
import fcntl
import math
import os
import sqlite3
import stat
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path


def _private_root(root):
    root = Path(root).absolute()
    if '..' in root.parts or root == Path('/'):
        raise ValueError('invalid scheduler directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('scheduler directory must be private and owned')
        for name in ('billing_scheduler.sqlite3', 'scheduler.lock'):
            item = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                _validate_file(os.fstat(item))
            finally:
                os.close(item)
        for suffix in ('-journal', '-wal', '-shm'):
            try:
                info = os.stat('billing_scheduler.sqlite3' + suffix, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            _validate_file(info)
    except OSError as exc:
        raise ValueError('unsafe scheduler path') from exc
    finally:
        os.close(fd)
    return root


def _validate_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('scheduler file must be private, owned, and not linked')


class CapacityError(RuntimeError):
    """Queue is full; input batch was not accepted. Operator must resolve it."""


class Scheduler:
    MAX_JOBS = 1000
    RETENTION_SECONDS = 7 * 86400
    def __init__(self, root, clock=time.time):
        self.root = _private_root(root)
        self.path = self.root / 'billing_scheduler.sqlite3'
        self.clock = clock
        with self._connect() as db:
            db.execute('PRAGMA auto_vacuum=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS control (id INTEGER PRIMARY KEY CHECK(id=1), not_before REAL)')
            db.execute('INSERT OR IGNORE INTO control VALUES (1,0)')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (session_id INTEGER PRIMARY KEY, '
                       'car_id INTEGER, observed_at REAL, state TEXT, attempts INTEGER, next_due REAL, started_at REAL)')
            if 'started_at' not in {r[1] for r in db.execute('PRAGMA table_info(jobs)')}:
                db.execute('ALTER TABLE jobs ADD COLUMN started_at REAL')
                db.execute("UPDATE jobs SET state='needs_review',next_due=NULL WHERE started_at IS NULL")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA secure_delete=ON')
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self):
        with self._connect() as db:
            db.execute('BEGIN')
            db.row_factory = sqlite3.Row
            jobs = [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY session_id')]
            not_before = db.execute('SELECT not_before FROM control WHERE id=1').fetchone()[0]
        next_due = min((max(j['next_due'], not_before) for j in jobs
                        if j['next_due'] is not None), default=None)
        return {'status': 'ok', 'jobs': jobs,
                'counts': dict(Counter(j['state'] for j in jobs)), 'next_due': next_due}

    @staticmethod
    def _timestamp(value):
        return (type(value) in (int, float) and 0 <= value <= 253402300799
                and math.isfinite(value))

    def _now(self):
        now = self.clock()
        if not self._timestamp(now):
            raise ValueError('clock must return a finite nonnegative UNIX epoch')
        return now

    def _eligible(self, c, now):
        return (isinstance(c, dict)
                and isinstance(c.get('status'), str)
                and c.get('status') in {'confirmed_away', 'unknown_location'}
                and (c.get('location') is None or isinstance(c.get('location'), str))
                and c.get('geofence_id') != 1 and c.get('location') != 'home'
                and all(type(c.get(key)) is int and 0 < c[key] <= 9223372036854775807
                        for key in ('session_id', 'car_id'))
                and self._timestamp(c.get('observed_at'))
                and self._timestamp(c.get('started_at'))
                and c['started_at'] <= c['observed_at'] <= now
                and c['observed_at'] >= now - 7 * 86400)

    def _prune(self, db, now):
        db.execute('DELETE FROM jobs WHERE observed_at < ?', (now - self.RETENTION_SECONDS,))

    def sync(self, candidates):
        now = self._now()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._prune(db, now)
            count = db.execute('SELECT count(*) FROM jobs').fetchone()[0]
            home_sessions = set()
            for c in candidates:
                if (isinstance(c, dict) and type(c.get('session_id')) is int
                        and 0 < c['session_id'] <= 9223372036854775807
                        and (c.get('status') == 'skipped_home' or c.get('geofence_id') == 1
                             or c.get('location') == 'home')):
                    home_sessions.add(c['session_id'])
                    db.execute("UPDATE jobs SET state='skipped_home',next_due=NULL "
                               "WHERE session_id=? AND state IN ('scheduled','waiting','budget_paused')",
                               (c['session_id'],))
                    continue
                if not self._eligible(c, now) or c['session_id'] in home_sessions:
                    continue
                if db.execute('SELECT 1 FROM jobs WHERE session_id=?', (c['session_id'],)).fetchone():
                    continue
                if count >= self.MAX_JOBS:
                    raise CapacityError('billing scheduler capacity reached')
                count += 1
                db.execute('INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?)',
                           (c['session_id'], c['car_id'], c['observed_at'],
                            'scheduled', 0, c['observed_at'] + 300, c['started_at']))
        return self.snapshot()

    def run_due(self, check_callable):
        with open(self.root / 'scheduler.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(self.snapshot(), status='busy')
            return self._run_locked(check_callable)

    def _run_locked(self, check_callable):
        now = self._now()
        with self._connect() as db:
            self._prune(db, now)
            recovered = db.execute("UPDATE jobs SET state='needs_review',next_due=NULL WHERE state='pending'").rowcount
        if recovered:
            return self.snapshot()
        summary = self.snapshot()
        if summary['next_due'] is None or summary['next_due'] > now:
            return summary
        jobs = [j for j in summary['jobs'] if j['next_due'] is not None and j['next_due'] <= now][:20]
        if jobs:
            previous_attempts = {j['session_id']: j['attempts'] for j in jobs}
            with self._connect() as db:
                db.execute('UPDATE control SET not_before=? WHERE id=1', (now + 60,))
                for job in jobs:
                    job['attempts'] = max(i for i, delay in enumerate((300, 1800, 7200), 1)
                                          if job['observed_at'] + delay <= now)
                    db.execute("UPDATE jobs SET state='pending',attempts=?,next_due=NULL WHERE session_id=?",
                               (job['attempts'], job['session_id']))
            try:
                results = check_callable([dict(j) for j in jobs])
                if isinstance(results, dict) and results.get('status') == 'paused':
                    retry_at = results.get('retry_at')
                    if (isinstance(retry_at, bool) or not isinstance(retry_at, (int, float))
                            or not now < retry_at <= now + 7 * 86400):
                        raise ValueError('invalid pause')
                    with self._connect() as db:
                        db.execute('UPDATE control SET not_before=? WHERE id=1', (max(now + 60, retry_at),))
                        for job in jobs:
                            db.execute("UPDATE jobs SET state='scheduled',attempts=?,next_due=? WHERE session_id=?",
                                       (previous_attempts[job['session_id']], max(now + 60, retry_at), job['session_id']))
                    return self.snapshot()
                allowed = {'waiting', 'imported', 'protected', 'conflict', 'unmatched',
                           'manual_review', 'auth_blocked', 'budget_paused'}
                if (not isinstance(results, dict)
                        or set(results) != {j['session_id'] for j in jobs}
                        or any(type(k) is not int for k in results)
                        or any(not isinstance(v, str) or v not in allowed for v in results.values())):
                    raise ValueError('invalid callback outcomes')
            except Exception:
                results = {j['session_id']: 'needs_review' for j in jobs}
            with self._connect() as db:
                for job in jobs:
                    stage = job['attempts']
                    state = results[job['session_id']]
                    next_due = None
                    if state == 'budget_paused':
                        stage = previous_attempts[job['session_id']]
                        next_due = max(now + 60, (now // 86400 + 1) * 86400)
                        db.execute('UPDATE control SET not_before=? WHERE id=1', (next_due,))
                    if state == 'waiting':
                        if stage == 3:
                            state = 'exhausted'
                        else:
                            next_due = job['observed_at'] + (300, 1800, 7200)[stage]
                    db.execute('UPDATE jobs SET state=?,attempts=?,next_due=? WHERE session_id=?',
                               (state, stage, next_due, job['session_id']))
        return self.snapshot()
