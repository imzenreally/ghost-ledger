import socket
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from callback_server import CallbackServer
from oauth_flow import OAuthAttempt


def _query(attempt, code):
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(attempt.authorize_url()).query)['state'][0]
    return urllib.parse.urlencode({'state': state, 'code': code})


def _get(port, path, host):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers={'Host':host})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(req, timeout=1)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read(), dict(response.headers)


class CallbackTests(unittest.TestCase):
    def test_badhost_then_valid_real_attempt(self):
        attempt = OAuthAttempt('synthetic-client')
        query = _query(attempt, 'synthetic-code-1')
        with CallbackServer(attempt, port=0, timeout=2) as server:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(server.wait)
                status, _, _ = _get(server.port, '/callback?'+query, 'localhost:9999')
                self.assertEqual(status,400)
                status, body, _ = _get(server.port, '/callback?'+query, f'127.0.0.1:{server.port}')
                self.assertEqual(status,200)
                self.assertNotIn(b'synthetic-code-1',body)
                self.assertEqual(result.result(timeout=3)['code'],'synthetic-code-1')

    def test_bind_host_rejected(self):
        with self.assertRaises(ValueError):
            CallbackServer(OAuthAttempt('synthetic-client'),host='0.0.0.0')

    def test_wrongpath_then_valid_headers(self):
        attempt = OAuthAttempt('synthetic-client')
        query = _query(attempt, 'synthetic-code-2')
        with CallbackServer(attempt,port=0,timeout=2) as server:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result=pool.submit(server.wait)
                status,_,_=_get(server.port,'/wrong',f'localhost:{server.port}')
                self.assertEqual(status,404)
                status,_,headers=_get(server.port,'/callback?'+query,f'localhost:{server.port}')
                self.assertEqual(status,200)
                self.assertEqual(headers.get('Cache-Control'),'no-store')
                self.assertEqual(headers.get('Referrer-Policy'),'no-referrer')
                self.assertEqual(result.result(timeout=3)['code'],'synthetic-code-2')

    def test_idle_timeout(self):
        with CallbackServer(OAuthAttempt('synthetic-client'),port=0,timeout=0.1) as server:
            start=time.monotonic()
            with self.assertRaises(TimeoutError):
                server.wait()
            self.assertLess(time.monotonic()-start,1)

    def test_incomplete_request_timeout(self):
        with CallbackServer(OAuthAttempt('synthetic-client'),port=0,timeout=0.2) as server:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result=pool.submit(server.wait)
                with socket.create_connection(('127.0.0.1',server.port),timeout=1):
                    with self.assertRaises(TimeoutError):
                        result.result(timeout=2)
