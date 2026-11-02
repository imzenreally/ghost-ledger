import base64
import hashlib
import math
import secrets
import threading
import time
import urllib.parse

_CALLBACK = "http://localhost:8769/callback"
_AUDIENCE = "https://fleet-api.prd.na.vn.cloud.tesla.com"
_SCOPE = "openid offline_access vehicle_charging_cmds"
_MAX_QUERY = 8192
_MAX_CODE = 4096
_FIELDS = ("state", "code")


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _control_chars(value):
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _parse_query(query):
    if len(query) > _MAX_QUERY:
        raise ValueError("callback query is too long")
    items = urllib.parse.parse_qsl(query, max_num_fields=8, keep_blank_values=True, strict_parsing=True)
    fields = {}
    for key, value in items:
        if key in fields:
            raise ValueError("callback query has duplicate fields")
        if key not in (*_FIELDS, "issuer"):
            raise ValueError("callback query has unknown fields")
        fields[key] = value
    return fields


class OAuthAttempt:
    def __init__(self, client_id, redirect_uri=_CALLBACK, *, clock=time.monotonic, ttl=300):
        if not isinstance(client_id, str) or not client_id or len(client_id) > 256:
            raise ValueError("client_id must be a nonempty string of at most 256 characters")
        if any(char.isspace() for char in client_id):
            raise ValueError("client_id must not contain whitespace")
        if redirect_uri != _CALLBACK:
            raise ValueError("redirect_uri is not allowed")
        if not _finite_number(ttl) or ttl <= 0 or ttl > 600:
            raise ValueError("ttl must be a finite number from 0 exclusive to 600 inclusive")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._clock = clock
        self._ttl = ttl
        self._lock = threading.Lock()
        self._state = secrets.token_urlsafe(32)
        self._verifier = secrets.token_urlsafe(64)
        self._created = clock()
        if not _finite_number(self._created):
            raise ValueError("clock must return a finite number")
        self._last_time = self._created
        self._used = False
        self._expired = False

    def _challenge(self):
        digest = hashlib.sha256(self._verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def _check_lifecycle(self):
        now = self._clock()
        if not _finite_number(now):
            raise ValueError("clock must return a finite number")
        if now < self._last_time:
            self._expired = True
            raise ValueError("clock moved backward")
        self._last_time = now
        if (now - self._created) >= self._ttl:
            self._expired = True
            raise ValueError("attempt expired")

    def authorize_url(self):
        with self._lock:
            self._check_lifecycle()
            if self._used or self._expired:
                raise ValueError("attempt is unavailable")
            query = urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": self._client_id,
                    "redirect_uri": self._redirect_uri,
                    "scope": _SCOPE,
                    "state": self._state,
                    "code_challenge_method": "S256",
                    "code_challenge": self._challenge(),
                    "audience": _AUDIENCE,
                }
            )
            return "https://auth.tesla.com/oauth2/v3/authorize?" + query

    def consume_callback(self, query_string):
        with self._lock:
            self._check_lifecycle()
            if self._used or self._expired:
                raise ValueError("attempt is unavailable")
            fields = _parse_query(query_string)
            if set(fields) - {"issuer"} != set(_FIELDS):
                raise ValueError("callback query must contain state and code")
            if "issuer" in fields and fields["issuer"] != "https://auth.tesla.com/oauth2/v3":
                raise ValueError("callback issuer does not match Tesla")
            state, code = fields["state"], fields["code"]
            if not state.isascii() or not secrets.compare_digest(state, self._state):
                raise ValueError("callback state does not match")
            if not code or len(code) > _MAX_CODE:
                raise ValueError("callback code has invalid length")
            if _control_chars(code) or _control_chars(state):
                raise ValueError("callback value contains control characters")
            self._used = True
            return {
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": self._verifier,
            }

    def __repr__(self):
        return f"<OAuthAttempt client_id={self._client_id!r} used={self._used}>"
