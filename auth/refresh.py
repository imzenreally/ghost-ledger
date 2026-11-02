"""Own-token renewal; no vehicle/billing calls and no implicit request retries.

An interrupted/failed attempt is not retried for the same refresh token, even if
its access token remains valid. A new sign-in (different token fingerprint) or
explicit operator recovery is required. Never remove pending state to retry an
ambiguous network outcome.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import stat
import time
import unicodedata
from urllib.parse import urlencode
from .runtime import (_atomic_json, _private_dir, save_tokens,
                      tesla_transport, TOKEN_URL)

_REASONS = frozenset(('recovery_required', 'sign_in_required', 'refresh_rejected',
                      'refresh_failed', 'invalid_response', 'token_save_failed'))


def _text(value, limit=16384):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and not any(unicodedata.category(c) in ('Cc', 'Cs') for c in value))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _read_private(path):
    """Runtime's private-reader contract, plus strict JSON and nonblocking open.

    Keep this local because changing the shared runtime reader is outside this
    module's scope. Expiry is validated by the caller, not by load_tokens.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise ValueError('unsafe private state')
        data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError('private state too large')
    return json.loads(data, object_pairs_hook=_unique)


def _blocked(reason):
    return {'status': 'blocked', 'refreshed': False, 'reason': reason}


def ensure_fresh(root, client_id, *, transport=tesla_transport, clock=time.time, margin=300):
    """Return a safe status dict, never tokens or provider error text.

    Returns status='fresh'/'refreshed'/'blocked' and a refreshed boolean.
    Successful results include expires_at; blocked results contain only a safe
    reason code. Uses only tokens.json in the private Ghost Ledger state root.
    Expired access tokens are intentionally accepted as input for refresh.
    Every transport attempt is preceded by a durable pending marker under a
    process flock.
    """
    try:
        if not _text(client_id, 1024) or client_id.strip() != client_id:
            return _blocked('invalid_configuration')
        if not _number(margin) or not 0 <= margin <= 86400:
            return _blocked('invalid_configuration')
        with rotation_lock(root) as root:
            return _ensure_locked(root, client_id, transport, clock, margin)
    except Exception:
        return _blocked('unsafe_or_invalid_state')


@contextmanager
def rotation_lock(root):
    """Serialize token writers across processes; yield the validated private root.

    Login callers MUST wrap save_tokens in this same lock. It is not reentrant;
    do not call ensure_fresh while holding it. Stop the renewal service during
    sign-in until the login writer has been integrated with this helper.
    Raises on unsafe paths; unlike ensure_fresh, this is a low-level primitive.
    """
    root = _private_dir(root)
    fd = os.open(root / 'token-rotation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise ValueError('unsafe rotation lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield root
    finally:
        os.close(fd)


def _ensure_locked(root, client_id, transport, clock, margin):
    tokens = _read_private(root / 'tokens.json')  # load_tokens rejects expired tokens.
    now = clock()
    if (not _number(now) or not isinstance(tokens, dict)
            or not _number(tokens.get('expires_at'))
            or not _text(tokens.get('access_token')) or not _text(tokens.get('refresh_token'))):
        return _blocked('sign_in_required')
    fingerprint = hashlib.sha256(tokens['refresh_token'].encode()).hexdigest()
    try:
        state = _read_private(root / 'refresh-state.json')
    except FileNotFoundError:
        state = None
    else:
        if (not isinstance(state, dict) or state.get('status') not in ('pending', 'failed', 'resolved')
                or not isinstance(state.get('fingerprint'), str)
                or len(state['fingerprint']) != 64
                or any(c not in '0123456789abcdef' for c in state['fingerprint'])
                or ('reason' in state and state['reason'] not in _REASONS)):
            return _blocked('unsafe_or_invalid_state')
    if state and state['status'] in ('pending', 'failed'):
        if state['fingerprint'] == fingerprint:
            return _blocked(state.get('reason', 'recovery_required'))
        # Rotation reached tokens.json before a crash, or a new sign-in occurred.
        _atomic_json(root, 'refresh-state.json', {'status': 'resolved', 'fingerprint': state['fingerprint']})
    if now < tokens['expires_at'] - margin:
        return {'status': 'fresh', 'refreshed': False, 'expires_at': tokens['expires_at']}
    _atomic_json(root, 'refresh-state.json', {'status': 'pending', 'fingerprint': fingerprint})
    reason = 'refresh_failed'
    try:
        status, body = transport(TOKEN_URL, urlencode({'grant_type': 'refresh_token',
                                 'client_id': client_id, 'refresh_token': tokens['refresh_token']}).encode(),
                                 {'Content-Type': 'application/x-www-form-urlencoded'}, 20)
        if type(status) is not int or status != 200:
            reason = 'sign_in_required' if type(status) is int and status == 401 else 'refresh_rejected'
            raise ValueError
        reason = 'invalid_response'
        if not isinstance(body, bytes) or len(body) > 65536:
            raise ValueError
        response = json.loads(body, object_pairs_hook=_unique)
        if (not isinstance(response, dict) or not isinstance(response.get('token_type'), str)
                or response['token_type'].lower() != 'bearer'
                or not _text(response.get('access_token')) or not _text(response.get('refresh_token'))
                or response.get('refresh_token') == tokens['refresh_token']
                or not _number(response.get('expires_in')) or not 0 < response['expires_in'] <= 86400
                or not _number(now + response['expires_in'])):
            raise ValueError
        reason = 'token_save_failed'
        save_tokens(root, response, clock=lambda: now)
        _atomic_json(root, 'refresh-state.json', {'status': 'resolved', 'fingerprint': fingerprint})
        return {'status': 'refreshed', 'refreshed': True, 'expires_at': now + response['expires_in']}
    except Exception:
        try:
            _atomic_json(root, 'refresh-state.json', {'status': 'failed', 'fingerprint': fingerprint,
                                                    'reason': reason})
        except Exception:
            pass  # The durable pending reservation still prevents a blind retry.
        return _blocked(reason)
