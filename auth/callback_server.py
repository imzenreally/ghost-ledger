import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Handler(BaseHTTPRequestHandler):
    server_version = 'GL'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _secure_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Length', '0')

    def _finish(self, code):
        self.close_connection = True
        self.send_response(code)
        self._secure_headers()
        self.send_header('Connection', 'close')
        self.end_headers()

    def _ok(self):
        self._finish(200)

    def _error(self, code):
        self._finish(code)

    def do_GET(self):
        server = self.server
        try:
            parsed = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._error(400)
            return
        path, query = parsed.path, parsed.query
        if path != '/callback':
            self._error(404)
            return
        host = self.headers.get('Host', '')
        port = server.server_address[1]
        if host not in ('localhost:%d' % port, '127.0.0.1:%d' % port):
            self._error(400)
            return
        with server._lock:
            if server._handled:
                self._error(400)
                return
            try:
                result = server.attempt.consume_callback(query)
            except (ValueError, TypeError):
                self._error(400)
                return
            server._result = result
            server._handled = True
        self._ok()


class _Server(HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        remaining = max(0.0, self._deadline - time.monotonic())
        self.socket.settimeout(remaining if remaining > 0 else 0.001)
        request, client_address = super().get_request()
        request.settimeout(remaining if remaining > 0 else 0.001)
        return request, client_address


class CallbackServer:
    def __init__(self, attempt, host='127.0.0.1', port=8769, timeout=300):
        if host not in ('127.0.0.1', 'localhost'):
            raise ValueError
        if not isinstance(port, int) or isinstance(port, bool) or port < 0 or port > 65535:
            raise ValueError
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 600:
            raise ValueError
        self.attempt = attempt
        self.host = host
        self.port = port
        self.timeout = float(timeout)
        self._server = None
        self._waited = False
        self._used = False
        self._lock = threading.Lock()
        self._handled = False
        self._result = None
        self._deadline = None

    def __enter__(self):
        if self._used:
            raise ValueError
        self._used = True
        self._server = _Server((self.host, self.port or 0), _Handler)
        self._server.attempt = self.attempt
        self._server._lock = self._lock
        self._server._handled = False
        self._server._result = None
        self._server._deadline = None
        self.port = self._server.server_address[1]
        return self

    def wait(self):
        if self._waited:
            raise ValueError
        self._waited = True
        if self._server is None:
            raise ValueError
        deadline = time.monotonic() + self.timeout
        self._server._deadline = deadline
        try:
            while time.monotonic() < deadline:
                remaining = max(0.001, deadline - time.monotonic())
                self._server.timeout = remaining
                try:
                    self._server.handle_request()
                except (socket.timeout, OSError):
                    break
                with self._lock:
                    if self._server._handled:
                        return self._server._result
        finally:
            self.close()
        raise TimeoutError

    def close(self):
        if self._server is not None:
            self._server._deadline = 0
            try:
                self._server.server_close()
            except OSError:
                pass
            self._server = None

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False