"""Synthetic full login through actual loopback HTTP; never contacts Tesla."""
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit,urlencode
import urllib.request

import ghost_ledger
from callback_server import CallbackServer
from runtime import load_tokens,history_once,BudgetExceeded,save_tokens


class LoginIntegrationTests(unittest.TestCase):
    def test_browser_callback_exchange_and_private_save(self):
        ready=threading.Event()
        urls=[]; servers=[]; calls=[]
        class Capture(io.StringIO):
            def write(self,text):
                result=super().write(text)
                if text.startswith('https://auth.tesla.com/'):
                    urls.append(text)
                    ready.set()
                return result
        def factory(attempt):
            server=CallbackServer(attempt,port=0,timeout=2)
            servers.append(server)
            return server
        def transport(url,data,headers,timeout):
            calls.append(url)
            form=parse_qs(data)
            self.assertEqual(form['code'],['synthetic-code'])
            self.assertEqual(form['client_secret'],['synthetic-secret'])
            self.assertIn('code_verifier',form)
            return 200,json.dumps({'access_token':'synthetic-access','refresh_token':'synthetic-refresh','expires_in':3600,'token_type':'Bearer'}).encode()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()/'private'
            with patch.object(ghost_ledger,'CallbackServer',factory),patch('sys.stdout',Capture()):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    result=pool.submit(ghost_ledger.login,root,'synthetic-client','synthetic-secret',transport=transport)
                    self.assertTrue(ready.wait(timeout=2))
                    state=parse_qs(urlsplit(urls[0]).query)['state'][0]
                    port=servers[0].port
                    url=f'http://127.0.0.1:{port}/callback?'+urlencode({'state':state,'code':'synthetic-code'})
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(url,timeout=2) as response:
                        self.assertEqual(response.status,200)
                    result.result(timeout=3)
            self.assertEqual(load_tokens(root)['access_token'],'synthetic-access')
            self.assertEqual(calls,['https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token'])
            self.assertFalse((root/'probe-budget.sqlite3').exists())

    def test_concurrent_probe_cannot_bypass_allowance(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()/'private'
            save_tokens(root,{'access_token':'synthetic','refresh_token':'synthetic','expires_in':3600})
            calls=[]
            def transport(*args):
                calls.append(1)
                return 200,b'{"response":{"data":[]}}'
            def run():
                try:
                    history_once(root,transport=transport)
                    return 'sent'
                except BudgetExceeded:
                    return 'blocked'
            with ThreadPoolExecutor(max_workers=4) as pool:
                results=list(pool.map(lambda _:run(),range(4)))
            self.assertEqual(results.count('sent'),1)
            self.assertEqual(results.count('blocked'),3)
            self.assertEqual(len(calls),1)
