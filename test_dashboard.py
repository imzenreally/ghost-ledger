import re
import secrets
import tempfile
import unittest
from pathlib import Path
from werkzeug.security import generate_password_hash


class DashboardTests(unittest.TestCase):
    def setUp(self):
        from ledger_store import LedgerStore
        from dashboard import create_app
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = LedgerStore(Path(self.tmp.name).resolve() / 'state' / 'ledger.sqlite3')
        self.password = secrets.token_urlsafe(24)
        self.config = {'TESTING': True, 'SECRET_KEY': secrets.token_hex(32),
                       'PASSWORD_HASH': generate_password_hash(self.password),
                       'ALLOWED_HOSTS': ['localhost', 'localhost:8897', '127.0.0.1:8897']}
        self.app = create_app(self.store, self.config)
        self.client = self.app.test_client()

    def csrf(self, html):
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def login(self):
        token = self.csrf(self.client.get('/login').text)
        return self.client.post('/login', data={'csrf_token': token, 'password': self.password})

    def test_private_login_and_read_only_review(self):
        self.assertEqual(self.client.get('/').status_code, 302)
        self.assertEqual(self.client.get('/api/sessions').status_code, 401)
        self.assertEqual(self.client.get('/healthz').json, {'status': 'ok'})
        self.assertEqual(self.login().status_code, 302)
        html = self.client.get('/').text
        self.assertIn('Ghost Ledger', html)
        self.assertIn('10¢/kWh', html)
        self.assertIn('Read-only dashboard', html)
        self.assertNotIn('Enter charging receipt', html)
        self.assertIn('No saved snapshot', html)
        self.assertEqual(self.client.get('/api/sessions').json['total'], 0)
        self.assertEqual(self.client.post('/api/sessions').status_code, 405)

    def test_sessions_are_projected_escaped_and_paginated(self):
        self.store.publish([{'external_id': f'fixture-{i:03}', 'status': 'conflict',
            'site': '<script>alert(1)</script>', 'amount': '8.40', 'currency': 'USD',
            'started_at': '2026-10-31T12:00:00Z', 'current_cost': '7.00',
            'reason': 'Amount differs', 'evidence': {'token': 'TOPSECRET'},
            'vin': 'VINSECRET', 'raw': 'RAWSECRET'} for i in range(120)])
        self.login()
        response = self.client.get('/api/sessions')
        self.assertEqual(len(response.json['rows']), 50)
        self.assertEqual(response.json['total'], 120)
        self.assertEqual(response.json['counts']['conflict'], 120)
        self.assertNotIn('TOPSECRET', response.text)
        self.assertNotIn('external_id', response.text)
        self.assertNotIn('VINSECRET', response.text)
        html = self.client.get('/').text
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertIn('Current cost', html)
        self.assertIn('7.00', html)
        self.assertIn('Amount differs', html)
        self.assertEqual(html.count('class="session-row conflict"'), 50)
        self.assertEqual(len(self.client.get('/api/sessions?limit=100&page=2').json['rows']), 20)
        for query in ('limit=101', 'limit=0', 'page=-1', 'page=nope', 'limit=1&limit=2'):
            self.assertEqual(self.client.get('/api/sessions?' + query).status_code, 400)
        self.assertEqual(self.client.get('/api/events').status_code, 404)
        self.assertEqual(self.client.get('/../../ledger.sqlite3').status_code, 404)

    def test_host_origin_csrf_logout_and_headers(self):
        self.assertEqual(self.client.get('/healthz', headers={'Host': 'evil.example'}).status_code, 400)
        self.assertEqual(self.client.get('/healthz', headers={'Origin': 'http://evil.example'}).status_code, 403)
        self.assertEqual(self.client.get('/healthz', headers={'Origin': 'null'}).status_code, 403)
        self.assertEqual(self.client.get('/healthz', headers={'X-Forwarded-Host': 'evil.example'}).status_code, 200)
        response = self.client.get('/login')
        self.assertIn('HttpOnly', response.headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', response.headers['Set-Cookie'])
        self.assertNotIn('Secure;', response.headers['Set-Cookie'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.client.post('/login', data={'password': self.password}).status_code, 403)
        self.login()
        self.assertEqual(self.client.post('/logout').status_code, 403)
        token = self.csrf(self.client.get('/').text)
        self.assertEqual(self.client.post('/logout', data={'csrf_token': token}).status_code, 302)
        self.assertEqual(self.client.get('/api/sessions').status_code, 401)
        self.assertEqual(self.client.get('/logout').status_code, 405)

    def test_login_rate_limit_and_no_password_echo(self):
        token = self.csrf(self.client.get('/login').text)
        for _ in range(5):
            response = self.client.post('/login', data={'csrf_token': token, 'password': self.password + '-wrong'})
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(self.password, response.text)
        response = self.client.post('/login', data={'csrf_token': token, 'password': self.password})
        self.assertEqual(response.status_code, 429)
        self.assertIn('Retry-After', response.headers)

    def test_real_local_http_login_and_review(self):
        import http.cookiejar
        import threading
        import urllib.request
        import urllib.error
        from werkzeug.serving import make_server, WSGIRequestHandler
        class QuietHandler(WSGIRequestHandler):
            def log_request(self, *args, **kwargs):
                pass
        server = make_server('127.0.0.1', 0, self.app, threaded=True, request_handler=QuietHandler)
        self.app.config['ALLOWED_HOSTS'].append(f'127.0.0.1:{server.server_port}')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(base + '/api/sessions')
            self.assertEqual(caught.exception.code, 401)
            token = self.csrf(opener.open(base + '/login').read().decode())
            import urllib.parse
            data = urllib.parse.urlencode({'csrf_token': token, 'password': self.password}).encode()
            response = opener.open(base + '/login', data=data)
            self.assertIn(b'Ghost Ledger', response.read())
            self.assertEqual(opener.open(base + '/api/sessions').status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_production_wsgi_entrypoint_over_real_http(self):
        import os
        import socket
        import subprocess
        import sys
        import time
        import urllib.request
        import urllib.error
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, GHOST_LEDGER_STATE=str(Path(self.tmp.name).resolve() / 'production' / 'ledger.sqlite3'),
                   GHOST_LEDGER_PASSWORD_HASH=self.config['PASSWORD_HASH'],
                   GHOST_LEDGER_SECRET_KEY=self.config['SECRET_KEY'],
                   GHOST_LEDGER_ALLOWED_HOSTS=f'127.0.0.1:{port}')
        process = subprocess.Popen([sys.executable, '-m', 'gunicorn', '--bind', f'127.0.0.1:{port}',
                                    '--workers', '1', '--threads', '4', '--worker-tmp-dir', self.tmp.name,
                                    'dashboard:app'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            base = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 10
            while True:
                try:
                    with urllib.request.urlopen(base + '/healthz', timeout=1) as response:
                        self.assertEqual(response.status, 200)
                    break
                except (urllib.error.URLError, TimeoutError):
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail('Production WSGI server failed to become ready')
                    time.sleep(.05)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + '/api/sessions', timeout=1)
            self.assertEqual(caught.exception.code, 401)
        finally:
            process.terminate()
            process.communicate(timeout=10)

    def test_deployed_app_loads_only_local_store_with_required_auth(self):
        from unittest.mock import patch
        import os
        import dashboard
        env = {'GHOST_LEDGER_STATE': str(Path(self.tmp.name).resolve() / 'deployed' / 'ledger.sqlite3'),
               'GHOST_LEDGER_PASSWORD_HASH': self.config['PASSWORD_HASH'],
               'GHOST_LEDGER_SECRET_KEY': self.config['SECRET_KEY'],
               'GHOST_LEDGER_ALLOWED_HOSTS': 'localhost:8897'}
        with patch.dict(os.environ, env):
            deployed = dashboard.app
            client = deployed.test_client()
            self.assertEqual(client.get('/healthz', base_url='http://localhost:8897').status_code, 200)
            self.assertEqual(client.get('/api/sessions', base_url='http://localhost:8897').status_code, 401)
            self.assertTrue(Path(env['GHOST_LEDGER_STATE']).exists())

    def test_non_ascii_csrf_is_rejected_without_server_error(self):
        self.client.get('/login')
        response = self.client.post('/login', data={'csrf_token': '☃', 'password': self.password})
        self.assertEqual(response.status_code, 403)

    def test_malformed_password_hash_configuration_fails_closed(self):
        from dashboard import create_app
        for value in ('plain-text-not-a-hash', 'scrypt:invalid$salt$hash'):
            with self.assertRaises(ValueError):
                create_app(self.store, dict(self.config, PASSWORD_HASH=value))

    def test_required_login_configuration_fails_closed(self):
        from dashboard import create_app
        from unittest.mock import patch
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError):
                create_app(self.store)


if __name__ == '__main__':
    unittest.main()
