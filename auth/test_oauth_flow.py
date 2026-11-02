import base64
import hashlib
import unittest
import urllib.parse

from oauth_flow import OAuthAttempt

CLIENT = "ghost-ledger"
CODE = "synthetic-code-0001"


def parse_qs(query):
    return urllib.parse.parse_qs(query, keep_blank_values=True)


def callback(state, code=CODE):
    return f"state={state}&code={code}"


class OAuthFlowTests(unittest.TestCase):
    def test_authorize_url_scope_and_s256(self):
        attempt = OAuthAttempt(CLIENT)
        url = attempt.authorize_url()
        parsed = urllib.parse.urlsplit(url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("https", "auth.tesla.com", "/oauth2/v3/authorize"))
        q = parse_qs(parsed.query)
        self.assertEqual(q["scope"], ["openid offline_access vehicle_charging_cmds"])
        self.assertEqual(q["code_challenge_method"], ["S256"])
        state = q["state"][0]
        grant = attempt.consume_callback(callback(state))
        digest = hashlib.sha256(grant["code_verifier"].encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        self.assertEqual(challenge, q["code_challenge"][0])

    def test_invalid_state_then_valid_single_use_consume(self):
        attempt = OAuthAttempt(CLIENT)
        q = parse_qs(urllib.parse.urlsplit(attempt.authorize_url()).query)
        state = q["state"][0]
        with self.assertRaises(ValueError):
            attempt.consume_callback(f"state=wrong&code={CODE}")
        grant = attempt.consume_callback(callback(state))
        self.assertEqual(grant["grant_type"], "authorization_code")
        self.assertEqual(grant["client_id"], CLIENT)
        self.assertEqual(grant["code"], CODE)
        with self.assertRaises(ValueError):
            attempt.consume_callback(callback(state))

    def test_injected_clock_expiry(self):
        now = [0.0]
        attempt = OAuthAttempt(CLIENT, clock=lambda: now[0], ttl=10)
        attempt.authorize_url()
        now[0] = 10.0
        with self.assertRaises(ValueError):
            attempt.authorize_url()


if __name__ == "__main__":
    unittest.main()