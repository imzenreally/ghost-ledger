import unittest
from dashboard import create_app

class Store:
    def snapshot(self):
        return {'rows': [], 'updated_at':None, 'warnings':[]}

class NoLoginTests(unittest.TestCase):
    def test_manual_writes_require_a_signing_key_even_without_login(self):
        with self.assertRaises(ValueError):
            create_app(Store(),{'AUTH_REQUIRED':False,'MANUAL_COSTS_ENABLED':True,
                                'PASSWORD_HASH':None,'SECRET_KEY':None})

    def test_explicit_no_login_keeps_review_and_boundaries(self):
        app=create_app(Store(), {'AUTH_REQUIRED':False, 'PASSWORD_HASH':None, 'SECRET_KEY':None,
                                  'ALLOWED_HOSTS':['localhost:8897']})
        client=app.test_client(); base='http://localhost:8897'
        root=client.get('/',base_url=base)
        self.assertEqual(root.status_code,200)
        self.assertNotIn(b'Sign out',root.data)
        self.assertNotIn('Set-Cookie',root.headers)
        self.assertEqual(client.get('/api/sessions',base_url=base).status_code,200)
        self.assertEqual(client.get('/login',base_url=base).status_code,302)
        self.assertEqual(client.post('/logout',base_url=base).status_code,404)
        self.assertEqual(client.get('/api/sessions',base_url=base,headers={'Host':'evil.invalid'}).status_code,400)
        self.assertEqual(client.get('/api/sessions',base_url=base,headers={'Origin':'https://evil.invalid'}).status_code,403)
        self.assertEqual(client.post('/api/sessions',base_url=base).status_code,405)
