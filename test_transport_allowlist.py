import unittest
from unittest.mock import patch
from auth.runtime import tesla_transport

class TransportAllowlistTests(unittest.TestCase):
    def test_only_bounded_canonical_billing_page_urls_are_allowed(self):
        prefix='https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/dx/charging/history'
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,*a):return b'{"data":[]}'
        class Opener:
            def open(self,*a,**k):return Response()
        with patch('auth.runtime.urllib.request.build_opener',return_value=Opener()):
            for suffix in ('?pageSize=50','?pageSize=50&pageNo=1','?pageSize=50&pageNo=2'):
                self.assertEqual(tesla_transport(prefix+suffix,None,{},5)[0],200)
            for suffix in ('?pageSize=50&pageNo=3','?pageSize=500','?pageSize=50&pageNo=01',
                           '?pageSize=50&vin=anything','?pageSize=50&pageNo=1&pageNo=2',
                           '?pageNo=1&pageSize=50','?pageSize=50#fragment'):
                with self.assertRaises(ValueError):tesla_transport(prefix+suffix,None,{},5)
            with self.assertRaises(ValueError):tesla_transport(prefix+'?pageSize=50',b'bad',{},5)
