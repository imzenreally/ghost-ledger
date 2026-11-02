import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import owner_database as owner


class OwnerDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.commands = []
        def run(args, **kwargs):
            self.commands.append((args, kwargs))
            if 'inspect' in args[-1]:
                return Mock(returncode=0, stdout=json.dumps([{'NetworkSettings': {'Networks': {'bridge': {'IPAddress': '172.22.0.4'}}}}]).encode())
            if 'printenv' in args[-1]:
                return Mock(returncode=0, stdout=b'fixture-password\n')
            kwargs['stdout'].write(b'PGDMPfixture-dump')
            return Mock(returncode=0)
        self.runner = run
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen = Mock(return_value=self.process)
        self.connector = Mock(return_value=Mock())

    def database(self, **kwargs):
        return owner.OwnerDatabase(run=self.runner, popen=self.popen, connector=self.connector,
                                   ready=lambda port: True, **kwargs)

    def test_tunnel_connection_backup_cleanup_without_password_arguments(self):
        with self.database() as db:
            first = db.connect()
            db.connect()
            backup = db.backup(self.root / 'backup.dump')
            self.assertEqual(backup.read_bytes()[:5], b'PGDMP')
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.connector.call_count, 2)
            config = self.connector.call_args.kwargs
            self.assertEqual(config['password'], 'fixture-password')
            self.assertEqual(config['host'], '127.0.0.1')
            self.assertEqual(config['dbname'], 'teslamate')
            self.assertEqual(config['application_name'], 'ghost-ledger')
            self.assertIn('statement_timeout=10000', config['options'])
            self.assertIn('lock_timeout=5000', config['options'])
            self.assertEqual(config['connect_timeout'], 5)
        self.process.terminate.assert_called_once()
        self.process.wait.assert_called()
        argv = self.popen.call_args.args[0]
        self.assertIn('127.0.0.1:', ' '.join(argv))
        self.assertIn(':172.22.0.4:5432', ' '.join(argv))
        self.assertNotIn('fixture-password', str(self.commands) + str(argv))
        for command in [argv] + [entry[0] for entry in self.commands]:
            for option in ('ControlMaster=no', 'ControlPath=none', 'ControlPersist=no', 'ForkAfterAuthentication=no'):
                self.assertIn(option, command)
        dump_cmd = self.commands[-1][0][-1]
        for table in ('charging_processes', 'cars', 'geofences'):
            self.assertIn('--table=public.' + table, dump_cmd)
        with self.assertRaises(owner.OwnerDatabaseError):
            db.connect()

    def test_discovery_failure_does_not_start_tunnel_or_echo_secret(self):
        def fail(*args, **kwargs):
            raise OSError('fixture-password secret stderr')
        self.runner = fail
        with self.assertRaises(owner.OwnerDatabaseError) as raised:
            with self.database(): self.fail('must not enter')
        self.popen.assert_not_called()
        self.assertNotIn('fixture-password', str(raised.exception))

    def test_ambiguous_or_invalid_ipv4_is_rejected_before_tunnel(self):
        for values in (['172.22.0.4', '172.23.0.5'], ['127.0.0.1;evil'], []):
            self.runner = lambda *a, **kw: Mock(returncode=0, stdout=json.dumps([{'NetworkSettings': {'Networks': {str(i): {'IPAddress': ip} for i, ip in enumerate(values)}}}]).encode())
            with self.assertRaises(owner.OwnerDatabaseError):
                with self.database(): self.fail('must not enter')
        self.popen.assert_not_called()

    def test_startup_failure_and_timeout_cleanup_child_and_credentials(self):
        self.process.poll.return_value = 255
        db = self.database()
        with self.assertRaisesRegex(owner.OwnerDatabaseError, 'SSH tunnel exited'):
            with db: self.fail('must not enter')
        self.assertIsNone(db._password)
        self.process.wait.assert_called()
        self.process.reset_mock()
        self.process.poll.return_value = None
        db = owner.OwnerDatabase(run=self.runner, popen=self.popen, connector=self.connector,
                                 ready=lambda port: False, startup_timeout=0.001)
        with self.assertRaisesRegex(owner.OwnerDatabaseError, 'timed out'):
            with db: self.fail('must not enter')
        self.process.terminate.assert_called_once()
        self.assertIsNone(db._password)

    def test_bad_backup_header_returncode_and_timeout_remove_partial_file(self):
        import subprocess
        for kind in ('header', 'returncode', 'empty', 'timeout'):
            with self.database() as db:
                def bad_dump(*args, **kwargs):
                    kwargs['stdout'].write(b'PGDMPvalid' if kind != 'header' else b'not-a-dump')
                    if kind == 'empty':
                        kwargs['stdout'].seek(0); kwargs['stdout'].truncate()
                    if kind == 'timeout': raise subprocess.TimeoutExpired('secret-command', 60)
                    return Mock(returncode=1 if kind == 'returncode' else 0)
                db._run = bad_dump
                target = self.root / (kind + '.dump')
                with self.assertRaises(owner.OwnerDatabaseError) as raised:
                    db.backup(target)
                self.assertFalse(target.exists())
                self.assertNotIn('secret-command', str(raised.exception))

    def test_backup_never_clobbers_existing_or_follows_symlink(self):
        target = self.root / 'existing.dump'
        target.write_bytes(b'keep-this'); target.chmod(0o600)
        link = self.root / 'link.dump'; link.symlink_to(target)
        with self.database() as db:
            with self.assertRaises(owner.OwnerDatabaseError): db.backup(target)
            with self.assertRaises(ValueError): db.backup(link)
        self.assertEqual(target.read_bytes(), b'keep-this')

    def test_context_body_failure_stops_tunnel_and_connect_error_is_sanitized(self):
        self.connector.side_effect = OSError('secret-password')
        db = self.database()
        with self.assertRaisesRegex(RuntimeError, 'body failure'):
            with db:
                with self.assertRaises(owner.OwnerDatabaseError) as raised:
                    db.connect()
                self.assertNotIn('secret-password', str(raised.exception))
                raise RuntimeError('body failure')
        self.process.terminate.assert_called_once()
        self.assertIsNone(db._password)

    def test_real_loopback_health_and_child_process_cleanup(self):
        import socket
        import subprocess
        import sys
        processes = []
        def spawn(args, **kwargs):
            forward = args[args.index('-L') + 1]
            port = forward.split(':')[1]
            child = subprocess.Popen([sys.executable, '-c',
                'import socket,sys,time; s=socket.socket(); s.bind(("127.0.0.1",int(sys.argv[1]))); s.listen(); time.sleep(30)', port], **kwargs)
            processes.append(child)
            return child
        db = owner.OwnerDatabase(run=self.runner, popen=spawn, connector=self.connector, startup_timeout=3)
        with db:
            port = db.port
            self.assertTrue(owner._ready(port))
            self.assertIsNone(processes[0].poll())
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(owner._ready(port))

if __name__ == '__main__':
    unittest.main()
