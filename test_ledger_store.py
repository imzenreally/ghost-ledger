import tempfile
import unittest
from pathlib import Path


class StoreTests(unittest.TestCase):
    def test_publish_survives_reopen_and_upserts(self):
        from ledger_store import LedgerStore
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'private' / 'ledger.sqlite3'
            store = LedgerStore(path)
            store.publish([{'external_id': 'fixture-1', 'status': 'ready', 'amount': '8.40'}])
            store.publish([{'external_id': 'fixture-1', 'status': 'conflict', 'amount': '9.20'}])
            snapshot = LedgerStore(path).snapshot()
            self.assertEqual(len(snapshot['rows']), 1)
            self.assertEqual(snapshot['rows'][0]['amount'], '9.20')
            self.assertEqual(snapshot['source'], 'saved history')
            self.assertIsNotNone(snapshot['updated_at'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_audit_is_durable_and_imported_status_cannot_regress(self):
        from ledger_store import LedgerStore
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / 'private' / 'ledger.sqlite3'
            store = LedgerStore(path)
            store.record_event('fixture-1', 7, '8.40', 'imported', 'explicit CLI')
            store.publish([{'external_id': 'fixture-1', 'session_id': 7, 'status': 'ready', 'amount': '8.40'}])
            self.assertEqual(LedgerStore(path).events()[0]['outcome'], 'imported')
            self.assertEqual(store.snapshot()['rows'][0]['status'], 'imported')
            store.record_event('fixture-1', 7, '8.40', 'failed', 'rollback')
            self.assertEqual(len(store.events()), 2)
            self.assertEqual(store.snapshot()['rows'][0]['status'], 'imported')

    def test_publish_is_atomic_and_total_rows_are_bounded(self):
        from ledger_store import LedgerStore
        with tempfile.TemporaryDirectory() as tmp:
            store = LedgerStore(Path(tmp).resolve() / 'private' / 'ledger.sqlite3')
            store.MAX_ROWS = 2
            store.publish([{'external_id': 'one'}, {'external_id': 'two'}])
            with self.assertRaises(ValueError):
                store.publish([{'external_id': 'three'}])
            with self.assertRaises((ValueError, KeyError)):
                store.publish([{'external_id': 'one', 'status': 'changed'}, {}])
            self.assertNotIn('status', store.snapshot()['rows'][0])
            self.assertEqual(len(store.snapshot()['rows']), 2)

    def test_rejects_symlinks_in_state_path(self):
        from ledger_store import LedgerStore
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            (base / 'real').mkdir()
            (base / 'link').symlink_to(base / 'real', target_is_directory=True)
            with self.assertRaises(ValueError):
                LedgerStore(base / 'link' / 'ledger.sqlite3')
            (base / 'real' / 'target').write_text('do not touch')
            (base / 'real' / 'ledger.sqlite3').symlink_to(base / 'real' / 'target')
            with self.assertRaises(ValueError):
                LedgerStore(base / 'real' / 'ledger.sqlite3')
            self.assertEqual((base / 'real' / 'target').read_text(), 'do not touch')


if __name__ == '__main__':
    unittest.main()
