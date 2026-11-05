"""Synthetic callback tests. Never contact Tesla, SSH, or a live database."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import copy
import json
from decimal import Decimal
from auth import runtime
from charging_costs.test_importer import bill, session
from ledger import import_lock


class SyntheticDatabase:
    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.queries = []
        self.connections = []
        self.backups = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def connect(self):
        conn = SyntheticConnection(self)
        self.connections.append(conn)
        return conn

    def backup(self, path):
        self.backups.append(path)
        path.write_bytes(b'PGDMPsynthetic-backup')
        path.chmod(0o600)
        return path


class SyntheticConnection:
    def __init__(self, db):
        self.db = db
        self.closed = False
        self.autocommit = True
        self.result = []

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.db.queries.append((sql, params))
        if sql.startswith('SET TRANSACTION'):
            return
        if 'WHERE cp.id IN (' in sql:
            self.result = [copy.deepcopy(r) for r in self.db.rows if r['id'] in params]
        elif sql.startswith('SELECT cp.id') and 'WHERE cp.id = %s' in sql:
            self.result = [copy.deepcopy(r) for r in self.db.rows if r['id'] == params[0]]
        elif sql.startswith('SELECT cp.id') and 'c.vin = %s' in sql:
            from charging_costs.importer import timestamp
            self.result = [copy.deepcopy(r) for r in self.db.rows if any(
                r['vin'] == params[i] and params[i+1] <= timestamp(r['start_date'], db=True).replace(tzinfo=None) <= params[i+2]
                for i in range(0, len(params), 3))]
        elif sql.startswith('UPDATE charging_processes'):
            assert self.db.backups, 'write before backup'
            if len(params) == 4:
                amount, sid, previous, home = params
            else:
                amount, sid, home = params
                previous = None
            found = [r for r in self.db.rows if r['id'] == sid and r['cost'] == previous and r['geofence_id'] != home]
            for r in found:
                r['cost'] = amount
            self.result = [(r['id'],) for r in found]
        elif sql.startswith('SELECT id, cost'):
            self.result = [(r['id'], r['cost']) for r in self.db.rows if r['id'] == params[0]]
        else:
            raise AssertionError('unexpected synthetic query: ' + sql)

    def fetchone(self):
        return self.result[0] if self.result else None

    def commit(self):
        pass

    def fetchall(self):
        return self.result

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def job(session_id=42, **changes):
    value = dict(session_id=session_id, car_id=7, observed_at=1767270660,
                 started_at=1767268860, geofence_id=2)
    value.update(changes)
    return value


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.db = SyntheticDatabase([session(car_id=7)])
        self.calls = []

    def check(self, jobs=None, transport=None, clock=lambda: 101):
        from automatic_billing import check_jobs
        def forbidden(*args):
            self.fail('unexpected HTTP request')
        return check_jobs(self.root, [job()] if jobs is None else jobs,
                          database_factory=lambda: self.db,
                          transport=transport or forbidden, clock=clock)

    def test_live_home_and_existing_cost_are_protected_without_fetch(self):
        self.db.rows = [session(car_id=7, geofence_id=1),
                        session(id=43, car_id=7, cost=Decimal('0'))]
        self.assertEqual(self.check([job(), job(43)]), {42: 'protected', 43: 'protected'})
        sql, params = self.db.queries[1]
        self.assertIn('cp.id IN (%s,%s)', sql)
        self.assertEqual(params, (42, 43))
        self.assertEqual(self.db.queries[0], ('SET TRANSACTION READ ONLY', ()))
        self.assertTrue(all(c.closed for c in self.db.connections))
        self.assertEqual(self.db.backups, [])
        self.assertFalse((self.root / 'manual-budget.sqlite3').exists())

    def test_shared_import_lock_prevents_even_database_read(self):
        with import_lock(self.root / 'ledger.sqlite3'):
            self.assertEqual(self.check(), {42: 'budget_paused'})
        self.assertEqual(self.db.queries, [])

    def test_wrong_car_and_incomplete_sessions_require_review_without_fetch(self):
        self.db.rows = [session(car_id=9), session(id=43, car_id=7, end_date=None)]
        self.assertEqual(self.check([job(), job(43)]), {42: 'manual_review', 43: 'manual_review'})

    def test_pending_eligible_target_requires_valid_own_token_before_budget(self):
        self.assertEqual(self.check(), {42: 'auth_blocked'})
        self.assertFalse((self.root / 'manual-budget.sqlite3').exists())
        runtime.save_tokens(self.root, dict(access_token='synthetic', refresh_token='synthetic-refresh', expires_in=30), clock=lambda: 1)
        self.assertEqual(self.check(), {42: 'auth_blocked'})
        self.assertFalse((self.root / 'manual-budget.sqlite3').exists())

    def signed_in(self):
        runtime.save_tokens(self.root, dict(access_token='synthetic', refresh_token='synthetic-refresh', expires_in=3600), clock=lambda: 1)

    def response(self, records, **metadata):
        def transport(*args):
            self.calls.append(args)
            return 200, json.dumps({'response': dict(data=records, **metadata)}).encode()
        return transport

    def test_imports_only_due_target_but_publishes_all_rows(self):
        from ledger_store import LedgerStore
        self.signed_in()
        self.db.rows.append(session(id=43, car_id=7, vin='SECOND-SYNTHETIC'))
        result = self.check(transport=self.response([bill(), bill(sessionId=102, vin='SECOND-SYNTHETIC')]))
        self.assertEqual(result, {42: 'imported'})
        self.assertEqual([r['cost'] for r in self.db.rows], [Decimal('8'), None])
        self.assertEqual(len(self.db.backups), 1)
        self.assertEqual(self.calls, [('https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/dx/charging/history?pageSize=50', None,
                                      {'Authorization': 'Bearer synthetic'}, 20)])
        store = LedgerStore(self.root / 'ledger.sqlite3')
        self.assertEqual([e['session_id'] for e in store.events()], [42, 42])
        self.assertEqual({r['session_id']:r['status'] for r in store.snapshot()['rows']}, {42:'imported',43:'ready'})
        self.assertEqual((self.root / 'automatic-history.json').stat().st_mode & 0o777, 0o600)

    def test_reads_second_page_when_target_not_yet_corroborated(self):
        self.signed_in()
        pages = [[bill(sessionId=1000+i, vin='OTHER') for i in range(50)], [bill()]]
        def transport(*args):
            index = len(self.calls)
            self.calls.append(args)
            return 200, json.dumps({'response': {'data': pages[index], 'totalResults': 51}}).encode()
        self.assertEqual(self.check(transport=transport), {42: 'imported'})
        self.assertEqual([c[0] for c in self.calls], [
            'https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/dx/charging/history?pageSize=50',
            'https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/dx/charging/history?pageSize=50&pageNo=1'])
        self.assertEqual(len(json.loads((self.root / 'automatic-history.json').read_text())), 51)

    def test_identical_duplicate_billing_ids_are_deduplicated(self):
        self.signed_in()
        self.assertEqual(self.check(transport=self.response([bill(), bill()])), {42: 'imported'})
        self.assertEqual(len(json.loads((self.root / 'automatic-history.json').read_text())), 1)

    def test_uncertain_prior_operation_stops_before_billing_lookup(self):
        from ledger_store import LedgerStore
        store = LedgerStore(self.root / 'ledger.sqlite3')
        store.record_event('101', 42, '8.00', 'uncertain', 'synthetic unknown commit')
        self.signed_in()
        self.assertEqual(self.check(), {42: 'manual_review'})
        self.assertFalse((self.root / 'manual-budget.sqlite3').exists())

    def test_verified_own_import_is_reported_without_another_request(self):
        self.signed_in()
        self.assertEqual(self.check(transport=self.response([bill()])), {42: 'imported'})
        self.assertEqual(self.check(), {42: 'imported'})
        self.assertEqual(len(self.calls), 1)
        self.db.rows[0]['cost'] = Decimal('9')
        self.assertEqual(self.check(), {42: 'conflict'})

    def test_receipt_window_mismatch_is_not_billing_authorization(self):
        self.signed_in()
        self.assertEqual(self.check([job(observed_at=1767357060)], transport=self.response([bill()])), {42:'manual_review'})
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / 'manual-budget.sqlite3').exists())

    def test_invalid_backup_never_authorizes_writes(self):
        self.signed_in()
        def bad_backup(path):
            self.db.backups.append(path)
            path.write_bytes(b'not-a-dump')
            path.chmod(0o600)
            return path
        self.db.backup = bad_backup
        self.assertEqual(self.check(transport=self.response([bill()])), {42: 'manual_review'})
        self.assertIsNone(self.db.rows[0]['cost'])
        self.assertFalse(any(sql.startswith('UPDATE ') for sql, _ in self.db.queries))

    def test_full_candidate_set_prevents_ambiguous_target_import(self):
        self.signed_in()
        self.db.rows.append(session(id=43, car_id=7))
        self.assertEqual(self.check(transport=self.response([bill()])), {42: 'conflict'})
        self.assertEqual(self.db.backups, [])
        self.assertTrue(all(r['cost'] is None for r in self.db.rows))

    def test_home_change_during_backup_is_protected_not_retried(self):
        self.signed_in()
        backup = self.db.backup
        def moved_home(path):
            result = backup(path)
            self.db.rows[0]['geofence_id'] = 1
            return result
        self.db.backup = moved_home
        self.assertEqual(self.check(transport=self.response([bill()])), {42: 'protected'})
        self.assertFalse(any(sql.startswith('UPDATE ') for sql, _ in self.db.queries))

    def test_missing_bill_stops_at_three_pages_without_claiming_completeness(self):
        self.signed_in()
        def transport(*args):
            page = len(self.calls)
            self.calls.append(args)
            return 200, json.dumps({'response': {'data': [bill(sessionId=1000+page*50+i, vin='OTHER') for i in range(50)]}}).encode()
        self.assertEqual(self.check(transport=transport), {42: 'waiting'})
        self.assertEqual(len(self.calls), 3)
        self.assertTrue(self.calls[-1][0].endswith('&pageNo=2'))
        self.assertEqual(len(json.loads((self.root / 'automatic-history.json').read_text())), 150)
        self.assertEqual(self.db.backups, [])

    def test_partial_page_failure_preserves_previous_history_without_any_import(self):
        self.signed_in()
        runtime._atomic_json(self.root, 'automatic-history.json', [{'previous': True}])
        self.db.rows.append(session(id=43, car_id=7, vin='MISSING-SYNTHETIC'))
        def transport(*args):
            page = len(self.calls)
            self.calls.append(args)
            if page:
                return 503, b'private-error'
            return 200, json.dumps({'data': [bill()] + [bill(sessionId=1000+i, vin='OTHER') for i in range(49)]}).encode()
        self.assertEqual(self.check([job(), job(43)], transport=transport), {42:'budget_paused',43:'budget_paused'})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(json.loads((self.root / 'automatic-history.json').read_text()), [{'previous':True}])
        self.assertEqual(self.db.backups, [])
        self.assertTrue(all(r['cost'] is None for r in self.db.rows))

    def test_conflicting_revision_across_pages_rejects_entire_batch(self):
        self.signed_in()
        first = [bill(sessionId=1000+i, vin='OTHER') for i in range(50)]
        changed = copy.deepcopy(first[0]); changed['siteLocationName'] = 'changed'
        pages = [first, [changed, bill()]]
        def transport(*args):
            index = len(self.calls)
            self.calls.append(args)
            return 200, json.dumps({'data':pages[index]}).encode()
        self.assertEqual(self.check(transport=transport), {42:'conflict'})
        self.assertFalse((self.root / 'automatic-history.json').exists())
        self.assertEqual(self.db.backups, [])

    def test_http_auth_failure_consumes_one_reservation_without_refresh_or_retry(self):
        import sqlite3
        self.signed_in()
        def transport(*args):
            self.calls.append(args)
            return 401, b'private-auth-error'
        self.assertEqual(self.check(transport=transport), {42:'auth_blocked'})
        self.assertEqual(len(self.calls), 1)
        with sqlite3.connect(self.root / 'manual-budget.sqlite3') as db:
            self.assertEqual(dict(db.execute('SELECT kind,used FROM allowance')), {'day':1,'month':1})
        self.assertFalse((self.root / 'automatic-history.json').exists())

    def test_combined_manual_budget_blocks_automatic_fetch(self):
        from manual_reader import reserve_manual
        self.signed_in()
        for _ in range(4):
            reserve_manual(self.root, clock=lambda:101)
        self.assertEqual(self.check(), {42:'budget_paused'})
        self.assertEqual(self.calls, [])

    def test_unknown_location_can_import_but_unsupported_bill_waits(self):
        self.signed_in()
        self.db.rows[0]['geofence_id'] = None
        self.assertEqual(self.check(transport=self.response([bill()])), {42:'imported'})

    def test_pending_bill_waits_without_backup(self):
        self.signed_in()
        self.assertEqual(self.check(transport=self.response([bill(invoices=[])])), {42:'waiting'})
        self.assertEqual(self.db.backups, [])

    def test_target_identity_change_after_backup_prevents_write(self):
        self.signed_in()
        backup = self.db.backup
        def moved_car(path):
            result = backup(path)
            self.db.rows[0]['car_id'] = 8
            return result
        self.db.backup = moved_car
        self.assertEqual(self.check(transport=self.response([bill()])), {42:'conflict'})
        self.assertIsNone(self.db.rows[0]['cost'])

    def test_unknown_commit_requires_manual_review_and_never_retries(self):
        self.signed_in()
        original_connect = self.db.connect
        def connect():
            conn = original_connect()
            def uncertain():
                raise OSError('synthetic private connection failure')
            conn.commit = uncertain
            return conn
        self.db.connect = connect
        self.assertEqual(self.check(transport=self.response([bill()])), {42:'manual_review'})
        self.assertEqual(self.check(), {42:'manual_review'})
        self.assertEqual(len(self.calls), 1)

    def test_target_repository_refuses_non_due_apply(self):
        from automatic_billing import TargetRepository
        repo = TargetRepository(self.db, {42:self.db.rows[0]})
        self.assertEqual(repo.apply({'session_id':43, 'status':'ready'})['status'], 'protected')
        self.assertEqual(self.db.queries, [])

    def test_oversized_and_duplicate_job_batches_do_not_open_database(self):
        self.assertEqual(self.check([job(i) for i in range(1,22)]), dict.fromkeys(range(1,22), 'manual_review'))
        self.assertEqual(self.check([job(), job()]), {42:'manual_review'})
        self.assertEqual(self.db.queries, [])

    def test_own_import_with_changed_energy_is_not_claimed_verified(self):
        self.signed_in()
        self.assertEqual(self.check(transport=self.response([bill()])), {42:'imported'})
        self.db.rows[0]['charge_energy_used'] = Decimal('99')
        self.assertEqual(self.check(), {42:'conflict'})
        self.assertEqual(len(self.calls), 1)

    def test_duplicate_revision_cannot_change_paid_boolean_into_number(self):
        self.signed_in()
        revised = bill()
        revised['fees'][0]['isPaid'] = 1
        self.assertEqual(self.check(transport=self.response([bill(), revised])), {42:'conflict'})
        self.assertIsNone(self.db.rows[0]['cost'])

    def test_decimal_precision_is_preserved_in_private_history(self):
        self.signed_in()
        payload = json.dumps({'data':[bill()]}).replace('"usageBase": "20"', '"usageBase": 20.000000000000000001').encode()
        self.assertEqual(self.check(transport=lambda *args:(200,payload)), {42:'imported'})
        saved = json.loads((self.root / 'automatic-history.json').read_text())
        self.assertEqual(saved[0]['fees'][0]['usageBase'], '20.000000000000000001')

    def test_bad_schema_or_oversized_page_never_saves_or_imports(self):
        self.signed_in()
        for body in (b'{"response":{"data":{}}}', b'{"data":[1]}', b'{"data":[],"totalResults":true}',
                     json.dumps({'data':[bill(sessionId=i) for i in range(51)]}).encode()):
            with self.subTest(body=body[:30]):
                self.assertEqual(self.check(transport=lambda *args:(200,body)), {42:'manual_review'})
                self.assertFalse((self.root / 'automatic-history.json').exists())
        self.assertEqual(self.db.backups, [])

    def test_tuple_target_rows_are_supported(self):
        from automatic_billing import TARGET_COLUMNS
        self.db.rows[0]['geofence_id'] = 1
        original_connect = self.db.connect
        def connect():
            conn = original_connect()
            original_fetchall = conn.fetchall
            conn.fetchall = lambda: [tuple(r[k] for k in TARGET_COLUMNS) for r in original_fetchall()]
            return conn
        self.db.connect = connect
        self.assertEqual(self.check(), {42:'protected'})

    def test_budget_exhaustion_between_pages_does_not_save_partial_batch(self):
        from manual_reader import reserve_manual
        self.signed_in()
        for _ in range(3):
            reserve_manual(self.root, clock=lambda:101)
        self.assertEqual(self.check(transport=self.response([bill(sessionId=i, vin='OTHER') for i in range(50)])), {42:'budget_paused'})
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.root / 'automatic-history.json').exists())

    def test_empty_jobs_do_not_open_state_database_or_transport(self):
        self.assertIsNotNone(importlib.util.find_spec('automatic_billing'))
        from automatic_billing import check_jobs
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / 'not-created'
            def forbidden(*args, **kwargs):
                self.fail('empty queue performed work')
            self.assertEqual(check_jobs(root, [], database_factory=forbidden, transport=forbidden), {})
            self.assertFalse(root.exists())
