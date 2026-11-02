from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest
from auth import runtime
import manual_reader as reader


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        os.chmod(self.root, 0o700)
        runtime.save_tokens(self.root, {'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh', 'expires_in': 3600}, clock=lambda: 100)

    def test_one_official_page_decimal_saved_privately_without_touching_probe(self):
        runtime._reserve_probe(self.root)
        calls = []
        def transport(*args):
            calls.append(args)
            return 200, b'{"response":{"data":[{"amount":12.340000000000001}]}}'
        result = reader.fetch_latest(self.root / 'history.json', token_root=self.root, transport=transport, clock=lambda: 101)
        self.assertEqual(result['returned_sessions'], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], (runtime.HISTORY_URL, None, {'Authorization': 'Bearer fixture-access'}, 20))
        saved = reader.read_history(self.root / 'history.json')
        self.assertEqual(saved['response']['data'][0]['amount'], '12.340000000000001')
        self.assertEqual((self.root / 'history.json').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(runtime.BudgetExceeded):
            runtime._reserve_probe(self.root)

    def test_expired_tokens_do_not_consume_budget_or_send(self):
        calls = []
        with self.assertRaisesRegex(reader.ManualReaderError, 'sign-in required'):
            reader.fetch_latest(self.root / 'history.json', token_root=self.root,
                                transport=lambda *args: calls.append(args), clock=lambda: 3700)
        self.assertEqual(calls, [])
        self.assertFalse((self.root / 'manual-budget.sqlite3').exists())

    def test_bad_status_schema_and_transport_failure_each_consume_one_attempt(self):
        attempts = []
        runtime._atomic_json(self.root, 'history.json', [{'unchanged': True}])
        for response in ((503, b'sensitive-body'), (200, b'{"response":{"data":{}}}'),
                         (200, b'{"response":{"data":[4]}}'), (200, b'{"response":NaN}')):
            def transport(*args):
                attempts.append(args)
                return response
            with self.assertRaises(reader.ManualReaderError):
                reader.fetch_latest(self.root / 'history.json', token_root=self.root, transport=transport, clock=lambda: 101)
        self.assertEqual(len(attempts), 4)
        self.assertEqual(reader.read_history(self.root / 'history.json'), [{'unchanged': True}])
        with self.assertRaises(runtime.BudgetExceeded):
            reader.fetch_latest(self.root / 'history.json', token_root=self.root, transport=lambda *a: self.fail('exhausted'), clock=lambda: 101)
        # Restart and failed network still consume a reservation, without retry.
        runtime.save_tokens(self.root, {'access_token': 'fixture-new', 'refresh_token': 'fixture-refresh', 'expires_in': 3600}, clock=lambda: 86400)
        with self.assertRaisesRegex(reader.ManualReaderError, 'no retry'):
            reader.fetch_latest(self.root / 'history.json', token_root=self.root,
                                transport=lambda *args: (_ for _ in ()).throw(OSError('secret')),
                                clock=lambda: 101 + 86400)

    def test_utc_day_month_limits_restart_and_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime, timezone
        now = datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()
        def reserve(_):
            try:
                reader.reserve_manual(self.root, clock=lambda: now)
                return True
            except runtime.BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertEqual(sum(pool.map(reserve, range(20))), 4)
        # Fresh SQLite connections on each call exercise durable/restart behavior.
        for day in range(1, 25):
            for _ in range(4):
                reader.reserve_manual(self.root, clock=lambda day=day: now + day * 86400)
        with self.assertRaises(runtime.BudgetExceeded):
            reader.reserve_manual(self.root, clock=lambda: now + 25 * 86400)
        reader.reserve_manual(self.root, clock=lambda: datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp())

    def test_read_cached_decimal_and_reject_unsafe_files(self):
        path = self.root / 'history.json'
        path.write_bytes(b'[{"cost":1.234567890123456789}]')
        path.chmod(0o600)
        self.assertEqual(reader.read_history(path)[0]['cost'], Decimal('1.234567890123456789'))
        path.chmod(0o644)
        with self.assertRaises(ValueError): reader.read_history(path)
        link = self.root / 'link.json'
        link.symlink_to(path)
        with self.assertRaises(ValueError): reader.read_history(link)
        sidecar = self.root / 'manual-budget.sqlite3-journal'
        sidecar.symlink_to(path)
        with self.assertRaises(ValueError): reader.reserve_manual(self.root, clock=lambda: 101)

    def test_budget_reservations_survive_separate_concurrent_processes(self):
        from concurrent.futures import ThreadPoolExecutor
        import subprocess
        import sys
        program = ('import sys; from auth.runtime import BudgetExceeded; '
                   'from manual_reader import reserve_manual; '
                   '\ntry: reserve_manual(sys.argv[1],clock=lambda:101)'
                   '\nexcept BudgetExceeded: sys.exit(3)')
        def child(_):
            result = subprocess.run([sys.executable, '-c', program, str(self.root)],
                                    capture_output=True, timeout=15, check=False)
            self.assertEqual(result.stderr, b'')
            return result.returncode
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(child, range(12)))
        self.assertEqual(codes.count(0), 4)
        self.assertEqual(codes.count(3), 8)
        self.assertEqual(child(None), 3)

if __name__ == '__main__':
    unittest.main()
