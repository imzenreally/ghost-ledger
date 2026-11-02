"""Explicit owner-only receipt totals. Disabled unless the dashboard opts in."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hmac
import re
import uuid
import secrets
import time
import os
from pathlib import Path

from ledger import import_lock
from ledger_store import LedgerStore
from manual_reader import private_path

from flask import Blueprint, abort, current_app, render_template, request, session
from owner_database import OwnerDatabase

COLUMNS = ('id', 'car_id', 'start_date', 'end_date', 'charge_energy_used', 'cost', 'geofence_id', 'geofence_name')
SELECT = '''SELECT cp.id, cp.car_id, cp.start_date, cp.end_date,
                   cp.charge_energy_used, cp.cost, cp.geofence_id, g.name
            FROM charging_processes cp JOIN cars c ON c.id=cp.car_id
            LEFT JOIN geofences g ON g.id=cp.geofence_id'''
ELIGIBLE = '''cp.geofence_id IS DISTINCT FROM 1 AND cp.end_date IS NOT NULL
    AND cp.start_date IS NOT NULL AND cp.start_date <= cp.end_date
    AND cp.end_date >= %s AND cp.end_date <= %s'''


def _rows(cursor):
    return [dict(zip(COLUMNS, row)) if not isinstance(row, dict) else dict(row)
            for row in cursor.fetchall()]


def _snapshot(row):
    result = dict(row)
    for key in ('start_date', 'end_date'):
        value = result[key]
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        result[key] = value.astimezone(timezone.utc).isoformat()
    for key in ('charge_energy_used', 'cost'):
        result[key] = None if result[key] is None else str(result[key])
    result['geofence_name'] = str(result['geofence_name'] or 'Unknown geofence')[:160]
    return result


def _read(database, now, sid=None):
    conn = database.connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            params = (now - timedelta(days=30), now)
            where = ELIGIBLE
            if sid is not None:
                where += ' AND cp.id=%s'
                params += (sid,)
            cursor.execute(SELECT + ' WHERE ' + where + ' ORDER BY cp.end_date DESC, cp.id DESC LIMIT 100', params)
            return [_snapshot(row) for row in _rows(cursor)]
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


class ReviewConflict(ValueError):
    """The reviewed session is no longer the exact same eligible target."""


def _same_snapshot(left, right):
    for key in COLUMNS:
        a, b = left.get(key), right.get(key)
        if key in ('cost', 'charge_energy_used') and a is not None and b is not None:
            if Decimal(a) != Decimal(b):
                return False
        elif a != b:
            return False
    return True


def _write(database, review, now):
    expected = review['snapshot']
    amount = Decimal(review['amount'])
    conn = database.connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL SERIALIZABLE')
            cursor.execute(SELECT + ' WHERE ' + ELIGIBLE + ' AND cp.id=%s FOR UPDATE OF cp,c',
                           (now - timedelta(days=30), now, expected['id']))
            rows = _rows(cursor)
            if len(rows) != 1 or not _same_snapshot(_snapshot(rows[0]), expected):
                raise ReviewConflict()
            if expected['cost'] is not None and not review['replace_existing']:
                raise ReviewConflict()
            previous = None if expected['cost'] is None else Decimal(expected['cost'])
            cursor.execute('''UPDATE charging_processes SET cost=%s
                WHERE id=%s AND car_id=%s AND cost IS NOT DISTINCT FROM %s
                AND geofence_id IS DISTINCT FROM 1 RETURNING id''',
                           (amount, expected['id'], expected['car_id'], previous))
            updated = cursor.fetchone()
            if updated is None:
                raise ReviewConflict()
            cursor.execute(SELECT + ' WHERE cp.id=%s', (expected['id'],))
            readback = _rows(cursor)
            target = dict(expected, cost=review['amount'])
            if len(readback) != 1 or not _same_snapshot(_snapshot(readback[0]), target):
                raise RuntimeError('readback failed')
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    # A separate owned connection AFTER commit, not a transaction-local claim.
    fresh = _read(database, now, expected['id'])
    if len(fresh) != 1 or not _same_snapshot(fresh[0], target):
        raise RuntimeError('independent readback failed')


def build_blueprint(database_factory=OwnerDatabase, store=None, *, clock=None):
    blueprint = Blueprint('manual_costs', __name__, template_folder='manual_templates')
    clock = clock or time.time

    @blueprint.record_once
    def require_secret(state):
        _require_secret(state.app)

    def token():
        if 'manual_csrf' not in session:
            session['manual_csrf'] = secrets.token_urlsafe(32)
        return session['manual_csrf']

    @blueprint.get('/manual')
    def index():
        csrf = token()
        try:
            with database_factory() as database:
                rows = _read(database, datetime.fromtimestamp(clock(), timezone.utc).replace(tzinfo=None))
        except Exception:
            return render_template('manual_result.html', message='Unable to read charging sessions.', saved=False), 503
        return render_template('manual_index.html', rows=rows, manual_csrf=csrf)

    def result(message, status=400, saved=False):
        return render_template('manual_result.html', message=message, saved=saved), status

    @blueprint.before_request
    def protect_manual():
        # Defense in depth: parent protects every route; standalone registration
        # also fails closed without an explicit exact Host allowlist.
        if request.host not in current_app.config.get('ALLOWED_HOSTS', ()):
            abort(400)
        origin = request.headers.get('Origin')
        if origin is not None and origin != request.scheme + '://' + request.host:
            abort(403)
        if request.method == 'POST':
            expected = session.get('manual_csrf', '')
            values = request.form.getlist('csrf_token')
            if (len(values) != 1 or not expected or not values[0].isascii()
                    or not hmac.compare_digest(expected, values[0])):
                abort(403)

    @blueprint.post('/manual/preview')
    def preview():
        # A new attempt invalidates any older review, even if validation fails.
        session.pop('manual_review', None)
        if any(len(request.form.getlist(key)) > 1 for key in request.form):
            return result('Use one value per field.')
        sid = request.form.get('session_id', '')
        raw = request.form.get('amount', '')
        provider = request.form.get('provider', '')
        if (not re.fullmatch(r'[1-9][0-9]{0,18}', sid) or int(sid) > 9223372036854775807
                or not re.fullmatch(r'[0-9]{1,5}(?:\.[0-9]{1,2})?', raw)
                or Decimal(raw) > Decimal('10000') or provider not in ('Blink', 'Other')
                or request.form.get('replace_existing', '') not in ('', 'yes')):
            return result('Select a session and provider and enter a USD total from 0 to 10000 with at most two decimal places.')
        try:
            with database_factory() as database:
                rows = _read(database, datetime.fromtimestamp(clock(), timezone.utc).replace(tzinfo=None), int(sid))
        except Exception:
            return result('Unable to read the selected session.', 503)
        if len(rows) != 1:
            return result('That session is not an eligible completed non-home session.', 409)
        row = rows[0]
        replace = request.form.get('replace_existing') == 'yes'
        if row['cost'] is not None and not replace:
            return result('An existing cost, including zero, requires explicit replacement approval.', 409)
        review = dict(snapshot=row, amount=format(Decimal(raw), '.2f'), provider=provider,
                      replace_existing=replace, nonce=secrets.token_urlsafe(32),
                      operation_id='manual:' + uuid.uuid4().hex, issued_at=clock())
        session['manual_review'] = review
        return render_template('manual_review.html', review=review, manual_csrf=token())

    @blueprint.post('/manual/apply')
    def apply():
        review = session.pop('manual_review', None)  # consume before any side effect
        nonces = request.form.getlist('review_nonce')
        if (not review or len(nonces) != 1 or not nonces[0].isascii()
                or not hmac.compare_digest(review['nonce'], nonces[0])
                or not 0 <= clock() - review['issued_at'] <= 300):
            return result('Review missing, expired, or already consumed. Start a new review.', 409)
        operation = review['operation_id']
        sid = review['snapshot']['id']
        amount = review['amount']
        audit_store = None
        pending = False
        try:
            audit_store = store
            if audit_store is None:
                state_path = current_app.config.get('GHOST_LEDGER_STATE') or os.environ.get('GHOST_LEDGER_STATE')
                state_path = state_path or Path.home() / '.local/share/ghost-ledger/ledger.sqlite3'
                audit_store = LedgerStore(state_path)
            with import_lock(audit_store.path):
                if any(event['external_id'] == operation for event in audit_store.events()):
                    return result('This operation was already attempted. Inspect its audit before any new entry.', 409)
                # Durable reservation precedes backup and external mutation. A crash
                # retains pending, so replay of an older signed cookie cannot retry.
                import json
                detail = json.dumps(dict(source='manual', provider=review['provider'],
                                         snapshot=review['snapshot'], replace_existing=review['replace_existing']),
                                    allow_nan=False)
                audit_store.record_event(operation, sid, amount, 'pending', detail)
                pending = True
                with database_factory() as database:
                    path = private_path(audit_store.path.parent / 'backups' / (uuid.uuid4().hex + '.dump'))
                    database.backup(path)
                    path = private_path(path)
                    with path.open('rb') as backup:
                        if backup.read(5) != b'PGDMP' or path.stat().st_size <= 5:
                            raise RuntimeError('backup invalid')
                    if not 0 <= clock() - review['issued_at'] <= 300:
                        raise ReviewConflict()
                    now = datetime.fromtimestamp(clock(), timezone.utc).replace(tzinfo=None)
                    _write(database, review, now)
                audit_store.record_event(operation, sid, amount, 'manual_saved', detail)
            return result(f'Saved USD {amount} to session #{sid}. Verified using an independent database read. Source: manual {review["provider"]} receipt total.', 200, True)
        except ReviewConflict:
            if pending and audit_store is not None:
                try:
                    audit_store.record_event(operation, sid, amount, 'protected', 'Reviewed session changed or expired; manual write refused')
                except Exception:
                    return result('Write refused; audit finalization failed. Inspect the durable pending audit before another entry.', 503)
            return result('Review expired or session identity, location, completion, or existing cost changed. No manual cost saved. Start a new review.', 409)
        except Exception:
            if pending and audit_store is not None:
                try:
                    audit_store.record_event(operation, sid, amount, 'uncertain', 'Manual operation not confirmed; inspect private audit and database, no automatic retry')
                except Exception:
                    pass  # the durable pending reservation still prevents replay
            return result('Save not confirmed. Inspect the private audit and database before another entry; do not retry automatically.', 503)

    return blueprint


def _require_secret(app):
    secret = app.config.get('SECRET_KEY')
    if not isinstance(secret, (str, bytes)) or len(secret) < 32:
        raise ValueError('Manual costs require a real SECRET_KEY of at least 32 characters')


def register_manual_costs(app, database_factory=OwnerDatabase, store=None, *, clock=None):
    """Register only when MANUAL_COSTS_ENABLED is True (default False).

    The dashboard owns environment parsing, login policy, Host allowlisting,
    cookie security, and global headers. Inject the SAME LedgerStore as the
    dashboard/CLI to share its audit and import lock. clock returns epoch seconds.
    No database or local state is opened by registration.
    """
    if not app.config.get('MANUAL_COSTS_ENABLED', False):
        return None
    _require_secret(app)
    blueprint = build_blueprint(database_factory, store, clock=clock)
    app.register_blueprint(blueprint)
    return blueprint
