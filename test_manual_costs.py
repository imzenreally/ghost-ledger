"""Manual-entry tests use only synthetic databases; never OwnerDatabase."""
import importlib
import unittest
from flask import Flask


class RegistrationTests(unittest.TestCase):
    def test_disabled_by_default_and_enabled_requires_secret_even_without_login(self):
        module = importlib.import_module('manual_costs')
        app = Flask(__name__)
        module.register_manual_costs(app)
        self.assertEqual(app.test_client().get('/manual').status_code, 404)
        app.config.update(MANUAL_COSTS_ENABLED=True, AUTH_REQUIRED=False)
        with self.assertRaises(ValueError):
            module.register_manual_costs(app)


    def test_direct_blueprint_registration_cannot_bypass_secret_check(self):
        from manual_costs import build_blueprint
        app = Flask(__name__)
        with self.assertRaises(ValueError):
            app.register_blueprint(build_blueprint())


# Opt-in DSN MUST target a disposable synthetic loopback database, never TeslaMate.
import os
import tempfile
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlparse
from flask import abort, request
from ledger_store import LedgerStore
import psycopg


class ReadOnlyAdapterTests(unittest.TestCase):
    def test_readonly_http_fake_cursor_and_parameterized_exact_lookup(self):
        from manual_costs import register_manual_costs
        commands = []
        now = datetime(2026, 6, 1, 12)
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql, params=()):
                commands.append((sql, params))
                if not (sql.startswith('SELECT ') or sql == 'SET TRANSACTION READ ONLY'):
                    raise AssertionError('No mutation allowed')
            def fetchall(self):
                return [(21, 3, now - timedelta(hours=1), now, Decimal('7.5'), None, None, None)]
        class Connection:
            def cursor(self): return Cursor()
            def rollback(self): pass
            def close(self): pass
        class Database:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self): return Connection()
        app = Flask(__name__, template_folder='dashboard_templates')
        app.config.update(SECRET_KEY='synthetic-unit-test-only-' * 2,
                          MANUAL_COSTS_ENABLED=True, ALLOWED_HOSTS=['localhost'])
        register_manual_costs(app, Database, clock=lambda: now.replace(tzinfo=timezone.utc).timestamp())
        client = app.test_client()
        self.assertEqual(client.get('/manual').status_code, 200)
        with client.session_transaction() as state:
            csrf = state['manual_csrf']
        response = client.post('/manual/preview', data=dict(csrf_token=csrf, session_id='21', amount='2.50', provider='Other'))
        self.assertEqual(response.status_code, 200)
        lookup, params = commands[-1]
        self.assertIn('cp.id=%s', lookup)
        self.assertEqual(params[-1], 21)
        self.assertNotIn('21', lookup)
        self.assertIn('Unknown geofence', response.get_data(as_text=True))

    def test_read_connection_closes_even_if_rollback_fails(self):
        from manual_costs import _read
        from unittest.mock import MagicMock
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        conn.rollback.side_effect = RuntimeError('rollback failed')
        database = MagicMock()
        database.connect.return_value = conn
        with self.assertRaises(RuntimeError):
            _read(database, datetime(2026, 6, 1))
        conn.close.assert_called_once()


