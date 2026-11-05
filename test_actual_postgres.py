"""Real disposable PostgreSQL authoritative reconciliation tests."""
import os
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
import charging_costs.test_postgres as fixtures
from charging_costs.test_importer import bill
from charging_costs.importer import build_preview
from charging_costs.policy import TeslaBillPolicy
from workflow import run_import
from ledger_store import LedgerStore


@unittest.skipUnless(fixtures.DSN, 'requires explicit disposable GHOST_LEDGER_TEST_DSN')
class ActualPostgresTests(unittest.TestCase):
    setUp = fixtures.PostgresTests.setUp
    cost = fixtures.PostgresTests.cost

    def authoritative_preview(self, amount='7', records=None):
        self.repo.policy = TeslaBillPolicy('2026-01-01T00:00:00Z')
        with self.connect() as conn:
            conn.execute('UPDATE charging_processes SET cost=%s', (amount,))
        history = records if records is not None else [bill()]
        return build_preview(history, self.repo.sessions_for(history), policy=self.repo.policy)[0]

    def test_authoritative_replace_under_lock_and_cas(self):
        row = self.authoritative_preview()
        self.assertEqual(self.repo.apply(row)['status'], 'imported')
        self.assertEqual(self.cost(), Decimal('8'))
        self.assertNotEqual(self.repo.apply(row)['status'], 'imported')

    def test_zero_previous_cost_and_finalized_free_bill(self):
        row = self.authoritative_preview('0')
        self.assertEqual(self.repo.apply(row)['status'], 'imported')
        free = bill()
        free['fees'][0].update(totalDue='0', totalBase='0', netDue='0')
        row = self.authoritative_preview('8', [free])
        self.assertEqual(self.repo.apply(row)['status'], 'imported')
        self.assertEqual(self.cost(), Decimal('0'))

    def test_concurrent_cost_and_identity_mutations_within_match_tolerance_refuse_write(self):
        for sql in ('UPDATE charging_processes SET cost=6',
                    "UPDATE charging_processes SET start_date=start_date+interval '1 second'",
                    "UPDATE charging_processes SET end_date=end_date+interval '1 second'",
                    'UPDATE charging_processes SET charge_energy_used=20.01',
                    'UPDATE charging_processes SET charge_energy_added=18.01',
                    'UPDATE charging_processes SET geofence_id=3',
                    'UPDATE charging_processes SET geofence_id=1',
                    "INSERT INTO cars VALUES (2, 'SYNTHETICVIN'); UPDATE charging_processes SET car_id=2"):
            with self.subTest(sql=sql):
                self.setUp()
                row = self.authoritative_preview()
                with self.connect() as conn:
                    conn.execute(sql)
                self.assertNotEqual(self.repo.apply(row)['status'], 'imported')
                self.assertNotEqual(self.cost(), Decimal('8'))

    def test_full_batch_collision_still_blocks_forced_ready(self):
        for records in ([bill(), bill()], [bill(), bill(sessionId=102)],
                        [bill(), bill(sessionId=102, invoices=[])]):
            row = self.authoritative_preview(records=records)
            self.assertEqual(self.repo.apply(dict(row, status='ready'))['status'], 'conflict')
        self.assertEqual(self.cost(), Decimal('7'))

    def test_automatic_due_targets_only_real_pg_and_dump(self):
        import json
        import subprocess
        from automatic_billing import check_jobs
        from auth.runtime import save_tokens
        from test_automatic_billing import job
        container = os.environ.get('GHOST_LEDGER_TEST_CONTAINER', '')
        if not container.startswith('ghost-ledger-verify-'):
            self.skipTest('requires explicit disposable container')
        self.authoritative_preview()
        with self.connect() as conn:
            conn.execute("INSERT INTO cars VALUES (2, 'OTHER-SYNTHETIC')")
            conn.execute('INSERT INTO charging_processes SELECT 43, 2, start_date, end_date, 6, '
                         'charge_energy_used, charge_energy_added, geofence_id FROM charging_processes WHERE id=42')
        connect = self.connect
        class Database:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self): return connect()
            def backup(self, path):
                result = subprocess.run(['docker', 'exec', container, 'pg_dump', '-U', 'postgres',
                                         '-d', 'synthetic', '-Fc'], capture_output=True, timeout=15, check=True)
                with open(path, 'xb') as dest:
                    dest.write(result.stdout)
                path.chmod(0o600)
                return path
        with tempfile.TemporaryDirectory() as tmp:
            from unittest.mock import patch
            root = Path(tmp).resolve(); root.chmod(0o700)
            save_tokens(root, dict(access_token='synthetic', refresh_token='synthetic-refresh', expires_in=3600), clock=lambda: 1)
            calls = []
            def transport(*args):
                calls.append(args)
                return 200, json.dumps({'data': [bill(), bill(sessionId=102, vin='OTHER-SYNTHETIC')]}).encode()
            with patch.dict('os.environ', {'GHOST_LEDGER_PREFER_TESLA_BILLS': 'true',
                                         'GHOST_LEDGER_TESLA_BILLS_SINCE': '2026-01-01T00:00:00Z'}):
                self.assertEqual(check_jobs(root, [job(car_id=1)], transport=transport,
                                            database_factory=Database, clock=lambda: 101), {42: 'imported'})
                self.assertEqual(check_jobs(root, [job(car_id=1)], transport=lambda *a: self.fail('repeat HTTP'),
                                            database_factory=Database, clock=lambda: 101), {42: 'imported'})
            self.assertEqual(self.cost(), Decimal('8'))
            with self.connect() as conn:
                self.assertEqual(conn.execute('SELECT cost FROM charging_processes WHERE id=43').fetchone()[0], Decimal('6'))
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(list((root / 'backups').glob('*.dump'))), 1)

    def test_workflow_actual_pg_replacement_audit_and_independent_readback(self):
        self.authoritative_preview()
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp).resolve() / 'ledger.sqlite3')
            report = run_import([bill()], self.repo, store, policy=self.repo.policy,
                                apply=True, allowed_session_ids={42})
            self.assertEqual((report['imported'], report['uncertain']), (1, 0))
            self.assertEqual(self.cost(), Decimal('8'))
            self.assertEqual(store.snapshot()['rows'][0]['status'], 'imported')
            self.assertEqual(run_import([bill()], self.repo, store, policy=self.repo.policy,
                                        apply=True)['imported'], 0)
