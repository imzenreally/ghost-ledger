import tempfile
import unittest
from pathlib import Path

class ServiceTests(unittest.TestCase):
    def test_confirmation_uses_bounded_completed_same_car_query(self):
        from passive_service import confirm_candidates
        class Cursor:
            def execute(self, sql, params=None):
                calls.append((sql,params))
            def fetchall(self): return [(42,1,1001.0)]
            def close(self): pass
        class Connection:
            def cursor(self): return Cursor()
            def rollback(self): pass
            def close(self): pass
        class Database:
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def connect(self): return Connection()
        class Detector:
            def snapshot(self): return {'rows':[{'event_id':'test','car_id':1,'started_at':900,'observed_at':1000,'status':'needs_db_confirmation'}]}
            def confirm(self,*args,**kwargs): confirmed.append((args,kwargs))
        calls=[];confirmed=[]
        result=confirm_candidates(Detector(),Database)
        self.assertEqual(result['confirmed'],1)
        self.assertIn('READ ONLY',calls[0][0])
        self.assertIn('cp.car_id = %s',calls[-1][0])
        self.assertIn('LIMIT 2',calls[-1][0])
        self.assertEqual(confirmed[0][0][:4],('test',42,1,True))

    def test_ambiguous_db_sessions_never_confirm(self):
        from passive_service import confirm_candidates
        class Cursor:
            def execute(self,*a): pass
            def fetchall(self): return [(42,2,1001),(43,2,1001)]
            def close(self): pass
        class Connection:
            def cursor(self): return Cursor()
            def rollback(self): pass
            def close(self): pass
        class Database:
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def connect(self): return Connection()
        class Detector:
            def snapshot(self):return {'rows':[{'event_id':'test','car_id':1,'started_at':900,'observed_at':1000,'status':'needs_db_confirmation'}]}
            def confirm(self,*a,**k): raise AssertionError('ambiguous confirmation')
        self.assertEqual(confirm_candidates(Detector(),Database)['confirmed'],0)

    def test_broker_tunnel_is_loopback_and_does_not_include_password(self):
        from passive_service import broker_tunnel_arguments
        args=broker_tunnel_arguments({'host':'192.168.0.11','port':1884,'password':'never-in-argv'}, 43210)
        self.assertIn('127.0.0.1:43210:192.168.0.11:1884',args)
        self.assertNotIn('never-in-argv',' '.join(args))
        self.assertIn('ExitOnForwardFailure=yes',args)

    def test_config_contains_only_client_id_not_client_secret(self):
        from passive_service import read_client_id
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp).resolve()/'credentials.env'
            Path(tmp).resolve().chmod(0o755)
            path.write_text('TESLA_CLIENT_ID=synthetic-id\nTESLA_CLIENT_SECRET=do-not-use\n');path.chmod(0o600)
            self.assertEqual(read_client_id(path),'synthetic-id')
