"""Synthetic responses only; never contacts Tesla."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from runtime import save_tokens, load_tokens, history_once, invoice_once, BudgetExceeded, tesla_transport

TOKENS={'access_token':'synthetic-access','refresh_token':'synthetic-refresh','expires_in':3600,'token_type':'Bearer'}

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'private'

    def test_invoice_is_single_call_and_private_pdf(self):
        save_tokens(self.root,TOKENS,clock=lambda:1000)
        calls=[]
        def transport(url,data,headers,timeout):
            calls.append(url)
            return 200,b'%PDF-1.4\nsynthetic invoice only'
        result=invoice_once(self.root,'synthetic-id',transport=transport,clock=lambda:1001)
        self.assertEqual(calls,['https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/dx/charging/invoice/synthetic-id'])
        self.assertEqual(result['http_status'],200)
        self.assertEqual(os.stat(self.root/'invoice.pdf').st_mode&0o777,0o600)
        with self.assertRaises(BudgetExceeded):
            invoice_once(self.root,'another-id',transport=transport,clock=lambda:1002)
        self.assertEqual(len(calls),1)

    def test_invoice_rejects_fake_pdf_and_consumes_allowance(self):
        save_tokens(self.root,TOKENS,clock=lambda:1000)
        with self.assertRaises(ValueError):
            invoice_once(self.root,'synthetic-id',transport=lambda *a:(200,b'<html>not a pdf</html>'),clock=lambda:1001)
        self.assertFalse((self.root/'invoice.pdf').exists())
        with self.assertRaises(BudgetExceeded):
            invoice_once(self.root,'synthetic-id',transport=lambda *a:self.fail('retried'),clock=lambda:1001)

    def test_private_roundtrip(self):
        save_tokens(self.root,TOKENS,clock=lambda:1000)
        saved=load_tokens(self.root,clock=lambda:1001)
        self.assertEqual(saved['access_token'],TOKENS['access_token'])
        self.assertEqual(os.stat(self.root).st_mode&0o777,0o700)
        self.assertEqual(os.stat(self.root/'tokens.json').st_mode&0o777,0o600)

    def test_expired_token_blocks_transport(self):
        save_tokens(self.root,TOKENS,clock=lambda:0)
        calls=[]
        with self.assertRaises(ValueError):
            history_once(self.root,transport=lambda *a:calls.append(a),clock=lambda:4000)
        self.assertEqual(calls,[])

    def test_one_call_then_hard_stop_after_restart(self):
        save_tokens(self.root,TOKENS,clock=lambda:1000)
        calls=[]
        def transport(url,data,headers,timeout):
            calls.append(url)
            self.assertIsNone(data)
            self.assertEqual(url,'https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/dx/charging/history?pageSize=5')
            return 200,b'{"response":{"data":[{"sessionId":"synthetic"}],"totalResults":50}}'
        result=history_once(self.root,transport=transport,clock=lambda:1001)
        self.assertEqual(result,{'http_status':200,'returned_sessions':1,'pagination_followed':False})
        with self.assertRaises(BudgetExceeded):
            history_once(Path(str(self.root)),transport=transport,clock=lambda:1002)
        self.assertEqual(len(calls),1)
        self.assertEqual(os.stat(self.root/'history.json').st_mode&0o777,0o600)

    def test_failure_spends_allowance_no_retry(self):
        save_tokens(self.root,TOKENS,clock=lambda:1000)
        calls=[]
        def transport(*args):
            calls.append(1)
            raise OSError('synthetic-access must not leak')
        with self.assertRaises(ValueError) as caught:
            history_once(self.root,transport=transport,clock=lambda:1001)
        self.assertNotIn('synthetic-access',str(caught.exception))
        with self.assertRaises(BudgetExceeded):
            history_once(self.root,transport=transport,clock=lambda:1002)
        self.assertEqual(len(calls),1)

    def test_http_error_spends_allowance_and_sanitizes(self):
        save_tokens(self.root,TOKENS,clock=lambda:1000)
        with self.assertRaisesRegex(ValueError,'403'):
            history_once(self.root,transport=lambda *a:(403,b'synthetic-private-error'),clock=lambda:1001)
        with self.assertRaises(BudgetExceeded):
            history_once(self.root,transport=lambda *a:self.fail('called again'),clock=lambda:1001)

    def test_transport_rejects_unknown_targets_before_io(self):
        for url,data in [('https://example.com/',None),('http://fleet-api.prd.na.vn.cloud.tesla.com/api/1/dx/charging/history',None),('https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles/1/wake_up',b'')]:
            with self.subTest(url=url),self.assertRaises(ValueError):
                tesla_transport(url,data,{},20)

    def test_bad_history_shape_not_reported_as_empty_success(self):
        save_tokens(self.root,TOKENS,clock=lambda:1000)
        with self.assertRaises(ValueError):
            history_once(self.root,transport=lambda *a:(200,b'{"error":"not history"}'),clock=lambda:1001)

    def test_symlink_state_rejected(self):
        elsewhere=Path(self.temp.name)/'elsewhere'
        elsewhere.mkdir()
        self.root.symlink_to(elsewhere,target_is_directory=True)
        with self.assertRaises(ValueError):
            save_tokens(self.root,TOKENS,clock=lambda:1000)
