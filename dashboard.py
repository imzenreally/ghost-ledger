"""Private LAN/VPN-only review UI. No scheduler, external fetches or billing writes.

Set GHOST_LEDGER_PASSWORD_HASH (Werkzeug hash) and GHOST_LEDGER_SECRET_KEY
(random >=32 characters). Never put passwords or secrets in URLs. HTTP LAN
sessions do not use Secure cookies; TLS is preferred. Do not expose publicly.
Reverse-proxy headers are intentionally not trusted.
"""
from datetime import timedelta
from collections import Counter, OrderedDict
from threading import Lock
import time
import hmac
import os
import secrets
import re

from flask import Flask, abort, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash


def create_app(store, config=None):
    app = Flask(__name__, template_folder='dashboard_templates', static_folder=None)
    app.config.update(
        SECRET_KEY=os.environ.get('GHOST_LEDGER_SECRET_KEY'),
        PASSWORD_HASH=os.environ.get('GHOST_LEDGER_PASSWORD_HASH'),
        SESSION_COOKIE_NAME='ghost_ledger_session',
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        SESSION_COOKIE_SECURE=False, PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=4096,
        ALLOWED_HOSTS=os.environ.get('GHOST_LEDGER_ALLOWED_HOSTS', 'localhost:8897,127.0.0.1:8897,10.0.0.10:8897').split(','),
    )
    if config:
        app.config.update(config)
    if not app.config['PASSWORD_HASH'] or len(app.config.get('SECRET_KEY') or '') < 32:
        raise ValueError('Dashboard login configuration is required')
    if not re.fullmatch(r'(?:scrypt:[0-9]+:[0-9]+:[0-9]+|pbkdf2:sha(?:256|512):[0-9]+)\$[A-Za-z0-9]+\$[0-9a-f]{64,128}', app.config['PASSWORD_HASH']):
        raise ValueError('A supported Werkzeug password hash is required')
    try:
        check_password_hash(app.config['PASSWORD_HASH'], 'configuration-validation')
    except (ValueError, OverflowError):
        raise ValueError('Invalid password hash configuration') from None
    if store is None:
        from ledger_store import LedgerStore
        store = LedgerStore(os.environ.get('GHOST_LEDGER_STATE', 'state/ledger.sqlite3'))

    def csrf_token():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        return session['csrf_token']

    app.jinja_env.globals['csrf_token'] = csrf_token

    failures = OrderedDict()
    failure_lock = Lock()

    def valid_csrf():
        expected = session.get('csrf_token', '')
        actual = request.form.get('csrf_token', '')
        return bool(expected) and actual.isascii() and hmac.compare_digest(expected, actual)

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        return response

    @app.before_request
    def protect():
        # Compare exact authorities, including ports. Never honor forwarded hosts/IPs.
        if request.host not in app.config['ALLOWED_HOSTS']:
            abort(400)
        origin = request.headers.get('Origin')
        if origin is not None and origin != request.scheme + '://' + request.host:
            abort(403)
        if request.endpoint is None:
            return None
        if request.endpoint not in ('login', 'healthz') and not session.get('authenticated'):
            if request.path.startswith('/api/'):
                return jsonify(error='Authentication required'), 401
            return redirect('/login')

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        error = None
        if request.method == 'POST':
            if not valid_csrf():
                abort(403)
            ip = request.remote_addr or 'unknown'
            now = time.monotonic()
            # One production worker, threads share this bounded map. Restarts reset it.
            with failure_lock:
                for key in list(failures):
                    if now - failures[key][1] >= 300:
                        del failures[key]
                attempts, first = failures.get(ip, (0, now))
                if attempts >= 5:
                    return render_template('login.html', error='Unable to sign in'), 429, {'Retry-After': str(max(1, int(300 - (now - first))))}
                if len(failures) >= 1024 and ip not in failures:
                    failures.popitem(last=False)
                # Reserve a failure before expensive hashing to bound concurrent attempts.
                failures[ip] = (attempts + 1, first)
            if check_password_hash(app.config['PASSWORD_HASH'], request.form.get('password', '')):
                with failure_lock:
                    failures.pop(ip, None)
                session.clear()
                session['authenticated'] = True
                session.permanent = True
                return redirect('/')
            error = 'Unable to sign in'
        return render_template('login.html', error=error), 401 if error else 200

    @app.get('/healthz')
    def healthz():
        return jsonify(status='ok')

    @app.post('/logout')
    def logout():
        if not valid_csrf():
            abort(403)
        session.clear()
        return redirect('/login')

    def page_data():
        def number(name, default, maximum):
            values = request.args.getlist(name)
            if len(values) > 1:
                abort(400)
            value = values[0] if values else str(default)
            if not value.isascii() or not value.isdecimal() or len(value) > 7:
                abort(400)
            value = int(value)
            if not 1 <= value <= maximum:
                abort(400)
            return value
        page, limit = number('page', 1, 1000000), number('limit', 50, 100)
        snapshot = store.snapshot()
        states = ('ready', 'protected', 'conflict', 'pending', 'ambiguous', 'unmatched', 'invalid', 'imported')
        def status(row):
            return row.get('status') if row.get('status') in states else 'invalid'
        counts = Counter(status(row) for row in snapshot['rows'])
        # Only documented presentation fields cross the HTTP boundary. Never evidence,
        # identifiers, arbitrary warnings, source paths, or audit details.
        fields = ('session_id', 'amount', 'currency', 'site', 'started_at', 'ended_at', 'reason', 'current_cost')
        sorted_rows = sorted(snapshot['rows'], key=lambda row: str(row.get('started_at') or ''), reverse=True)
        rows = []
        for raw in sorted_rows[(page - 1) * limit:page * limit]:
            row = {key: str(raw[key])[:512] if raw.get(key) is not None else None for key in fields}
            row['status'] = status(raw)
            rows.append(row)
        return dict(rows=rows, total=len(sorted_rows), counts={state: counts[state] for state in states},
                    page=page, limit=limit, updated_at=str(snapshot.get('updated_at') or '')[:80] or None,
                    source='saved history', warning_count=len(snapshot.get('warnings', [])))

    @app.get('/')
    def index():
        return render_template('index.html', snapshot=page_data())

    @app.get('/api/sessions')
    def sessions():
        return jsonify(page_data())

    return app


def __getattr__(name):
    # Lazy WSGI export: imports in CLI/tests never open state or require secrets.
    # Gunicorn resolves dashboard:app, failing closed before serving if unconfigured.
    if name == 'app':
        return create_app(None)
    raise AttributeError(name)


if __name__ == '__main__':
    import sys
    os.execv(sys.executable, [sys.executable, '-m', 'gunicorn', '--bind',
             '127.0.0.1:8897', '--workers', '1', '--threads', '4',
             '--worker-tmp-dir', os.environ.get('TMPDIR', '/dev/shm'), 'dashboard:app'])
