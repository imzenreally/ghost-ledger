"""Passive TeslaMate completion hints; no network, publishing, or billing actions.

Contract:
* Call on_message from one serialized owner thread. Default topics are exactly
  teslamate/cars/<id>/state. Retained packets never arm or finish a session.
* Call disconnected on EVERY MQTT disconnect (including failed reconnects).
  Optionally pass monotonically increasing integer connection_generation values
  to reject obsolete callbacks. received_at is a local UNIX receipt timestamp,
  NOT vehicle time. Ordering/duplicate suppression cannot repair lost messages.
* A candidate is committed before status=candidate is returned. Its started_at
  is the FIRST LIVE charging receipt, not the true session start; observed_at
  is the first subsequent recognized noncharging receipt. Use those times and
  car_id for a bounded, read-only DB corroboration query. No reconnect backfill.
* Geofence/charging_state packets are intentionally ignored: TeslaMate publishes
  them independently, so a cached Home value is not authoritative. Every event
  starts unknown/needs_db_confirmation. confirm MUST be supplied only after the
  caller independently matches a TeslaMate DB session to this car/time window.
  completed=True means a DB completion, not a guess from MQTT. geofence_id=1 is
  Home, positive non-1 IDs are away; NULL/invalid IDs remain unknown. Away does
  NOT mean Supercharger. Confirmation never triggers bill fetch/import/schedule.
* Queue retention is seven days by observation, maximum 1000 candidates. This is
  a short-lived work queue, not charging history. IDs/times/classification only;
  no tokens, VIN, GPS, raw payloads, or geofence names are stored.
* Root and database must be owned by this uid, modes 0700/0600, no symlinks or
  hard-linked database files. Directory traversal/file creation use O_NOFOLLOW.
  Like SQLite itself, this assumes no hostile same-uid process replaces paths.
"""
import math
import os
import re
import sqlite3
import stat
import time
import uuid
from pathlib import Path


