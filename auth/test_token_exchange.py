import json, math, unittest
from urllib.parse import parse_qsl
import token_exchange

URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
AUD = "https://fleet-api.prd.na.vn.cloud.tesla.com"
FORM = {
    "grant_type": "authorization_code",
    "client_id": "cid",
    "code": "code123",
    "redirect_uri": "http://localhost:8769/callback",
    "code_verifier": "verifier"
}
SECRET = "secret-value"
BODY = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "token_type": "Bearer"}

def ok(json_body):
    return 200, json.dumps(json_body).encode("utf-8")

class TokenExchangeTests(unittest.TestCase):
    def test_valid_exchange_returns_only_fields(self):
        calls = []
        def transport(url, data, headers, timeout):
            calls.append((url, data, headers, timeout))
            return ok(BODY)
        got = token_exchange.exchange_code(FORM, SECRET, transport=transport)
        self.assertEqual(len(calls), 1)
        url, raw, headers, timeout = calls[0]
        self.assertEqual(url, URL)
        self.assertEqual(timeout, 20)
        self.assertEqual(headers.get("Content-Type"), "application/x-www-form-urlencoded")
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        sent = dict(parse_qsl(raw, keep_blank_values=True))
        self.assertEqual(sent, {**FORM, "client_secret": SECRET, "audience": AUD})
        self.assertEqual(got, BODY)
        self.assertEqual(set(got), {"access_token", "refresh_token", "expires_in", "token_type"})

    def test_http_400_sanitizes_error_and_one_call(self):
        calls = []
        bad = {"error": "bad", "client_secret": SECRET, "detail": "raw-body-marker"}
        def transport(url, data, headers, timeout):
            calls.append(1)
            return 400, json.dumps(bad).encode("utf-8")
        with self.assertRaises(ValueError) as cm:
            token_exchange.exchange_code(FORM, SECRET, transport=transport)
        self.assertEqual(calls, [1])
        msg = str(cm.exception)
        self.assertNotIn(SECRET, msg)
        self.assertNotIn("raw-body-marker", msg)

    def test_missing_refresh_token_rejected(self):
        calls = []
        bad = {k: v for k, v in BODY.items() if k != "refresh_token"}
        def transport(url, data, headers, timeout):
            calls.append(1)
            return ok(bad)
        with self.assertRaises(ValueError):
            token_exchange.exchange_code(FORM, SECRET, transport=transport)
        self.assertEqual(len(calls), 1)