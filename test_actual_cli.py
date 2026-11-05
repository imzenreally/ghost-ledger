"""Cached-only CLI correction tests with exact target scope."""
import contextlib
import io
import json
import unittest
from unittest.mock import patch
from decimal import Decimal
import ledger
import test_ledger as fixtures
from charging_costs.test_importer import bill, session
from test_automatic_billing import SyntheticDatabase
from auth import runtime


class ActualCliTests(unittest.TestCase):
    setUp = fixtures.LedgerCliTests.setUp
    invoke = fixtures.LedgerCliTests.invoke

    def test_cached_scoped_correction_previews_then_applies_without_fetch(self):
        runtime._atomic_json(self.root, self.history.name,
                             [bill(), bill(sessionId=102, vin='OTHER-SYNTHETIC')])
        db = SyntheticDatabase([session(cost=Decimal('7'), car_id=7),
                                session(id=43, cost=Decimal('6'), vin='OTHER-SYNTHETIC', car_id=7)])
        options = ['--prefer-tesla-bills', '--since', '2026-01-01T00:00:00Z', '--session-id', '42']
        with patch.object(ledger, 'fetch_latest', side_effect=AssertionError('no fetch')):
            self.assertEqual(self.invoke(['preview'] + options, database_factory=lambda: db), 0)
            preview = json.loads(self.output.getvalue())
            self.assertEqual([r['session_id'] for r in preview['sessions']], [42])
            self.assertEqual(preview['sessions'][0]['previous_cost'], '7')
            self.assertEqual(db.backups, [])
            self.output.seek(0); self.output.truncate()
            self.assertEqual(self.invoke(['import', '--apply'] + options, database_factory=lambda: db), 0)
            self.assertEqual(json.loads(self.output.getvalue())['imported'], 1)
        self.assertEqual([r['cost'] for r in db.rows], [Decimal('8'), Decimal('6')])
        self.assertEqual(len(db.backups), 1)

    def test_amount_history_projection_rejects_untrusted_values(self):
        row = ledger._safe_row({'status': 'ready', 'previous_cost': 'private-not-a-number',
                                'delta': {'private': 'value'}})
        self.assertNotIn('previous_cost', row)
        self.assertNotIn('delta', row)
        self.assertEqual(ledger._safe_row({'status': 'ready', 'previous_cost': None, 'delta': '-1.00'}),
                         {'status': 'ready', 'previous_cost': None, 'delta': '-1.00'})

    def test_invalid_policy_and_scope_rejected_before_any_io(self):
        for options in (['--prefer-tesla-bills'], ['--since', '2026-01-01T00:00:00Z'],
                        ['--prefer-tesla-bills', '--since', '2026-01-01'],
                        ['--prefer-tesla-bills', '--since', '2026-01-01T00:00:00Z', '--fetch'],
                        ['--session-id', '0'], ['--session-id', '-1']):
            with self.subTest(options=options):
                with self.assertRaises(SystemExit) as raised:
                    self.invoke(['preview'] + options, database_factory=lambda: self.fail('unexpected DB'))
                self.assertEqual(raised.exception.code, 2)
