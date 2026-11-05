"""Policy callback integration with synthetic transport/DB (no Fleet access)."""
import json
import unittest
from decimal import Decimal
from unittest.mock import patch
import test_automatic_billing as fixtures
from charging_costs.test_importer import bill, session


class ActualAutomaticTests(unittest.TestCase):
    setUp = fixtures.AutomaticTests.setUp
    check = fixtures.AutomaticTests.check
    signed_in = fixtures.AutomaticTests.signed_in
    response = fixtures.AutomaticTests.response

    def test_opt_in_replaces_only_due_target_and_never_idle_polls(self):
        self.signed_in()
        self.db.rows = [session(cost=Decimal('7'), car_id=7),
                        session(id=43, cost=Decimal('6'), car_id=7, vin='OTHER-SYNTHETIC')]
        with patch.dict('os.environ', {'GHOST_LEDGER_PREFER_TESLA_BILLS': 'true',
                                     'GHOST_LEDGER_TESLA_BILLS_SINCE': '2026-01-01T00:00:00Z'}):
            self.assertEqual(self.check(transport=self.response([bill(), bill(sessionId=102, vin='OTHER-SYNTHETIC')])),
                             {42: 'imported'})
            self.assertEqual([r['cost'] for r in self.db.rows], [Decimal('8'), Decimal('6')])
            self.assertEqual(self.check(jobs=[]), {})
            self.assertEqual(self.check(), {42: 'imported'})
            self.assertEqual(len(self.calls), 1)

    def test_old_existing_cost_never_fetches_even_when_opted_in(self):
        self.db.rows[0]['cost'] = Decimal('7')
        with patch.dict('os.environ', {'GHOST_LEDGER_PREFER_TESLA_BILLS': 'true',
                                     'GHOST_LEDGER_TESLA_BILLS_SINCE': '2026-01-02T00:00:00Z'}):
            self.assertEqual(self.check(), {42: 'protected'})
            self.assertEqual(self.calls, [])

    def test_worker_rejects_malformed_policy_before_startup(self):
        import billing_worker
        with patch.dict('os.environ', {'GHOST_LEDGER_PREFER_TESLA_BILLS': 'yes'}):
            with patch('charging_detector.Detector', side_effect=AssertionError('unsafe startup')):
                with self.assertRaises(ValueError):
                    billing_worker.run(self.root)

    def test_malformed_policy_fails_closed_before_request_or_db(self):
        with patch.dict('os.environ', {'GHOST_LEDGER_PREFER_TESLA_BILLS': 'yes'}):
            self.assertEqual(self.check(), {42: 'manual_review'})
        self.assertEqual(self.db.queries, [])
