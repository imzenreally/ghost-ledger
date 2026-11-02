import copy
import json
import unittest
from decimal import Decimal

from charging_costs.importer import build_preview


def bill(**changes):
    value = {
        'sessionId': 101, 'vin': 'SYNTHETICVIN', 'siteLocationName': 'Test station',
        'chargeStartDateTime': '2026-01-01T12:00:00Z',
        'chargeStopDateTime': '2026-01-01T12:30:00Z',
        'unlatchDateTime': '2026-01-01T12:31:00Z',
        'countryCode': 'US', 'billingType': 'PAID',
        'invoices': [{'fileName': 'test.pdf', 'contentId': 'test-invoice', 'invoiceType': 'CHARGING'}],
        'fees': [{'sessionFeeId': 201, 'feeType': 'CHARGING', 'currencyCode': 'USD',
                  'pricingType': 'FIXED', 'uom': 'kwh', 'usageBase': '20',
                  'totalBase': '8.00', 'totalDue': '8.00', 'netDue': '8.00',
                  'isPaid': True, 'status': 'PAID'}],
    }
    value.update(changes)
    return value


def session(**changes):
    value = {'id': 42, 'vin': 'SYNTHETICVIN', 'start_date': '2026-01-01 12:00:00',
             'end_date': '2026-01-01 12:30:00', 'charge_energy_used': Decimal('20'),
             'charge_energy_added': Decimal('18'), 'cost': None, 'geofence_id': 2}
    value.update(changes)
    return value


class ImporterTests(unittest.TestCase):
    def test_paid_charger_energy_match_is_json_safe_without_vin(self):
        row = build_preview({'response': {'data': [bill()]}}, [session()])[0]
        self.assertEqual((row['status'], row['session_id'], row['amount']), ('ready', 42, '8.00'))
        self.assertEqual(row['external_id'], '101')
        self.assertEqual(row['started_at'], '2026-01-01T12:00:00Z')
        self.assertNotIn('SYNTHETICVIN', json.dumps(row))
        self.assertEqual(row['evidence']['billed_kwh'], '20')

    def test_pending_records_never_ready(self):
        for change in ({'invoices': []}, {'fees': [dict(bill()['fees'][0], isPaid=False)]},
                       {'fees': [dict(bill()['fees'][0], status='PENDING')]}):
            with self.subTest(change=change):
                self.assertEqual(build_preview([bill(**change)], [session()])[0]['status'], 'pending')

    def test_invalid_billing_fails_closed(self):
        cases = [dict(totalDue='NaN'), dict(totalDue='-1'), dict(totalDue='1.001'),
                 dict(currencyCode='EUR'), dict(feeType='IDLE'), dict(netDue='-1'),
                 dict(uom='min'), dict(usageBase=None), dict(totalBase='7'),
                 dict(totalTier1='Infinity'), dict(usageTier1='-1')]
        records = [bill(fees=[dict(bill()['fees'][0], **change)]) for change in cases]
        records += [bill(vin=''), bill(sessionId=True), bill(chargeStartDateTime='2026-01-01T12:00:00'),
                    bill(billingType='REFUND'), bill(invoices=[{'invoiceType': 'CREDIT_NOTE', 'fileName': 'a', 'contentId': 'b'}])]
        for record in records:
            with self.subTest(record=record):
                self.assertEqual(build_preview([record], [session()])[0]['status'], 'invalid')

    def test_congestion_total_zero_tiers_and_net_due_not_double_counted(self):
        charging = dict(bill()['fees'][0], totalDue='0', totalBase='0', netDue='0', usageTier1=None)
        congestion = dict(charging, sessionFeeId=202, feeType='CONGESTION', uom='min',
                          usageBase='5', totalDue='2.00', totalBase='2.00', netDue='1.50')
        row = build_preview([bill(fees=[charging, congestion])], [session()])[0]
        self.assertEqual((row['status'], row['amount']), ('ready', '2.00'))

    def test_collisions_fail_closed_including_duplicate_invalid_record(self):
        for records in ([bill(), bill()], [bill(), bill(sessionId=102)],
                        [bill(), bill(fees=[]) ]):
            rows = build_preview(records, [session()])
            self.assertTrue(all(row['status'] == 'conflict' for row in rows))

    def test_matching_boundaries_and_protection(self):
        for changes, expected in [({'cost': '0'}, 'protected'), ({'cost': '8.00'}, 'protected'),
                ({'geofence_id': 1}, 'unmatched'), ({'geofence_id': '1'}, 'unmatched'),
                ({'vin': 'OTHER'}, 'unmatched'), ({'charge_energy_used': '18'}, 'unmatched'),
                ({'charge_energy_used': None}, 'unmatched'),
                ({'start_date': '2026-01-01 12:05:00', 'end_date': '2026-01-01 12:31:00',
                  'charge_energy_used': '20.4'}, 'ready'),
                ({'start_date': '2026-01-01 12:05:01'}, 'unmatched'),
                ({'end_date': '2026-01-01 12:31:01'}, 'unmatched'),
                ({'charge_energy_used': '20.401'}, 'unmatched')]:
            with self.subTest(changes=changes):
                self.assertEqual(build_preview([bill()], [session(**changes)])[0]['status'], expected)
        self.assertEqual(build_preview([bill()], [session(), session(id=43)])[0]['status'], 'ambiguous')

    def test_invalid_wrapper_is_explicit_error(self):
        for history in ({}, {'data': {}}, [bill()] * 1001):
            with self.assertRaises(ValueError):
                build_preview(history, [])

    def test_pending_bill_cannot_share_target_with_ready_bill(self):
        records = [bill(), bill(sessionId=102, invoices=[])]
        self.assertTrue(all(r['status'] == 'conflict' for r in build_preview(records, [session()])))

    def test_extreme_and_malformed_values_are_invalid_not_exceptions(self):
        for change in ({'totalDue': '1e999999'}, {'uom': None}, {'usageBase': True}):
            record = bill(fees=[dict(bill()['fees'][0], **change)])
            self.assertEqual(build_preview([record], [session()])[0]['status'], 'invalid')

    def test_ambiguous_bill_cannot_leave_shared_target_ready(self):
        # Bill 101 matches both; shifted bill 102 matches only session 42.
        records = [bill(), bill(sessionId=102, chargeStartDateTime='2026-01-01T12:05:00Z')]
        sessions = [session(), session(id=43, start_date='2026-01-01 11:55:00')]
        self.assertTrue(all(row['status'] == 'conflict' for row in build_preview(records, sessions)))


if __name__ == '__main__':
    unittest.main()
