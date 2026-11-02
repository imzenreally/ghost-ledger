"""Opt-in synthetic PostgreSQL integration; never defaults to a live database.
GHOST_LEDGER_TEST_DSN must point to a disposable local DB named synthetic.
"""
import os
import unittest
from decimal import Decimal

from charging_costs.importer import build_preview
from charging_costs.postgres import PgRepository
from charging_costs.test_importer import bill

DSN = os.environ.get('GHOST_LEDGER_TEST_DSN')


@unittest.skipUnless(DSN, 'requires explicit disposable GHOST_LEDGER_TEST_DSN')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        info = conninfo_to_dict(DSN)
        if info.get('dbname') != 'synthetic' or info.get('host') != '127.0.0.1':
            raise RuntimeError('tests only permit synthetic DB on loopback')
        self.connect = lambda: psycopg.connect(DSN)
        with self.connect() as conn:
            conn.execute('DROP TABLE IF EXISTS charging_processes; DROP TABLE IF EXISTS cars')
            conn.execute('CREATE TABLE cars (id integer PRIMARY KEY, vin text)')
            conn.execute('''CREATE TABLE charging_processes (
                id integer PRIMARY KEY, car_id integer, start_date timestamp, end_date timestamp,
                cost numeric, charge_energy_used numeric, charge_energy_added numeric, geofence_id integer)''')
            conn.execute("INSERT INTO cars VALUES (1, 'SYNTHETICVIN')")
            conn.execute("""INSERT INTO charging_processes VALUES
                (42, 1, '2026-01-01 12:00:00', '2026-01-01 12:30:00', NULL, 20, 18, 2)""")
        self.repo = PgRepository(self.connect)

    def preview(self, records=None):
        history = records if records is not None else [bill()]
        return build_preview(history, self.repo.sessions_for(history))[0]

    def cost(self):
        with self.connect() as conn:
            return conn.execute('SELECT cost FROM charging_processes WHERE id=42').fetchone()[0]

    def test_transactional_import_readback_and_repeat_protection(self):
        row = self.preview()
        result = self.repo.apply(row)
        self.assertEqual(result['status'], 'imported')
        self.assertEqual(result['readback']['cost'], '8.00')
        self.assertEqual(self.cost(), Decimal('8'))
        self.assertEqual(self.repo.apply(row)['status'], 'protected')

    def test_apply_rechecks_every_corroboration_field(self):
        mutations = ["cost=0", "cost=8", "geofence_id=1", "charge_energy_used=18",
                     "end_date='2026-01-01 12:32:00'", "start_date='2026-01-01 12:06:00'"]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.setUp()
                row = self.preview()
                with self.connect() as conn:
                    conn.execute('UPDATE charging_processes SET ' + mutation)
                self.assertNotEqual(self.repo.apply(row)['status'], 'imported')
        self.setUp()
        row = self.preview()
        with self.connect() as conn:
            conn.execute("UPDATE cars SET vin='OTHER'")
        self.assertNotEqual(self.repo.apply(row)['status'], 'imported')
        self.assertIsNone(self.cost())

    def test_forged_or_evidenceless_rows_never_write(self):
        row = self.preview()
        self.assertEqual(PgRepository(self.connect).apply(row)['status'], 'invalid')
        for change in ({'amount': '1.00'}, {'session_id': 99}, {'currency': 'EUR'},
                       {'external_id': '999'}, {'status': 'protected'}):
            self.assertNotEqual(self.repo.apply(dict(row, **change))['status'], 'imported')
        self.assertIsNone(self.cost())

    def test_original_batch_is_private_immutable_and_serializable(self):
        import json
        history = [bill()]
        row = build_preview(history, self.repo.sessions_for(history))[0]
        history[0]['fees'][0]['totalDue'] = '1'
        restored = json.loads(json.dumps([bill()]))
        other = PgRepository(self.connect)
        other.sessions_for(restored)
        self.assertEqual(other.apply(row)['status'], 'imported')
        self.assertEqual(self.repo.apply(row)['status'], 'protected')

    def test_new_ambiguity_since_preview_prevents_write(self):
        row = self.preview()
        with self.connect() as conn:
            conn.execute('INSERT INTO charging_processes SELECT 43, car_id, start_date, end_date, cost, '
                         'charge_energy_used, charge_energy_added, geofence_id FROM charging_processes WHERE id=42')
        self.assertEqual(self.repo.apply(row)['status'], 'ambiguous')
        self.assertIsNone(self.cost())

    def test_collision_batch_refuses_forged_ready_status(self):
        for records in ([bill(), bill()], [bill(), bill(sessionId=102)]):
            row = self.preview(records)
            self.assertEqual(self.repo.apply(dict(row, status='ready'))['status'], 'conflict')
        self.assertIsNone(self.cost())

    def test_readback_mismatch_rolls_back_update(self):
        row = self.preview()
        with self.connect() as conn:
            conn.execute('''CREATE OR REPLACE FUNCTION synthetic_cost_trigger() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN NEW.cost := 99; RETURN NEW; END $$''')
            conn.execute('''CREATE TRIGGER synthetic_corrupt BEFORE UPDATE ON charging_processes
                FOR EACH ROW EXECUTE FUNCTION synthetic_cost_trigger()''')
        with self.assertRaisesRegex(RuntimeError, 'readback'):
            self.repo.apply(row)
        self.assertIsNone(self.cost())

    def test_concurrent_apply_has_only_one_writer(self):
        import concurrent.futures
        import psycopg
        row = self.preview()
        other = PgRepository(self.connect)
        other.sessions_for([bill()])
        def apply(repo):
            try:
                return repo.apply(row)['status']
            except psycopg.errors.SerializationFailure:
                return repo.apply(row)['status']
        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            results = list(executor.map(apply, [self.repo, other]))
        self.assertCountEqual(results, ['imported', 'protected'])
        self.assertEqual(self.cost(), Decimal('8'))

    def test_bound_query_treats_injected_vin_as_data(self):
        self.assertEqual(self.repo.sessions_for([bill(vin="' OR TRUE --")]), [])
        self.assertIsNone(self.cost())

    def test_psycopg_dictionary_row_factory_supported(self):
        import psycopg
        from psycopg.rows import dict_row
        repo = PgRepository(lambda: psycopg.connect(DSN, row_factory=dict_row))
        row = build_preview([bill()], repo.sessions_for([bill()]))[0]
        self.assertEqual(repo.apply(row)['readback'], {'session_id': 42, 'cost': '8.00'})

    def test_candidate_limit_fails_closed_without_write(self):
        with self.connect() as conn:
            conn.execute('''INSERT INTO charging_processes
                SELECT n, 1, '2026-01-01 12:00:00'::timestamp, '2026-01-01 12:30:00'::timestamp,
                    NULL, 20, 18, 2 FROM generate_series(100, 10100) n''')
        with self.assertRaisesRegex(ValueError, 'limit'):
            self.repo.sessions_for([bill()])
        self.assertIsNone(self.cost())


if __name__ == '__main__':
    unittest.main()
