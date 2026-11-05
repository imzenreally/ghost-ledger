"""Synthetic authoritative billing policy tests; no external requests."""
import unittest
from decimal import Decimal
from charging_costs.importer import build_preview
from charging_costs.test_importer import bill, session


class ActualCostTests(unittest.TestCase):
    def test_policy_boundaries_and_protections(self):
        from charging_costs.policy import TeslaBillPolicy, from_environment
        self.assertIsNone(from_environment({}))
        for env in ({'GHOST_LEDGER_PREFER_TESLA_BILLS': 'TRUE'},
                    {'GHOST_LEDGER_PREFER_TESLA_BILLS': 'true'},
                    {'GHOST_LEDGER_TESLA_BILLS_SINCE': '2026-01-01T00:00:00Z'},
                    {'GHOST_LEDGER_PREFER_TESLA_BILLS': 'true', 'GHOST_LEDGER_TESLA_BILLS_SINCE': '2026-01-01'}):
            with self.assertRaises(ValueError):
                from_environment(env)
        for cost in (None, Decimal('0'), Decimal('7')):
            with self.subTest(cost=cost):
                for cutoff, expected in (('2026-01-01T12:00:00Z', 'ready'),
                                         ('2026-01-01T12:00:00.000001Z', 'protected')):
                    row = build_preview([bill()], [session(cost=cost)], policy=TeslaBillPolicy(cutoff))[0]
                    self.assertEqual(row['status'], expected)
        policy = TeslaBillPolicy('2026-01-01T00:00:00Z')
        for raw, target, expected in ((bill(), session(cost=Decimal('8')), 'protected'),
                                      (bill(), session(geofence_id=1, cost=Decimal('7')), 'unmatched'),
                                      (bill(invoices=[]), session(cost=Decimal('7')), 'pending'),
                                      (bill(), session(cost=Decimal('NaN')), 'invalid')):
            self.assertEqual(build_preview([raw], [target], policy=policy)[0]['status'], expected)
        self.assertEqual(build_preview([bill()], [session(cost=Decimal('7'))])[0]['status'], 'protected')


    def test_historical_discrepancy_is_visible_but_apply_never_writes(self):
        import tempfile
        from pathlib import Path
        from ledger_store import LedgerStore
        from test_workflow import FakeRepository
        from workflow import run_import
        from charging_costs.policy import TeslaBillPolicy
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp).resolve() / 'ledger.sqlite3')
            repo = FakeRepository([session(cost=Decimal('7'))])
            report = run_import([bill()], repo, store, apply=True, allowed_session_ids={42},
                                policy=TeslaBillPolicy('2026-01-02T00:00:00Z'))
            row = store.snapshot()['rows'][0]
            self.assertEqual((report['imported'], repo.writes), (0, 0))
            self.assertEqual((row['status'], row['previous_cost'], row['amount'], row['delta']),
                             ('conflict', '7', '8.00', '1.00'))
            self.assertIn('preview only', row['reason'])
            self.assertEqual(store.events(), [])

    def test_other_billing_reference_pending_or_uncertain_blocks_same_session(self):
        import tempfile
        from pathlib import Path
        from ledger_store import LedgerStore
        from test_workflow import FakeRepository
        from workflow import run_import
        from charging_costs.policy import TeslaBillPolicy
        for outcome in ('pending', 'uncertain'):
            with tempfile.TemporaryDirectory() as tmp:
                store = LedgerStore(Path(tmp).resolve() / 'ledger.sqlite3')
                store.record_event('other-reference', 42, '7', outcome)
                repo = FakeRepository([session(cost=Decimal('7'))])
                result = run_import([bill()], repo, store, apply=True,
                                    policy=TeslaBillPolicy('2026-01-01T00:00:00Z'))
                self.assertEqual(result['imported'], 0)
                self.assertEqual(repo.writes, 0)

    def test_opt_in_replaces_existing_estimate_with_snapshot(self):
        from charging_costs.policy import TeslaBillPolicy
        policy = TeslaBillPolicy(since='2026-01-01T00:00:00+00:00')
        row = build_preview([bill()], [session(cost=Decimal('7'))], policy=policy)[0]
        self.assertEqual(row['status'], 'ready')
        self.assertEqual((row['previous_cost'], row['amount'], row['delta']), ('7', '8.00', '1.00'))
        self.assertTrue(row['evidence']['session_snapshot'])

    def test_workflow_audits_amount_history_and_repeat_is_noop(self):
        import json
        import tempfile
        from pathlib import Path
        from charging_costs.policy import TeslaBillPolicy
        from ledger_store import LedgerStore
        from test_workflow import FakeRepository
        from workflow import run_import
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp).resolve() / 'ledger.sqlite3')
            repo = FakeRepository([session(cost=Decimal('7'))])
            policy = TeslaBillPolicy('2026-01-01T00:00:00Z')
            result = run_import([bill()], repo, store, apply=True, policy=policy, allowed_session_ids={42})
            self.assertEqual(result['imported'], 1)
            event = json.loads(store.events()[-1]['detail'])
            self.assertEqual((event['old_cost'], event['new_cost'], event['delta'], event['billing_ref']),
                             ('7', '8.00', '1.00', '101'))
            self.assertEqual(run_import([bill()], repo, store, apply=True, policy=policy)['imported'], 0)
            self.assertEqual(repo.writes, 1)

    def test_independent_verification_failure_is_uncertain_and_blocks_retry(self):
        import tempfile
        from pathlib import Path
        from ledger_store import LedgerStore
        from test_workflow import FakeRepository
        from workflow import run_import
        from charging_costs.policy import TeslaBillPolicy
        class Unverified(FakeRepository):
            def apply(self, row):
                result = super().apply(row)
                self.sessions[0]['cost'] = Decimal('9')
                return result
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp).resolve() / 'ledger.sqlite3')
            repo = Unverified([session(cost=Decimal('7'))])
            policy = TeslaBillPolicy('2026-01-01T00:00:00Z')
            report = run_import([bill()], repo, store, apply=True, policy=policy)
            self.assertEqual((report['imported'], report['uncertain']), (0, 1))
            run_import([bill()], repo, store, apply=True, policy=policy)
            self.assertEqual(repo.writes, 1)
