"""Explicit, single-page manual Fleet reader; independent of lifetime probes.

No refresh, retry, pagination, scheduling, or HA credentials. Failed attempts consume
UTC daily/monthly allowances durably before transport. Never log raw responses.
"""
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import stat
import time

from auth import runtime


class ManualReaderError(ValueError):
    """Sanitized manual reader failure."""


def private_path(path, *, create=False):
    path = Path(path).expanduser().absolute()
    runtime._private_dir(path.parent)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ManualReaderError('symlink state path rejected')
    if create:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    try:
        info = path.lstat()
    except FileNotFoundError:
        # SQLite may remove its rollback journal between checks. Missing optional
        # paths are allowed; actual readers/openers still use O_NOFOLLOW.
        return path
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
        raise ManualReaderError('state file must be private, owned, and regular')
    return path


def _records(raw):
    payload = raw.get('response', raw) if isinstance(raw, dict) else raw
    if isinstance(payload, dict):
        payload = payload.get('data')
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ManualReaderError('unexpected billing response; no retry')
    return payload


def _decode(body):
    if not isinstance(body, bytes) or len(body) > runtime.MAX_RESPONSE:
        raise ManualReaderError('unexpected billing response; no retry')
    try:
        def reject_constant(value):
            raise ValueError('nonfinite JSON')
        raw = json.loads(body, parse_float=Decimal, parse_constant=reject_constant)
        _records(raw)
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ManualReaderError('unexpected billing response; no retry') from None


def read_history(path):
    path = private_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        return _decode(source.read(runtime.MAX_RESPONSE + 1))


def reserve_manual(root, *, clock=time.time):
    path = private_path(Path(root) / 'manual-budget.sqlite3', create=True)
    for suffix in ('-journal', '-wal', '-shm'):
        private_path(Path(str(path) + suffix))
    now = datetime.fromtimestamp(clock(), timezone.utc)
    day, month = now.strftime('%Y-%m-%d'), now.strftime('%Y-%m')
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS allowance (kind TEXT, period TEXT, used INTEGER NOT NULL, PRIMARY KEY(kind, period))')
        db.execute('BEGIN IMMEDIATE')
        for kind, period, limit in (('day', day, 4), ('month', month, 100)):
            used = db.execute('SELECT used FROM allowance WHERE kind=? AND period=?', (kind, period)).fetchone()
            if used and used[0] >= limit:
                raise runtime.BudgetExceeded('manual UTC daily/monthly allowance exhausted; no request sent')
        for kind, period in (('day', day), ('month', month)):
            db.execute('INSERT INTO allowance VALUES (?,?,1) ON CONFLICT(kind,period) DO UPDATE SET used=used+1', (kind, period))
        db.commit()
    finally:
        db.close()


def fetch_latest(history_path, *, token_root=None, transport=runtime.tesla_transport, clock=time.time):
    target = private_path(history_path)
    root = target.parent if token_root is None else Path(token_root)
    # Expiration/sign-in rejection is deliberately before allowance reservation.
    try:
        token = runtime.load_tokens(root, clock=clock)['access_token']
    except ValueError:
        raise ManualReaderError('valid Ghost Ledger sign-in required; no automatic renewal or request') from None
    reserve_manual(root, clock=clock)
    try:
        status, body = transport(runtime.HISTORY_URL, None, {'Authorization': 'Bearer ' + token}, 20)
    except Exception:
        raise ManualReaderError('billing transport failed; allowance consumed; no retry') from None
    if type(status) is not int or status != 200:
        raise ManualReaderError('billing HTTP request rejected; allowance consumed; no retry')
    raw = _decode(body)
    encoded = json.dumps(raw, default=str, allow_nan=False, separators=(',', ':')).encode()
    runtime._atomic_bytes(target.parent, target.name, encoded)
    return {'http_status': 200, 'returned_sessions': len(_records(raw)), 'pagination_followed': False}
