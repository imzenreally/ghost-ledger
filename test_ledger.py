import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import ledger
from manual_reader import runtime


class LedgerCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.history = self.root / 'history.json'
        runtime._atomic_json(self.root, self.history.name, {'response': {'data': []}})
        self.args = ['--history', str(self.history), '--state', str(self.root / 'ledger.sqlite3')]
        self.output = io.StringIO()
        self.errors = io.StringIO()

    def invoke(self, command, **kwargs):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.errors):
            return ledger.main(command + self.args, **kwargs)

    def test_cached_preview_real_workflow_no_fetch_no_backup_redacted(self):
        class Database:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self): raise AssertionError('no records means injected repository is used')
            def backup(self, path): raise AssertionError('preview must not backup')
        class Repository:
            def __init__(self, connect): pass
            def sessions_for(self, history): return []
        with patch.object(ledger, 'PgRepository', Repository), patch.object(ledger, 'fetch_latest', side_effect=AssertionError('no fetch')):
            result = self.invoke(['preview'], database_factory=Database)
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(self.output.getvalue())['statuses'], {})
        self.assertEqual((self.root / 'ledger.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_import_requires_explicit_apply_before_any_connections_or_fetch(self):
        factory = Mock(side_effect=AssertionError('must not connect'))
        with self.assertRaises(SystemExit) as raised:
            self.invoke(['import'], database_factory=factory)
        self.assertEqual(raised.exception.code, 2)
        factory.assert_not_called()
        self.assertFalse((self.root / 'ledger.sqlite3').exists())

    def test_apply_backup_precedes_write_repeat_is_noop_and_output_has_no_vin(self):
        from charging_costs.test_importer import bill, session
        from test_workflow import FakeRepository
        runtime._atomic_json(self.root, self.history.name, [bill()])
        repo = FakeRepository([session()])
        backups = []
        class Database:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def connect(self): raise AssertionError('injected repository')
            def backup(self, path):
                self_path = Path(path)
                self_path.write_bytes(b'PGDMPfixture')
                self_path.chmod(0o600)
                backups.append((path, repo.writes))
                return path
        with patch.object(ledger, 'PgRepository', return_value=repo), patch.object(ledger, 'fetch_latest', side_effect=AssertionError('no fetch')):
            self.assertEqual(self.invoke(['import', '--apply'], database_factory=Database), 0)
            first = json.loads(self.output.getvalue())
            self.output.seek(0); self.output.truncate()
            self.assertEqual(self.invoke(['import', '--apply'], database_factory=Database), 0)
        self.assertEqual(first['imported'], 1)
        self.assertEqual(repo.writes, 1)
        self.assertEqual(len(backups), 2)
        self.assertNotEqual(backups[0][0], backups[1][0])
        self.assertEqual(backups[0][1], 0)
        self.assertNotIn(bill()['vin'], self.output.getvalue())
        self.assertNotIn('external_id', self.output.getvalue())
        self.assertEqual(json.loads(self.output.getvalue())['imported'], 0)

    def test_backup_failure_prevents_workflow_and_closes_database(self):
        database = Mock()
        database.__enter__ = Mock(return_value=database)
        database.__exit__ = Mock(return_value=None)
        database.backup.side_effect = ledger.OwnerDatabaseError('pre-apply backup failed')
        with patch.object(ledger, 'run_import') as workflow:
            self.assertEqual(self.invoke(['import', '--apply'], database_factory=lambda: database), 2)
            workflow.assert_not_called()
        database.__exit__.assert_called_once()

    def test_single_process_lock_blocks_fetch_connection_and_duplicate_audits(self):
        with ledger.import_lock(self.root / 'ledger.sqlite3'):
            with patch.object(ledger, 'fetch_latest') as fetch:
                factory = Mock(side_effect=AssertionError('must not connect'))
                self.assertEqual(self.invoke(['import', '--apply', '--fetch'], database_factory=factory), 2)
                fetch.assert_not_called()
                factory.assert_not_called()
        self.assertIn('another manual ledger operation', self.errors.getvalue())

    def test_fetch_is_explicit_once_and_uses_auth_default_not_history_override(self):
        from charging_costs.test_importer import bill, session
        from test_workflow import FakeRepository
        repo = FakeRepository([session()])
        runtime._atomic_json(self.root, self.history.name, [bill()])
        database = Mock()
        database.__enter__ = Mock(return_value=database)
        database.__exit__ = Mock(return_value=None)
        with patch.object(ledger, 'PgRepository', return_value=repo), patch.object(ledger, 'fetch_latest') as fetch:
            self.assertEqual(self.invoke(['preview', '--fetch'], database_factory=lambda: database), 0)
            fetch.assert_called_once_with(self.history, token_root=ledger.DEFAULT_ROOT)
            database.backup.assert_not_called()

    def test_untrusted_state_paths_and_secret_exceptions_are_not_echoed(self):
        state = self.root / 'ledger.sqlite3'
        state.symlink_to(self.history)
        factory = Mock(side_effect=AssertionError('must not connect'))
        self.assertEqual(self.invoke(['preview'], database_factory=factory), 2)
        factory.assert_not_called()
        state.unlink()
        factory = Mock(side_effect=ConnectionError('secret-password VIN invoice-data'))
        self.assertEqual(self.invoke(['preview'], database_factory=factory), 2)
        self.assertNotIn('secret-password', self.errors.getvalue())
        self.assertNotIn('invoice-data', self.errors.getvalue())

if __name__ == '__main__':
    unittest.main()