@unittest.skipUnless(os.environ.get('GHOST_LEDGER_TEST_POSTGRES_DSN'), 'isolated PostgreSQL DSN not set')
class PostgreSQLHTTPTests(unittest.TestCase):
    def setUp(self):
        from manual_costs import register_manual_costs
        self.dsn = os.environ['GHOST_LEDGER_TEST_POSTGRES_DSN']
        parsed = urlparse(self.dsn)
        if parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.path not in ('/manual_costs_test', '/synthetic'):
            raise ValueError('Only the synthetic loopback manual_costs_test database is allowed')
        self.schema = 'manual_test_' + uuid.uuid4().hex
        self.now = datetime(2026, 6, 1, 12)
        self.clock_value = self.now.replace(tzinfo=timezone.utc).timestamp()
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA {self.schema}')
            conn.execute(f'SET search_path TO {self.schema}')
            conn.execute('CREATE TABLE cars (id integer PRIMARY KEY, vin text)')
            conn.execute('CREATE TABLE geofences (id integer PRIMARY KEY, name text)')
            conn.execute('''CREATE TABLE charging_processes (id integer PRIMARY KEY,
                car_id integer REFERENCES cars(id), start_date timestamp, end_date timestamp,
                charge_energy_used numeric, cost numeric, geofence_id integer)''')
            conn.execute("INSERT INTO cars VALUES (7, 'NEVER-EXPOSE-THIS-VIN')")
            conn.execute("INSERT INTO geofences VALUES (1, 'Home'), (2, '<Blink & work>')")
            for sid, geo, cost, end in [(10, 2, None, self.now), (11, None, Decimal('0'), self.now),
                                        (12, 1, None, self.now), (13, 2, None, None),
                                        (14, 2, None, self.now - timedelta(days=31))]:
                conn.execute('INSERT INTO charging_processes VALUES (%s,7,%s,%s,12.345,%s,%s)',
                             (sid, self.now - timedelta(days=32) if sid == 14 else self.now - timedelta(hours=1), end, cost, geo))
        self.addCleanup(self.drop_schema)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = LedgerStore(self.root / 'ledger.sqlite3')
        self.backups = []
        self.fail_backup = False
        outer = self
        class Database:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self):
                return psycopg.connect(outer.dsn, options=f'-c search_path={outer.schema}')
            def backup(self, path):
                outer.backups.append(path)
                if outer.fail_backup:
                    raise RuntimeError('SECRET-DB-ERROR')
                # Deliberately a backup stub, not claimed as a real pg_dump.
                with path.open('xb') as target:
                    target.write(b'PGDMPsynthetic-backup-stub')
                path.chmod(0o600)
                return path
        self.factory = Database
        self.app = Flask(__name__, template_folder='dashboard_templates')
        self.app.config.update(TESTING=True, SECRET_KEY='synthetic-test-only-' * 3,
                               MANUAL_COSTS_ENABLED=True, ALLOWED_HOSTS=['localhost'], AUTH_REQUIRED=False)
        self.app.jinja_env.globals['csrf_token'] = lambda: ''
        @self.app.before_request
        def protect():
            if request.host != 'localhost': abort(400)
            if request.headers.get('Origin', 'http://localhost') != 'http://localhost': abort(403)
        register_manual_costs(self.app, self.factory, self.store, clock=lambda: self.clock_value)
        self.client = self.app.test_client()

    def drop_schema(self):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def query(self, sql, params=()):
        with self.factory().connect() as conn:
            cursor = conn.execute(sql, params)
            return cursor.fetchall() if cursor.description else []

    def csrf(self):
        self.client.get('/manual')
        with self.client.session_transaction() as state:
            return state['manual_csrf']

    def preview(self, sid='10', amount='6.70', **extra):
        return self.client.post('/manual/preview', data=dict(
            csrf_token=self.csrf(), session_id=sid, amount=amount, provider='Blink', **extra))

    def apply(self, **overrides):
        with self.client.session_transaction() as state:
            review = state.get('manual_review', {})
            data = dict(csrf_token=state.get('manual_csrf', ''), review_nonce=review.get('nonce', ''))
        data.update(overrides)
        return self.client.post('/manual/apply', data=data)

    def test_apply_exact_cost_backup_audit_independent_readback_and_cookie_replay(self):
        self.assertEqual(self.preview().status_code, 200)
        cookie = self.client.get_cookie('session').value
        with self.client.session_transaction() as state:
            review = dict(state['manual_review'])
        response = self.apply(amount='9999', session_id='12', provider='Other')
        self.assertEqual(response.status_code, 200)
        self.assertIn('6.70', response.get_data(as_text=True))
        self.assertEqual(self.query('SELECT id,cost FROM charging_processes ORDER BY id'),
                         [(10, Decimal('6.70')), (11, Decimal('0')), (12, None), (13, None), (14, None)])
        self.assertEqual(len(self.backups), 1)
        events = self.store.events()
        self.assertEqual([e['outcome'] for e in events], ['pending', 'manual_saved'])
        self.assertEqual(events[-1]['external_id'], review['operation_id'])
        self.assertIn('Blink', events[-1]['detail'])
        self.assertEqual(events[-1]['amount'], '6.70')
        self.assertEqual(self.store.snapshot()['rows'], [])
        self.assertEqual(self.apply().status_code, 409)
        self.client.set_cookie('session', cookie)
        self.assertEqual(self.apply().status_code, 409)
        self.assertEqual(len(self.backups), 1)
        self.assertEqual(len(self.store.events()), 2)

    def test_apply_zero_replacement_only_after_review(self):
        self.assertEqual(self.preview(sid='11', replace_existing='yes', amount='0.00').status_code, 200)
        self.assertEqual(self.apply().status_code, 200)
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=11'), [(Decimal('0.00'),)])

    def test_apply_rejects_expired_or_lost_or_wrong_nonce_and_csrf(self):
        self.preview()
        self.assertEqual(self.apply(csrf_token='invalid').status_code, 403)
        self.assertEqual(self.apply(review_nonce='invalid').status_code, 409)
        self.assertEqual(self.apply().status_code, 409)
        self.preview()
        self.clock_value += 301
        self.assertEqual(self.apply().status_code, 409)
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.backups, [])

    def test_real_dashboard_registration_no_login_csrf_and_headers(self):
        from unittest.mock import patch
        from dashboard import create_app
        import manual_costs
        register = manual_costs.register_manual_costs
        def injected(app, store=None):
            return register(app, self.factory, store, clock=lambda: self.clock_value)
        with patch.object(manual_costs, 'register_manual_costs', injected):
            app = create_app(self.store, dict(TESTING=True, AUTH_REQUIRED=False,
                SECRET_KEY='synthetic-integration-test-' * 2, ALLOWED_HOSTS=['localhost'],
                MANUAL_COSTS_ENABLED=True))
        client = app.test_client()
        response = client.get('/manual')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn("form-action 'self'", response.headers['Content-Security-Policy'])
        self.assertEqual(client.post('/manual/preview').status_code, 403)
        with client.session_transaction() as state:
            csrf = state['manual_csrf']
        self.assertEqual(client.post('/manual/preview', data=dict(csrf_token=csrf,
            session_id='10', amount='4.20', provider='Other')).status_code, 200)
        with client.session_transaction() as state:
            nonce = state['manual_review']['nonce']
        self.assertEqual(client.post('/manual/apply', data=dict(csrf_token=csrf, review_nonce=nonce),
                                    headers={'Origin': 'http://localhost'}).status_code, 200)
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(Decimal('4.20'),)])
        self.assertEqual(client.get('/login').status_code, 302)

    def test_conflict_audit_failure_is_sanitized_and_pending_stays_reserved(self):
        from unittest.mock import patch
        self.preview()
        self.query('UPDATE charging_processes SET cost=1 WHERE id=10 RETURNING id')
        original = self.store.record_event
        def record(*args, **kwargs):
            if args[3] == 'protected':
                raise RuntimeError('SECRET-DB-ERROR')
            return original(*args, **kwargs)
        with patch.object(self.store, 'record_event', record):
            response = self.apply()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('SECRET-DB-ERROR', response.get_data(as_text=True))
        self.assertEqual(self.store.events()[-1]['outcome'], 'pending')
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(Decimal('1'),)])

    def test_review_that_expires_during_backup_is_not_written(self):
        from unittest.mock import patch
        self.preview()
        original = self.factory.backup
        def slow_backup(database, path):
            value = original(database, path)
            self.clock_value += 301
            return value
        with patch.object(self.factory, 'backup', slow_backup):
            self.assertEqual(self.apply().status_code, 409)
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(None,)])

    def test_apply_revalidates_all_identity_fields_and_existing_cost(self):
        changes = [('geofence_id', 1), ('geofence_id', None), ('cost', Decimal('0')),
                   ('charge_energy_used', Decimal('13')), ('start_date', self.now - timedelta(hours=2)),
                   ('end_date', None), ('car_id', 8)]
        self.query("INSERT INTO cars VALUES (8, 'ALSO-PRIVATE') RETURNING id")
        for field, value in changes:
            with self.subTest(field=field, value=value):
                self.query('UPDATE charging_processes SET car_id=7,geofence_id=2,cost=NULL,charge_energy_used=12.345,start_date=%s,end_date=%s WHERE id=10 RETURNING id',
                           (self.now - timedelta(hours=1), self.now))
                self.preview(replace_existing='yes')
                self.query(f'UPDATE charging_processes SET {field}=%s WHERE id=10 RETURNING id', (value,))
                self.assertEqual(self.apply().status_code, 409)
                self.assertEqual(self.store.events()[-1]['outcome'], 'protected')
                expected_cost = value if field == 'cost' else None
                self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(expected_cost,)])

    def test_backup_failure_never_writes_or_leaks_errors_and_cookie_cannot_retry(self):
        self.preview()
        cookie = self.client.get_cookie('session').value
        self.fail_backup = True
        response = self.apply()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('SECRET-DB-ERROR', response.get_data(as_text=True))
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(None,)])
        self.assertEqual([e['outcome'] for e in self.store.events()], ['pending', 'uncertain'])
        self.client.set_cookie('session', cookie)
        self.assertEqual(self.apply().status_code, 409)

    def test_corrupt_backup_is_rejected_before_write(self):
        from unittest.mock import patch
        self.preview()
        def bad_backup(database, path):
            path.write_bytes(b'not-a-dump')
            path.chmod(0o600)
        with patch.object(self.factory, 'backup', bad_backup):
            self.assertEqual(self.apply().status_code, 503)
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(None,)])

    def test_transaction_readback_failure_rolls_back_and_reports_uncertain(self):
        self.query('''CREATE FUNCTION sabotage_cost() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN NEW.cost=NEW.cost+1; RETURN NEW; END $$''')
        self.query('''CREATE TRIGGER sabotage BEFORE UPDATE ON charging_processes
            FOR EACH ROW EXECUTE FUNCTION sabotage_cost()''')
        self.preview()
        self.assertEqual(self.apply().status_code, 503)
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(None,)])
        self.assertEqual(self.store.events()[-1]['outcome'], 'uncertain')

    def test_independent_readback_failure_never_claims_saved_or_retries(self):
        from unittest.mock import patch
        import manual_costs
        self.preview()
        with patch.object(manual_costs, '_read', side_effect=RuntimeError('SECRET-DB-ERROR')):
            response = self.apply()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('SECRET-DB-ERROR', response.get_data(as_text=True))
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(Decimal('6.70'),)])
        self.assertEqual([e['outcome'] for e in self.store.events()], ['pending', 'uncertain'])

    def test_crash_pending_is_durable_and_shared_cli_lock_prevents_write(self):
        from ledger import import_lock
        self.preview()
        with self.client.session_transaction() as state:
            review = state['manual_review']
        self.store.record_event(review['operation_id'], 10, '6.70', 'pending', 'synthetic crash')
        self.assertEqual(self.apply().status_code, 409)
        self.assertEqual(self.backups, [])
        self.preview()
        with import_lock(self.store.path):
            self.assertEqual(self.apply().status_code, 503)
        self.assertEqual(self.backups, [])
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(None,)])

    def test_list_limit_and_duplicate_form_fields(self):
        from werkzeug.datastructures import MultiDict
        with self.factory().connect() as conn:
            conn.execute('''INSERT INTO charging_processes
                SELECT n,7,%s,%s,1,NULL,NULL FROM generate_series(100,209) n''',
                         (self.now - timedelta(hours=1), self.now))
        text = self.client.get('/manual').get_data(as_text=True)
        import re
        self.assertEqual(len(re.findall(r'<option value="[0-9]+"', text)), 100)
        response = self.client.post('/manual/preview', data=MultiDict([
            ('csrf_token', self.csrf()), ('session_id', '10'), ('session_id', '12'),
            ('amount', '1.00'), ('provider', 'Blink')]))
        self.assertEqual(response.status_code, 400)

    def test_preview_is_authoritative_read_only_and_requires_explicit_zero_replacement(self):
        response = self.preview()
        self.assertEqual(response.status_code, 200)
        self.assertIn('Save to TeslaMate', response.get_data(as_text=True))
        with self.client.session_transaction() as state:
            review = state['manual_review']
        self.assertEqual(review['snapshot']['id'], 10)
        self.assertEqual(review['amount'], '6.70')
        self.assertEqual(review['provider'], 'Blink')
        self.assertEqual(review['snapshot']['cost'], None)
        self.assertTrue(review['nonce'])
        self.assertTrue(review['operation_id'].startswith('manual:'))
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.backups, [])
        self.assertEqual(self.preview(sid='11').status_code, 409)
        self.assertEqual(self.preview(sid='11', replace_existing='yes').status_code, 200)
        for sid in ('12', '13', '14', '999', '10 OR 1=1', '0', '-1'):
            with self.subTest(sid=sid):
                self.assertIn(self.preview(sid=sid).status_code, (400, 409))
        for amount in ('NaN', 'Infinity', '-1', '10000.01', '1e2', '1.001', '.2', 'true', ' 1', '1.', '', '１２'):
            with self.subTest(amount=amount):
                self.assertEqual(self.preview(amount=amount).status_code, 400)
        for amount in ('0', '0.00', '10000', '3.1'):
            self.assertEqual(self.preview(amount=amount).status_code, 200)
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(None,)])

    def test_preview_csrf_origin_and_host_are_required_without_login(self):
        self.csrf()
        for token in ('', 'bad', '非ascii'):
            self.assertEqual(self.client.post('/manual/preview', data=dict(csrf_token=token)).status_code, 403)
        self.assertEqual(self.client.post('/manual/preview', data={'csrf_token': self.csrf()},
                                         headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/manual', headers={'Host': 'evil.example'}).status_code, 400)
        self.assertEqual(self.store.events(), [])

    def test_list_is_read_only_bounded_nonhome_completed_recent_and_escaped(self):
        response = self.client.get('/manual')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('UTC', text)
        self.assertIn('&lt;Blink &amp; work&gt;', text)
        self.assertIn('value="10"', text)
        self.assertIn('value="11"', text)
        for sid in (12, 13, 14):
            self.assertNotIn(f'value="{sid}"', text)
        self.assertNotIn('NEVER-EXPOSE', text)
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.backups, [])
        self.assertEqual(self.query('SELECT cost FROM charging_processes WHERE id=10'), [(None,)])


if __name__ == '__main__':
    unittest.main()