def _private_database(root):
    root = Path(root).absolute()
    if '..' in root.parts or root == Path('/'):
        raise ValueError('invalid candidate directory')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError('candidate directory must be private (0700) and owned')
        name = 'charging_candidates.sqlite3'
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('candidate database must be a private owned regular file')
        finally:
            os.close(descriptor)
        for suffix in ('-journal', '-wal', '-shm'):
            try:
                info = os.stat(name + suffix, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('unsafe candidate database sidecar')
    except OSError as exc:
        raise ValueError('unsafe candidate database path') from exc
    finally:
        os.close(directory)
    return root / name


class Detector:
    MAX_CANDIDATES = 1000
    RETENTION_SECONDS = 7 * 24 * 60 * 60

    def __init__(self, root, clock=time.time, *, namespace=''):
        if not isinstance(namespace, str) or (namespace and not re.fullmatch(
                r'[A-Za-z0-9_-]{1,64}', namespace)):
            raise ValueError('namespace must be one simple topic segment')
        prefix = 'teslamate' + ('/' + namespace if namespace else '')
        self._topic = re.compile(re.escape(prefix) + r'/cars/([1-9][0-9]{0,8})/state')
        self.path = _private_database(root)
        self.root = self.path.parent
        self.clock = clock
        self._cars = {}
        self._last = {}
        self._generation = None
        self._db = sqlite3.connect(self.path, timeout=5)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute('PRAGMA journal_mode=DELETE')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('PRAGMA secure_delete=ON')
            self._db.execute('CREATE TABLE IF NOT EXISTS candidates ('
                             'event_id TEXT PRIMARY KEY, car_id INTEGER NOT NULL, '
                             'started_at REAL NOT NULL, observed_at REAL NOT NULL, '
                             'status TEXT NOT NULL, location TEXT NOT NULL, '
                             'session_id INTEGER, geofence_id INTEGER, ended_at REAL)')
            with self._db:
                self._prune()
        except Exception:
            self._db.close()
            raise

    def _prune(self):
        self._db.execute('DELETE FROM candidates WHERE observed_at < ?',
                         (self.clock() - self.RETENTION_SECONDS,))
        self._db.execute('DELETE FROM candidates WHERE event_id IN ('
                         'SELECT event_id FROM candidates ORDER BY observed_at DESC, '
                         'rowid DESC LIMIT -1 OFFSET ?)', (self.MAX_CANDIDATES,))

    def disconnected(self):
        """Forget arming/context; no transition is inferred across a connection gap."""
        self._cars.clear()
        self._last.clear()

    def on_message(self, topic, payload, retained=False, *, received_at=None,
                   connection_generation=None):
        if retained or not isinstance(topic, str) or len(topic) > 256:
            return {'status': 'ignored'}
        match = self._topic.fullmatch(topic)
        if not match or not isinstance(payload, (str, bytes)) or len(payload) > 64:
            return {'status': 'ignored'}
        if isinstance(payload, bytes):
            try:
                payload = payload.decode('utf-8')
            except UnicodeDecodeError:
                return {'status': 'ignored'}
        if payload not in {'charging', 'online', 'asleep', 'offline', 'driving', 'suspended'}:
            return {'status': 'ignored'}
        now = self.clock() if received_at is None else received_at
        if (isinstance(now, bool) or not isinstance(now, (int, float))
                or not 0 <= now <= 253402300799 or not math.isfinite(now)):
            return {'status': 'ignored'}
        if connection_generation is not None:
            if type(connection_generation) is not int or connection_generation < 0:
                return {'status': 'ignored'}
            if self._generation is not None and connection_generation < self._generation:
                return {'status': 'ignored'}
            if connection_generation != self._generation:
                self.disconnected()
                self._generation = connection_generation
        car_id = int(match[1])
        if now < self._last.get(car_id, float('-inf')):
            return {'status': 'ignored'}
        self._last[car_id] = now
        if payload == 'charging':
            self._cars.setdefault(car_id, now)
        elif car_id in self._cars:
            started = self._cars[car_id]
            event_id = f'{car_id}:{uuid.uuid4().hex}'
            try:
                with self._db:
                    self._db.execute('INSERT INTO candidates '
                                     '(event_id,car_id,started_at,observed_at,status,location) '
                                     'VALUES (?,?,?,?,?,?)',
                                     (event_id, car_id, started, now,
                                      'needs_db_confirmation', 'unknown'))
                    self._prune()
            except sqlite3.Error:
                return {'status': 'storage_error', 'retryable': True}
            del self._cars[car_id]
            return {'status': 'candidate', 'event_id': event_id}
        return {'status': 'ignored'}

    def confirm(self, event_id, session_id, geofence_id, completed, ended_at=None):
        """Persist caller-verified DB evidence only; never perform follow-up actions."""
        with self._db:
            self._prune()
            row = self._db.execute('SELECT * FROM candidates WHERE event_id=?',
                                   (event_id,)).fetchone()
            if row is None:
                return {'status': 'not_found'}
            if row['status'] in {'skipped_home', 'confirmed_away'}:
                return {'status': row['status'], 'event_id': event_id}
            if completed is not True or type(session_id) is not int or session_id <= 0:
                return {'status': row['status'], 'event_id': event_id}
            if (session_id > 9223372036854775807
                    or row['session_id'] not in (None, session_id)):
                return {'status': 'invalid_confirmation', 'event_id': event_id}
            if type(geofence_id) is not int or not 0 < geofence_id <= 9223372036854775807:
                geofence_id = None
            if ended_at is not None and (isinstance(ended_at, bool)
                    or not isinstance(ended_at, (int, float)) or not math.isfinite(ended_at)):
                return {'status': 'invalid_confirmation', 'event_id': event_id}
            location = 'home' if geofence_id == 1 else ('away' if geofence_id else 'unknown')
            status = {'home': 'skipped_home', 'away': 'confirmed_away',
                      'unknown': 'unknown_location'}[location]
            self._db.execute('UPDATE candidates SET status=?,location=?,session_id=?, '
                             'geofence_id=?,ended_at=? WHERE event_id=?',
                             (status, location, session_id, geofence_id, ended_at, event_id))
        return {'status': status, 'event_id': event_id}

    def snapshot(self):
        with self._db:
            self._prune()
        return {'status': 'passive', 'coverage': 'live_only_gaps_may_miss_sessions',
                'armed_cars': sorted(self._cars),
                'rows': [dict(r) for r in self._db.execute(
                    'SELECT * FROM candidates ORDER BY observed_at, rowid')]}

    def close(self):
        self._db.close()
