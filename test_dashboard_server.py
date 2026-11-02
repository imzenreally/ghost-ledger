import json
import os
from pathlib import Path
import tempfile
import unittest


class DashboardServerTests(unittest.TestCase):
    def test_private_config_sets_no_credentials_in_command_arguments(self):
        from dashboard_server import server_settings
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); path = root/'config.json'; os.chmod(root, 0o700)
            path.write_text(json.dumps({'password_hash':'synthetic-hash', 'secret_key':'x'*48,
                                       'bind_host':'10.0.0.10', 'state':str(root/'ledger.sqlite3')})); os.chmod(path, 0o600)
            args, env = server_settings(path)
            self.assertIn('10.0.0.10:8897', args)
            self.assertNotIn('synthetic-hash', ' '.join(args))
            self.assertEqual(env['GHOST_LEDGER_PASSWORD_HASH'], 'synthetic-hash')
            self.assertEqual(env['GHOST_LEDGER_STATE'], str(root/'ledger.sqlite3'))

    def test_public_or_wildcard_binding_is_rejected(self):
        from dashboard_server import server_settings
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); path=root/'config.json'; os.chmod(root, 0o700)
            for host in ('0.0.0.0','8.8.8.8','::','example.com'):
                path.write_text(json.dumps({'password_hash':'synthetic', 'secret_key':'x'*48,
                                           'bind_host':host, 'state':str(root/'ledger.sqlite3')})); os.chmod(path,0o600)
                with self.assertRaises(ValueError): server_settings(path)
