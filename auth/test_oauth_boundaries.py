import unittest
from urllib.parse import parse_qs, urlsplit, urlencode
from oauth_flow import OAuthAttempt

class OAuthBoundaryTests(unittest.TestCase):
    def test_tesla_issuer_accepted_with_matching_state(self):
        attempt=OAuthAttempt('synthetic-client')
        state=parse_qs(urlsplit(attempt.authorize_url()).query)['state'][0]
        result=attempt.consume_callback(urlencode({'code':'synthetic-code','issuer':'https://auth.tesla.com/oauth2/v3','state':state}))
        self.assertEqual(result['code'],'synthetic-code')
        self.assertNotIn('issuer',result)

    def test_wrong_issuer_rejected_without_consuming_attempt(self):
        attempt=OAuthAttempt('synthetic-client')
        state=parse_qs(urlsplit(attempt.authorize_url()).query)['state'][0]
        with self.assertRaises(ValueError):
            attempt.consume_callback(urlencode({'code':'synthetic-code','issuer':'https://example.com/oauth2/v3','state':state}))
        result=attempt.consume_callback(urlencode({'code':'synthetic-code','issuer':'https://auth.tesla.com/oauth2/v3','state':state}))
        self.assertEqual(result['code'],'synthetic-code')

    def test_duplicate_issuer_rejected(self):
        attempt=OAuthAttempt('synthetic-client')
        state=parse_qs(urlsplit(attempt.authorize_url()).query)['state'][0]
        query=urlencode({'code':'synthetic-code','issuer':'https://auth.tesla.com/oauth2/v3','state':state})+'&issuer='
        with self.assertRaises(ValueError):
            attempt.consume_callback(query)

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
