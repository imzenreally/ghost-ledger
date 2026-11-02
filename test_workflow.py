"""Synthetic end-to-end orchestration tests; no production services."""
import copy
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from ledger_store import LedgerStore
from charging_costs.test_importer import bill, session


class WorkflowTests(unittest.TestCase):
    def test_import_is_journaled_and_visible_then_repeat_is_noop(self):
        from workflow import run_import
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp).resolve()/'state'/'ledger.sqlite3')
            repo = FakeRepository([session()])
            result = run_import([bill()], repo, store, apply=True)
            self.assertEqual(result['imported'], 1)
            self.assertEqual([e['outcome'] for e in store.events()], ['pending', 'imported'])
            self.assertEqual(store.snapshot()['rows'][0]['status'], 'imported')
            self.assertEqual(store.snapshot()['rows'][0]['current_cost'], '8.00')
            result = run_import([bill()], repo, store, apply=True)
            self.assertEqual(result['imported'], 0)
            self.assertEqual(repo.writes, 1)

    def test_preview_never_writes_and_flags_existing_disagreement(self):
        from workflow import run_import
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp).resolve()/'state'/'ledger.sqlite3')
            repo = FakeRepository([session(cost=Decimal('7.00'))])
            run_import([bill()], repo, store)
            row = store.snapshot()['rows'][0]
            self.assertEqual((row['status'], row['current_cost']), ('conflict', '7.00'))
            self.assertEqual(repo.writes, 0)

    def test_weak_match_is_conflict_not_ready(self):
        from workflow import run_import
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp).resolve()/'state'/'ledger.sqlite3')
            repo = FakeRepository([session(cost=Decimal('7.00'), charge_energy_used='15')])
            run_import([bill()], repo, store, apply=True)
            row = store.snapshot()['rows'][0]
            self.assertEqual(row['status'], 'conflict')
            self.assertIn('corroboration', row['reason'])
            self.assertEqual(repo.writes, 0)

    def test_exception_is_uncertain_not_retried(self):
        from workflow import run_import
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp).resolve()/'state'/'ledger.sqlite3')
            repo = FakeRepository([session()]); repo.fail = True
            result = run_import([bill()], repo, store, apply=True)
            self.assertEqual(result['uncertain'], 1)
            self.assertEqual(repo.writes, 1)
            self.assertEqual([e['outcome'] for e in store.events()], ['pending', 'uncertain'])
            run_import([bill()], repo, store, apply=True)
            self.assertEqual(repo.writes, 1)
            self.assertEqual(store.snapshot()['rows'][0]['status'], 'conflict')

    def test_revised_bill_does_not_reuse_prior_import_claim(self):
        from workflow import run_import
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp).resolve()/'state'/'ledger.sqlite3')
            repo = FakeRepository([session()])
            run_import([bill()], repo, store, apply=True)
            changed = bill(); changed['fees'][0].update(totalDue='9.00', totalBase='9.00', netDue='9.00')
            run_import([changed], repo, store, apply=True)
            self.assertEqual(repo.writes, 1)
            self.assertEqual(store.snapshot()['rows'][0]['status'], 'conflict')


class FakeRepository:
    def __init__(self, sessions):
        self.sessions = copy.deepcopy(sessions); self.writes = 0; self.fail = False
    def sessions_for(self, history):
        return copy.deepcopy(self.sessions)
    def apply(self, row):
        self.writes += 1
        if self.fail:
            raise ConnectionError('synthetic unknown commit')
        target = next(s for s in self.sessions if s['id'] == row['session_id'])
        target['cost'] = Decimal(row['amount'])
        return {'status':'imported', 'external_id':row['external_id'], 'session_id':row['session_id'],
                'reason':'Synthetic verified commit', 'readback':{'session_id':row['session_id'], 'cost':row['amount']}}
