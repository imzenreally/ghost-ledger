"""Offline-only OAuth code exchange helper using an injected transport."""
import json
import math
from urllib.parse import urlencode

URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
AUDIENCE = "https://fleet-api.prd.na.vn.cloud.tesla.com"
FORM_FIELDS = ("grant_type", "client_id", "code", "redirect_uri", "code_verifier")
MAX_FIELD = 4096
MAX_BODY = 65536

def _fail():
    raise ValueError("token exchange failed") from None

def _check_text(value, field):
    if not isinstance(value, str) or not value or len(value) > MAX_FIELD:
        _fail()

def exchange_code(form, client_secret, *, transport):
    if not isinstance(form, dict) or tuple(form) != FORM_FIELDS:
        _fail()
    for field in FORM_FIELDS:
        _check_text(form[field], field)
    _check_text(client_secret, "client_secret")
    payload = urlencode({**dict(form), "client_secret": client_secret, "audience": AUDIENCE})
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        result = transport(URL, payload, headers, 20)
        if not isinstance(result, tuple) or len(result) != 2:
            _fail()
        status, body = result
        if not isinstance(status, int) or status != 200:
            _fail()
        if not isinstance(body, bytes) or len(body) > MAX_BODY:
            _fail()
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            _fail()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        expires_in = data.get("expires_in")
        token_type = data.get("token_type")
        _check_text(access_token, "access_token")
        _check_text(refresh_token, "refresh_token")
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            _fail()
        if not math.isfinite(expires_in) or expires_in <= 0 or expires_in > 86400:
            _fail()
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            _fail()
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": expires_in,
            "token_type": token_type,
        }
    except Exception:
        _fail()
