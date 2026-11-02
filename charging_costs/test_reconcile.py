import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from reconcile import preview


def make_bill(**overrides):
    bill = {
        'external_id': 'bill-1',
        'vin': 'VIN1',
        'started_at': datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        'amount': Decimal('10.00'),
        'currency': 'USD',
        'finalized': True,
    }
    bill.update(overrides)
    return bill


def make_session(**overrides):
    session = {
        'id': 1,
        'vin': 'VIN1',
        'started_at': datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        'cost': None,
    }
    session.update(overrides)
    return session


class ReconcilePreviewTests(unittest.TestCase):
    def test_pending_when_not_finalized(self):
        bill = make_bill(finalized=False)
        session = make_session()
        result = preview(bill, [session])
        self.assertEqual(result, {'status': 'pending', 'session_id': None, 'amount': None})

    def test_currency_mismatch(self):
        bill = make_bill(currency='EUR')
        session = make_session()
        result = preview(bill, [session], currency='USD')
        self.assertEqual(result, {'status': 'currency_mismatch', 'session_id': None, 'amount': None})

    def test_no_match_when_no_same_vin(self):
        bill = make_bill()
        session = make_session(vin='OTHERVIN')
        result = preview(bill, [session])
        self.assertEqual(result, {'status': 'no_match', 'session_id': None, 'amount': None})

    def test_no_match_when_too_far(self):
        bill = make_bill()
        session = make_session(started_at=datetime(2026, 1, 1, 12, 6, 0, tzinfo=timezone.utc))
        result = preview(bill, [session], tolerance_seconds=300)
        self.assertEqual(result, {'status': 'no_match', 'session_id': None, 'amount': None})

    def test_ambiguous_when_multiple_qualifying_sessions(self):
        bill = make_bill()
        sessions = [
            make_session(id=1, started_at=datetime(2026, 1, 1, 12, 0, 10, tzinfo=timezone.utc)),
            make_session(id=2, started_at=datetime(2026, 1, 1, 12, 0, 20, tzinfo=timezone.utc)),
        ]
        result = preview(bill, sessions, tolerance_seconds=300)
        self.assertEqual(result, {'status': 'ambiguous', 'session_id': None, 'amount': None})

    def test_protected_existing_nonzero_cost(self):
        bill = make_bill()
        session = make_session(cost=Decimal('1.00'))
        result = preview(bill, [session])
        self.assertEqual(result, {'status': 'protected_existing', 'session_id': None, 'amount': None})

    def test_protected_existing_zero_cost(self):
        bill = make_bill()
        session = make_session(cost=Decimal('0'))
        result = preview(bill, [session])
        self.assertEqual(result, {'status': 'protected_existing', 'session_id': None, 'amount': None})

    def test_proposed_unique_match(self):
        bill = make_bill()
        session = make_session(id=42)
        result = preview(bill, [session])
        self.assertEqual(result, {'status': 'proposed', 'session_id': 42, 'amount': Decimal('10.00')})

    def test_proposed_zero_bill_amount(self):
        bill = make_bill(amount=Decimal('0.00'))
        session = make_session(id=43)
        result = preview(bill, [session])
        self.assertEqual(result, {'status': 'proposed', 'session_id': 43, 'amount': Decimal('0.00')})

    def test_inclusive_tolerance_at_exact_boundary(self):
        bill = make_bill()
        session = make_session(id=7, started_at=datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc))
        result = preview(bill, [session], tolerance_seconds=300)
        self.assertEqual(result, {'status': 'proposed', 'session_id': 7, 'amount': Decimal('10.00')})

    def test_zero_tolerance_matches_exact_time_only(self):
        bill = make_bill()
        exact = make_session(id=1, started_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc))
        near = make_session(id=2, started_at=datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(preview(bill, [exact], tolerance_seconds=0)['status'], 'proposed')
        self.assertEqual(preview(bill, [near], tolerance_seconds=0)['status'], 'no_match')

    def test_non_usd_currency_supported(self):
        bill = make_bill(currency='EUR')
        session = make_session()
        result = preview(bill, [session], currency='EUR')
        self.assertEqual(result, {'status': 'proposed', 'session_id': 1, 'amount': Decimal('10.00')})

    def test_duplicate_session_ids_raise(self):
        bill = make_bill()
        sessions = [
            make_session(id=1),
            make_session(id=1, started_at=datetime(2026, 1, 1, 13, 0, 0, tzinfo=timezone.utc)),
        ]
        with self.assertRaises(ValueError):
            preview(bill, sessions)

    def test_inputs_unchanged(self):
        bill = make_bill()
        session = make_session()
        bill_snapshot = dict(bill)
        session_snapshot = dict(session)
        sessions = [session]
        preview(bill, sessions, currency='USD', tolerance_seconds=300)
        self.assertEqual(bill, bill_snapshot)
        self.assertEqual(session, session_snapshot)

    def test_missing_bill_field_raises(self):
        bill = make_bill()
        del bill['external_id']
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_blank_external_id_raises(self):
        bill = make_bill(external_id='   ')
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_blank_vin_raises(self):
        bill = make_bill(vin='')
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_naive_bill_started_at_raises(self):
        bill = make_bill(started_at=datetime(2026, 1, 1, 12, 0, 0))
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_bill_amount_float_disallowed(self):
        bill = make_bill(amount=10.0)
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_bill_amount_bool_disallowed(self):
        bill = make_bill(amount=True)
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_bill_amount_int_disallowed(self):
        bill = make_bill(amount=10)
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_bill_amount_nan_raises(self):
        bill = make_bill(amount=Decimal('NaN'))
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_bill_amount_negative_raises(self):
        bill = make_bill(amount=Decimal('-0.01'))
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_blank_bill_currency_raises(self):
        bill = make_bill(currency='')
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_non_bool_finalized_raises(self):
        bill = make_bill(finalized='yes')
        with self.assertRaises(ValueError):
            preview(bill, [make_session()])

    def test_naive_session_started_at_raises(self):
        bill = make_bill()
        session = make_session(started_at=datetime(2026, 1, 1, 12, 0, 0))
        with self.assertRaises(ValueError):
            preview(bill, [session])

    def test_session_id_nonpositive_raises(self):
        bill = make_bill()
        session = make_session(id=0)
        with self.assertRaises(ValueError):
            preview(bill, [session])

    def test_session_id_float_disallowed(self):
        bill = make_bill()
        session = make_session(id=1.0)
        with self.assertRaises(ValueError):
            preview(bill, [session])

    def test_session_cost_bool_disallowed(self):
        bill = make_bill()
        session = make_session(cost=True)
        with self.assertRaises(ValueError):
            preview(bill, [session])

    def test_session_cost_float_disallowed(self):
        bill = make_bill()
        session = make_session(cost=0.5)
        with self.assertRaises(ValueError):
            preview(bill, [session])

    def test_tolerance_negative_raises(self):
        bill = make_bill()
        with self.assertRaises(ValueError):
            preview(bill, [make_session()], tolerance_seconds=-1)

    def test_tolerance_bool_raises(self):
        bill = make_bill()
        with self.assertRaises(ValueError):
            preview(bill, [make_session()], tolerance_seconds=True)

    def test_tolerance_nan_raises(self):
        bill = make_bill()
        with self.assertRaises(ValueError):
            preview(bill, [make_session()], tolerance_seconds=float('nan'))

    def test_tolerance_infinite_raises(self):
        bill = make_bill()
        with self.assertRaises(ValueError):
            preview(bill, [make_session()], tolerance_seconds=float('inf'))

    def test_too_many_sessions_raises(self):
        bill = make_bill()
        sessions = [make_session(id=i) for i in range(1, 1002)]
        with self.assertRaises(ValueError):
            preview(bill, sessions)


if __name__ == '__main__':
    unittest.main()