import unittest
from urllib.parse import parse_qs, urlsplit, urlencode
from oauth_flow import OAuthAttempt

class OAuthBoundaryTests(unittest.TestCase):
    def test_rejects_nonfinite_initial_clock(self):
        for value in (float('nan'),float('inf'),True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OAuthAttempt('synthetic-client',clock=lambda:value)

    def test_rejects_blank_duplicate_state(self):
        attempt=OAuthAttempt('synthetic-client')
        state=parse_qs(urlsplit(attempt.authorize_url()).query)['state'][0]
        query=urlencode({'state':state,'code':'synthetic-code'})+'&state='
        with self.assertRaises(ValueError):
            attempt.consume_callback(query)

    def test_non_ascii_state_is_sanitized_value_error(self):
        attempt=OAuthAttempt('synthetic-client')
        with self.assertRaises(ValueError):
            attempt.consume_callback(urlencode({'state':'☃','code':'synthetic-code'}))
