"""Owner-specific Fleet sign-in storage and a one-request billing probe.

No refresh loop, background polling, vehicle calls, invoice downloads or DB writer.
The persistent probe allowance is ONE request total, not one per invocation.
"""
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
import urllib.error
import urllib.request

TOKEN_URL='https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token'
HISTORY_URL='https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/dx/charging/history?pageSize=5'
MAX_RESPONSE=2*1024*1024

class BudgetExceeded(ValueError):
    pass


def _private_dir(root):
    root=Path(root).expanduser().absolute()
    # State must be user-owned and private. Never follow symlinks in its path.
    for parent in (root,*root.parents):
        if parent.is_symlink():
            raise ValueError('symlink state path rejected')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=root.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ValueError('state directory must be owned by you and mode 0700')
    return root


def _atomic_json(root,name,data):
    root=_private_dir(root)
    fd,path=tempfile.mkstemp(prefix='.pending-',dir=root)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(data,f,allow_nan=False,separators=(',',':'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(path,root/name)
        directory=os.open(root,os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _read_private(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'r') as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
            raise ValueError('private state file has unsafe permissions')
        text=f.read(MAX_RESPONSE+1)
        if len(text)>MAX_RESPONSE:
            raise ValueError('private state file too large')
        return json.loads(text)


def save_tokens(root,tokens,*,clock=time.time):
    try:
        access=tokens['access_token']; refresh=tokens['refresh_token']; expiry=tokens['expires_in']; now=clock()
        if any(not isinstance(x,str) or not x or len(x)>16384 for x in (access,refresh)):
            raise ValueError
        if isinstance(expiry,bool) or not isinstance(expiry,(float,int)) or not math.isfinite(expiry) or not 0<expiry<=86400:
            raise ValueError
        if not isinstance(now,(int,float)) or isinstance(now,bool) or not math.isfinite(now):
            raise ValueError
    except (KeyError,TypeError,ValueError):
        raise ValueError('invalid token response') from None
    _atomic_json(root,'tokens.json',{'access_token':access,'refresh_token':refresh,'expires_at':now+expiry})


def load_tokens(root,*,clock=time.time):
    try:
        data=_read_private(_private_dir(root)/'tokens.json')
        expiry=data['expires_at']; now=clock()
        if isinstance(expiry,bool) or not isinstance(expiry,(int,float)) or not math.isfinite(expiry):
            raise ValueError
        if not isinstance(now,(int,float)) or isinstance(now,bool) or not math.isfinite(now) or now>=expiry-30:
            raise ValueError
        if not isinstance(data['access_token'],str) or not data['access_token']:
            raise ValueError
        return data
    except (OSError,ValueError,TypeError,KeyError):
        raise ValueError('valid Ghost Ledger sign-in required; HA tokens are not used') from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def tesla_transport(url,data,headers,timeout):
    """Strict endpoint allowlist; finite socket timeout, not a total DNS deadline."""
    if (url==TOKEN_URL and data is not None):
        method='POST'; limit=65536
    elif url==HISTORY_URL and data is None:
        method='GET'; limit=MAX_RESPONSE
    else:
        raise ValueError('Tesla request target rejected')
    if isinstance(data,str):
        data=data.encode('utf-8')
    request=urllib.request.Request(url,data=data,headers={**headers,'User-Agent':'GhostLedger/0.1'},method=method)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
    try:
        response=opener.open(request,timeout=timeout)
    except urllib.error.HTTPError as error:
        response=error
    except Exception:
        raise ValueError('Tesla transport failed; request was not retried') from None
    try:
        with response:
            body=response.read(limit+1)
            if len(body)>limit:
                raise ValueError
            return response.status,body
    except Exception:
        raise ValueError('Tesla response could not be read; request was not retried') from None


def _reserve_probe(root):
    root=_private_dir(root)
    path=root/'probe-budget.sqlite3'
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
            raise ValueError('unsafe budget permissions')
    finally:
        os.close(fd)
    with sqlite3.connect(path,timeout=5) as db:
        db.execute('CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY CHECK(id=1), reserved INTEGER NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        used=db.execute('SELECT reserved FROM probe WHERE id=1').fetchone()
        if used:
            raise BudgetExceeded('single-request test allowance already reserved; no request sent')
        db.execute('INSERT INTO probe VALUES (1,1)')
        db.commit()  # Reserve durably BEFORE HTTP; failures still consume the allowance.


def history_once(root,*,transport=tesla_transport,clock=time.time):
    token=load_tokens(root,clock=clock)['access_token']
    _reserve_probe(root)
    try:
        status,body=transport(HISTORY_URL,None,{'Authorization':'Bearer '+token},20)
    except Exception:
        raise ValueError('billing request failed; allowance consumed; no retry') from None
    if status!=200:
        safe_status=status if type(status) is int and 100<=status<=599 else 'unknown'
        raise ValueError(f'billing request HTTP {safe_status}; allowance consumed; no retry')
    try:
        if not isinstance(body,bytes) or len(body)>MAX_RESPONSE:
            raise ValueError
        raw=json.loads(body)
        payload=raw.get('response',raw) if isinstance(raw,dict) else raw
        if isinstance(payload,dict) and isinstance(payload.get('data'),list):
            items=payload['data']
        elif isinstance(payload,list):
            items=payload
        else:
            raise ValueError
        if not all(isinstance(item,dict) for item in items):
            raise ValueError
    except (ValueError,TypeError):
        raise ValueError('unexpected billing response; allowance consumed; no retry') from None
    _atomic_json(root,'history.json',raw)
    return {'http_status':200,'returned_sessions':len(items),'pagination_followed':False}
