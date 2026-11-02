"""Opt-in full pipeline against disposable loopback PostgreSQL; synthetic bills only."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from charging_costs.test_importer import bill
from charging_costs import test_postgres as pgtests
DSN=pgtests.DSN
from charging_detector import Detector
from billing_scheduler import Scheduler
from passive_service import confirm_candidates
from automatic_billing import check_jobs
from auth.runtime import save_tokens

CONTAINER=os.environ.get('GHOST_LEDGER_TEST_CONTAINER','')

@unittest.skipUnless(DSN and CONTAINER.startswith('ghost-ledger-verify-'), 'requires explicit disposable DB/container')
class PipelineIntegrationTests(unittest.TestCase):
    def test_passive_completion_to_bill_to_verified_import_and_no_repeat(self):
        fixture=pgtests.PostgresTests('test_transactional_import_readback_and_repeat_protection');fixture.setUp()
        connect=fixture.connect
        class Database:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def connect(self):return connect()
            def backup(self,path):
                result=subprocess.run(['docker','exec',CONTAINER,'pg_dump','-U','postgres','-d','synthetic','-Fc'],capture_output=True,timeout=15)
                if result.returncode:raise ValueError('synthetic backup failed')
                with open(path,'xb') as f:f.write(result.stdout)
                Path(path).chmod(0o600)
                return path
        observed=datetime(2026,1,1,12,30,1,tzinfo=timezone.utc).timestamp()
        clock=[observed]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();root.chmod(0o700)
            detector=Detector(root,clock=lambda:clock[0]);self.addCleanup(detector.close)
            detector.on_message('teslamate/cars/1/state','charging',received_at=observed-1800)
            detector.on_message('teslamate/cars/1/state','online',received_at=observed)
            self.assertEqual(confirm_candidates(detector,Database)['confirmed'],1)
            scheduler=Scheduler(root,clock=lambda:clock[0]);scheduler.sync(detector.snapshot()['rows'])
            save_tokens(root,{'access_token':'synthetic-access','refresh_token':'synthetic-refresh','expires_in':3600},clock=lambda:clock[0])
            clock[0]+=300;calls=[]
            def transport(url,data,headers,timeout):
                calls.append(url)
                return 200,json.dumps({'data':[bill()],'totalResults':1}).encode()
            report=scheduler.run_due(lambda jobs:check_jobs(root,jobs,transport=transport,database_factory=Database,clock=lambda:clock[0]))
            self.assertEqual(report['counts'],{'imported':1})
            self.assertEqual(str(fixture.cost()),'8.00')
            scheduler.run_due(lambda jobs:self.fail('already imported job repeated'))
            self.assertEqual(len(calls),1)
            self.assertTrue(list((root/'backups').glob('*.dump')))
