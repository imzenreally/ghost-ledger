"""Private, at-most-once Pushover queue. Importing never performs network I/O.

emit returns a sanitized {'status': ...}; sent means API queue acceptance, not
receipt/read confirmation. Pending survives process death and is never retried.
Transport injection contract: (token, user, title, message) -> (HTTP status, bytes).
"""
from contextlib import contextmanager
import fcntl
import json
import re
import stat
import unicodedata
import os
from pathlib import Path
import sqlite3
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def pushover_transport(token, user, title, message):
    """One bounded HTTPS POST; never follows redirects or environment proxies."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect())
    data = urllib.parse.urlencode({'token': token, 'user': user, 'title': title,
                                  'message': message, 'priority': '0'}).encode('ascii')
    request = urllib.request.Request('https://api.pushover.net/1/messages.json',
                                     data=data, method='POST',
                                     headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read(65537)
    except urllib.error.HTTPError as error:
        # Error bodies may contain submitted credentials; do not read or expose.
        status = error.code
        error.close()
        return status, b''


def _valid_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError('unsafe notification file')


def _open_directory(path, create=False):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('unsafe notification directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _credentials(path):
    if not path:
        return None
    path = Path(path).absolute()
    directory = _open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, 'rb') as source:
        _valid_file(os.fstat(source.fileno()))
        raw = source.read(4097)
    if len(raw) > 4096:
        raise ValueError('invalid notification credentials')
    values = {}
    for line in raw.decode('ascii').splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'(PUSHOVER_API_TOKEN|PUSHOVER_USER_KEY)=([A-Za-z0-9]{30})', line)
        if not match or match[1] in values:
            raise ValueError('invalid notification credentials')
        values[match[1]] = match[2]
    if set(values) != {'PUSHOVER_API_TOKEN', 'PUSHOVER_USER_KEY'}:
        raise ValueError('invalid notification credentials')
    return values['PUSHOVER_API_TOKEN'], values['PUSHOVER_USER_KEY']


def _text(value, limit):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(unicodedata.category(c).startswith('C') for c in value))


_ISSUE_TEXT = {
    'conflict': 'Cost conflict requires local review; no overwrite was made.',
    'needs_review': 'Billing result requires local review before another attempt.',
    'auth_blocked': 'Sign-in requires attention; billing access is blocked.',
    'budget_blocked': 'Billing lookup allowance is exhausted; review local status.',
    'daily_failure': 'Daily reconciliation requires local review.',
    'event_failure': 'Event billing operation requires local review.',
}


def _identifier(value):
    return type(value) is int and 0 < value <= 9223372036854775807


def _issue(state):
    return {'conflict': 'conflict', 'needs_review': 'needs_review',
            'manual_review': 'needs_review', 'uncertain': 'needs_review', 'auth_blocked': 'auth_blocked',
            'budget_paused': 'budget_blocked', 'budget_blocked': 'budget_blocked'}.get(state)


class CapacityError(RuntimeError):
    """Private notification history is full; no live dedupe keys are evicted."""


class Notifier:
    MAX_EVENTS = 1000
    RETENTION_SECONDS = 30 * 86400

    def __init__(self, root, credential_path=None, clock=time.time, transport=pushover_transport):
        self.root = Path(root).absolute()
        self.path = self.root / 'notifications.sqlite3'
        self.lock = self.root / 'notifications.lock'
        self.credential_path = credential_path
        self.clock, self.transport = clock, transport
        directory = _open_directory(self.root, create=True)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('notification directory must be private and owned')
            for path in (self.path, self.lock):
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                try:
                    # Separate exclusive creation from opening an existing file:
                    # concurrent O_CREAT|O_NOFOLLOW opens can fail with ENOENT
                    # on macOS even though the directory descriptor is valid.
                    fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
                except FileExistsError:
                    fd = os.open(path.name, flags, dir_fd=directory)
                try:
                    _valid_file(os.fstat(fd))
                finally:
                    os.close(fd)
        finally:
            os.close(directory)
        with self._locked():
            with self._db() as db:
                db.execute('PRAGMA auto_vacuum=FULL')
                db.execute('CREATE TABLE IF NOT EXISTS notifications (key TEXT PRIMARY KEY, state TEXT, created REAL)')
                db.execute('CREATE TABLE IF NOT EXISTS control (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                db.execute("UPDATE notifications SET state='uncertain' WHERE state='pending'")

    def _check_paths(self):
        directory = _open_directory(self.root)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('unsafe notification directory')
            for name in (self.path.name, self.lock.name,
                         *(self.path.name + s for s in ('-journal', '-wal', '-shm'))):
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    if name in (self.path.name, self.lock.name):
                        raise
                    continue
                _valid_file(info)
        finally:
            os.close(directory)

    @contextmanager
    def _db(self):
        self._check_paths()
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA secure_delete=ON')
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def _locked(self):
        self._check_paths()
        fd = os.open(self.lock, os.O_RDWR | os.O_NOFOLLOW)
        try:
            _valid_file(os.fstat(fd))
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def emit(self, key, title, message):
        """Attempt once. Invalid/private-state failures are sanitized, never raised."""
        try:
            with self._locked():
                return self._emit_locked(key, title, message)
        except CapacityError:
            return {'status': 'blocked', 'reason': 'capacity'}
        except Exception:
            return {'status': 'blocked'}

    def _emit_locked(self, key, title, message):
        if not (_text(key, 256) and _text(title, 250) and _text(message, 1024)):
            return {'status': 'blocked'}
        credentials = self._load_credentials()
        if isinstance(credentials, dict):
            return credentials
        now = self._now()
        with self._db() as db:
            db.execute('DELETE FROM notifications WHERE created < ?', (now - self.RETENTION_SECONDS,))
            db.execute("UPDATE notifications SET state='uncertain' WHERE state='pending'")
            if db.execute('SELECT 1 FROM notifications WHERE key=?', (key,)).fetchone():
                return {'status': 'duplicate'}
            if db.execute('SELECT count(*) FROM notifications').fetchone()[0] >= self.MAX_EVENTS:
                raise CapacityError('notification capacity exceeded')
            db.execute('INSERT INTO notifications VALUES (?, ?, ?)', (key, 'pending', now))
        state = 'uncertain'
        try:
            status, body = self.transport(*credentials, title, message)
            if type(status) is int and 400 <= status < 500:
                state = 'blocked'
            elif type(status) is int and status == 200 and isinstance(body, bytes) and len(body) <= 65536:
                payload = json.loads(body)
                result = payload.get('status') if isinstance(payload, dict) else None
                if type(result) is int and result in (0, 1):
                    state = 'sent' if result == 1 else 'blocked'
        except Exception:
            pass  # Response may have been lost after acceptance; never retry.
        try:
            with self._db() as db:
                db.execute('UPDATE notifications SET state=? WHERE key=?', (state, key))
        except Exception:
            return {'status': 'uncertain'}
        return {'status': state}

    def _load_credentials(self):
        try:
            path = self.credential_path
            if path is None:
                path = os.environ.get('GHOST_LEDGER_PUSHOVER_KEYS_FILE')
            credentials = _credentials(path)
            return credentials if credentials else {'status': 'disabled'}
        except FileNotFoundError:
            return {'status': 'disabled'}
        except Exception:
            return {'status': 'blocked'}

    def _now(self):
        now = self.clock()
        if type(now) not in (int, float) or not 0 <= now <= 253402300799:
            raise ValueError('invalid notification clock')
        return now

    def observe(self, store, scheduler_status, daily_status, passive_status):
        """Observe current snapshots; never copy arbitrary reasons into messages.

        Initial enable baselines the audit cursor (no historical import burst).
        Later imports use the original audit ID and session, not mutable previews.
        Issues coalesce across daily/event sources, at most once per UTC
        date. Routine waits, exhausted bill lookup slots and healthy empties are
        intentionally silent. Failures cannot interrupt the import worker.
        """
        try:
            credentials = self._load_credentials()
            if isinstance(credentials, dict):
                return credentials
            with self._locked():
                return self._observe_locked(store, scheduler_status or {}, daily_status or {}, passive_status or {})
        except CapacityError:
            return {'status': 'blocked', 'reason': 'capacity'}
        except Exception:
            return {'status': 'blocked'}

    def _observe_locked(self, store, scheduler, daily, passive):
        from datetime import datetime, timezone
        from decimal import Decimal, InvalidOperation
        now = self._now()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        # Gather before committing a cursor; a failed read must not lose events.
        events = store.events()
        events = sorted((e for e in events if isinstance(e, dict) and _identifier(e.get('id'))),
                        key=lambda e: e['id'])
        highest = max((e['id'] for e in events), default=0)
        with self._db() as db:
            db.execute('DELETE FROM notifications WHERE created < ?', (now - self.RETENTION_SECONDS,))
            saved = db.execute('SELECT value FROM control WHERE key=?', ('audit_cursor',)).fetchone()
            cursor = int(saved[0]) if saved else highest
            if saved is None:
                db.execute('INSERT INTO control VALUES (?,?)', ('audit_cursor', str(cursor)))
        results = {}
        for event in events:
            if event['id'] <= cursor:
                continue
            session = event.get('session_id')
            outcome = event.get('outcome')
            if outcome in {'imported', 'manual_saved'} and _identifier(session):
                try:
                    raw = event.get('amount')
                    if not isinstance(raw, (str, int, Decimal)) or isinstance(raw, bool) or len(str(raw)) > 32:
                        raise ValueError('invalid amount')
                    amount = Decimal(str(raw))
                    if not amount.is_finite() or not 0 <= amount <= Decimal('1000000') or amount != amount.quantize(Decimal('.01')):
                        raise ValueError('invalid amount')
                except (ValueError, InvalidOperation):
                    pass
                else:
                    text = (f'Manual charging cost saved: ${amount:.2f} USD for session {session}.'
                            if outcome == 'manual_saved' else
                            f'Session {session}: ${amount:.2f} USD imported and verified.')
                    result = self._emit_locked(f'{outcome}:{session}:{event["id"]}', 'Ghost Ledger', text)
                    results[result['status']] = results.get(result['status'], 0) + 1
                    if result['status'] in {'disabled', 'blocked'}:
                        # API terminal rejections have a durable key; config
                        # failures don't. Do not silently skip an unsent event.
                        with self._db() as db:
                            durable = db.execute('SELECT 1 FROM notifications WHERE key=?',
                                                 (f'{outcome}:{session}:{event["id"]}',)).fetchone()
                        if not durable:
                            return result
            kind = _issue(outcome)
            if kind and _identifier(session):
                result = self._emit_locked(f'issue:{kind}:{session}:{day}',
                                           'Ghost Ledger: attention needed',
                                           f'Session {session}: ' + _ISSUE_TEXT[kind])
                results[result['status']] = results.get(result['status'], 0) + 1
                if result['status'] == 'disabled':
                    return result
            # Progress only after a durable attempt; exceptions (capacity, I/O)
            # leave cursor untouched, so an operator can repair without loss.
            with self._db() as db:
                db.execute('UPDATE control SET value=? WHERE key=?', (str(event['id']), 'audit_cursor'))
        issues = set()
        # Old preview disagreements are not new operational failures.
        for row in [*scheduler.get('jobs', []), *daily.get('jobs', [])]:
            if not isinstance(row, dict):
                continue
            kind = _issue(row.get('state', row.get('status', row.get('outcome'))))
            if kind and _identifier(row.get('session_id')):
                issues.add((kind, row['session_id']))
        for source, label in ((scheduler, 'event'), (daily, 'daily')):
            failure = source.get('status')
            last = source.get('last_run') or {}
            if failure in {'blocked', 'failed', 'needs_review', 'overflow', 'unavailable', 'uncertain'} or last.get('state') in {'failed', 'needs_review', 'overflow', 'uncertain'}:
                issues.add((label + '_failure', 0))
        renewal = passive.get('renewal') or {}
        if renewal.get('status') in {'blocked', 'failed', 'auth_blocked', 'needs_review'}:
            issues.add(('auth_blocked', 0))
        for kind, session in sorted(issues):
            text = _ISSUE_TEXT.get(kind, 'Billing operation requires local review.')
            prefix = f'Session {session}: ' if session else ''
            result = self._emit_locked(f'issue:{kind}:{session}:{day}',
                                       'Ghost Ledger: attention needed', prefix + text)
            results[result['status']] = results.get(result['status'], 0) + 1
        return {'status': 'ok', 'counts': results}

    def snapshot(self):
        with self._db() as db:
            return {'counts': dict(db.execute('SELECT state, count(*) FROM notifications GROUP BY state'))}
