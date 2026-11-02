"""Offline-only notification tests; fake credentials and injected transports."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import notifications


class NotificationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'state'
        self.keys = self.base / 'keys'
        self.keys.write_text('PUSHOVER_API_TOKEN=' + 'A' * 30 + '\nPUSHOVER_USER_KEY=' + 'B' * 30 + '\n')
        self.keys.chmod(0o600)
        self.calls = []
        self.now = 1800000000

    def send(self, *args):
        self.calls.append(args)
        return 200, b'{"status":1}'

    def notifier(self, **kwargs):
        return notifications.Notifier(self.root, credential_path=self.keys,
                                      clock=lambda: self.now, transport=kwargs.get('transport', self.send))

    def test_pending_is_durable_before_send_and_restart_deduplicates(self):
        def send(*args):
            with sqlite3.connect(n.path) as db:
                self.assertEqual(db.execute('SELECT state FROM notifications').fetchone()[0], 'pending')
            return self.send(*args)
        n = self.notifier(transport=send)
        self.assertEqual(n.emit('import:1:2', 'Imported', 'Session 1: $2.00 USD')['status'], 'sent')
        self.assertEqual(self.notifier().emit('import:1:2', 'Imported', 'Session 1: $2.00 USD')['status'], 'duplicate')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(n.snapshot()['counts'], {'sent': 1})
        self.assertNotIn('Session', str(n.snapshot()))
        self.assertEqual(n.path.stat().st_mode & 0o777, 0o600)

    def test_missing_credentials_disable_without_reserving(self):
        self.keys.unlink()
        n = self.notifier()
        self.assertEqual(n.emit('a', 'Title', 'Message')['status'], 'disabled')
        self.assertEqual(n.snapshot()['counts'], {})
        self.assertEqual(self.calls, [])
        with patch.dict(os.environ, {'GHOST_LEDGER_PUSHOVER_KEYS_FILE': ''}):
            self.assertEqual(notifications.Notifier(self.root).emit('b', 'T', 'M')['status'], 'disabled')

    def test_invalid_credentials_and_unsafe_paths_fail_closed(self):
        n = self.notifier()
        for content in ('PUSHOVER_API_TOKEN=bad\nPUSHOVER_USER_KEY=bad',
                        self.keys.read_text() + 'EXTRA=value\n',
                        self.keys.read_text() + 'PUSHOVER_API_TOKEN=' + 'C'*30):
            self.keys.write_text(content)
            self.assertEqual(n.emit('bad', 'T', 'M')['status'], 'blocked')
        self.keys.write_text('PUSHOVER_API_TOKEN=' + 'A'*30 + '\nPUSHOVER_USER_KEY=' + 'B'*30)
        for mode in (0o644, 0o400, 0o666):
            self.keys.chmod(mode)
            self.assertEqual(n.emit('bad', 'T', 'M')['status'], 'blocked')
        self.keys.chmod(0o600)
        alias = self.base / 'alias'
        alias.symlink_to(self.keys)
        self.assertEqual(notifications.Notifier(self.root, alias).emit('x', 'T', 'M')['status'], 'blocked')
        alias.unlink()
        os.link(self.keys, alias)
        self.assertEqual(n.emit('bad', 'T', 'M')['status'], 'blocked')
        self.assertEqual(self.calls, [])

    def test_ambiguous_and_rejected_results_are_terminal(self):
        cases = [(400, b'{"status":0}', 'blocked'), (429, b'{}', 'blocked'),
                 (500, b'{}', 'uncertain'), (302, b'{}', 'uncertain'),
                 (200, b'{"status":true}', 'uncertain'),
                 (200, b'{"status":"1"}', 'uncertain'), (200, b'{}', 'uncertain'),
                 (200, b'{"status":0}', 'blocked'), (200, b'x'*65537, 'uncertain')]
        for i, (http, body, expected) in enumerate(cases):
            n = self.notifier(transport=lambda *args: (http, body))
            self.assertEqual(n.emit(str(i), 'T', 'M')['status'], expected)
            self.assertEqual(n.emit(str(i), 'T', 'M')['status'], 'duplicate')
        def failing(*args):
            raise TimeoutError('private credentials must never escape')
        n = self.notifier(transport=failing)
        self.assertEqual(n.emit('timeout', 'T', 'M'), {'status': 'uncertain'})
        self.assertEqual(n.emit('timeout', 'T', 'M')['status'], 'duplicate')

    def test_payload_bounds_and_control_characters_block_before_transport(self):
        n = self.notifier()
        for key, title, message in [('', 'T', 'M'), ('x'*257, 'T', 'M'),
                                    ('x', 'T'*251, 'M'), ('x', 'T', 'M'*1025),
                                    ('x', 'T\n', 'M'), ('x', 'T', 'M\x7f'),
                                    ('x', '', 'M'), ('x', 'T', ''), ('x\x00', 'T', 'M')]:
            self.assertEqual(n.emit(key, title, message)['status'], 'blocked')
        self.assertEqual(n.emit('x'*256, 'T'*250, 'M'*1024)['status'], 'sent')
        self.assertEqual(len(self.calls), 1)
    def test_state_rejects_links_permissions_and_recovers_pending(self):
        n = self.notifier()
        with n._db() as db:
            db.execute('INSERT INTO notifications VALUES (?,?,?)', ('crash', 'pending', self.now))
        self.assertEqual(self.notifier().snapshot()['counts'], {'uncertain': 1})
        self.assertEqual(n.emit('crash', 'T', 'M')['status'], 'duplicate')
        n.path.chmod(0o644)
        self.assertEqual(n.emit('other', 'T', 'M')['status'], 'blocked')
        with self.assertRaises((ValueError, OSError)):
            self.notifier()
        n.path.chmod(0o600)
        alias = self.base / 'linkedroot'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            notifications.Notifier(alias)
        sidecar = Path(str(n.path) + '-journal')
        sidecar.symlink_to(self.keys)
        self.assertEqual(n.emit('other', 'T', 'M')['status'], 'blocked')
        self.assertEqual(self.calls, [])

    def test_retention_is_bounded_capacity_fails_without_eviction(self):
        n = self.notifier()
        with n._db() as db:
            db.executemany('INSERT INTO notifications VALUES (?,?,?)',
                           [(str(i), 'sent', self.now) for i in range(1000)])
        self.assertEqual(n.emit('overflow', 'T', 'M')['status'], 'blocked')
        self.assertEqual(n.snapshot()['counts'], {'sent': 1000})
        self.assertEqual(self.calls, [])
        self.now += 30*86400 + 1
        self.assertEqual(n.emit('new', 'T', 'M')['status'], 'sent')
        self.assertEqual(n.snapshot()['counts'], {'sent': 1})
        with n._db() as db:
            self.assertEqual(db.execute('PRAGMA auto_vacuum').fetchone()[0], 1)

    def test_transport_uses_fixed_https_verified_tls_no_redirects_or_proxies(self):
        class Response:
            status = 200
            def read(self, size):
                self.size = size
                return b'{"status":1}'
            def __enter__(self): return self
            def __exit__(self, *args): pass
        response = Response()
        with patch('notifications.urllib.request.build_opener') as build:
            build.return_value.open.return_value = response
            self.assertEqual(notifications.pushover_transport('A'*30, 'B'*30, 'T', 'M'),
                             (200, b'{"status":1}'))
            args, kwargs = build.return_value.open.call_args
            request = args[0]
            self.assertEqual(request.full_url, 'https://api.pushover.net/1/messages.json')
            self.assertEqual(request.get_method(), 'POST')
            self.assertEqual(kwargs['timeout'], 10)
            from urllib.parse import parse_qs
            payload = parse_qs(request.data.decode())
            self.assertEqual(payload['priority'], ['0'])
            self.assertNotIn('html', payload)
            self.assertNotIn('retry', payload)
            handlers = build.call_args.args
            proxy = next(h for h in handlers if isinstance(h, notifications.urllib.request.ProxyHandler))
            self.assertEqual(proxy.proxies, {})
            https = next(h for h in handlers if isinstance(h, notifications.urllib.request.HTTPSHandler))
            import ssl
            self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(https._context.check_hostname)
            redirect = next(h for h in handlers if isinstance(h, notifications.urllib.request.HTTPRedirectHandler))
            self.assertIsNone(redirect.redirect_request(None, None, 302, 'redirect', {}, 'https://evil.invalid'))
            self.assertEqual(response.size, 65537)
    def test_monitor_baselines_history_then_only_new_verified_imports(self):
        from ledger_store import LedgerStore
        store = LedgerStore(self.base / 'ledger' / 'ledger.sqlite3')
        store.record_event('old-private', 1, '1.25', 'imported', 'PRIVATE VIN GPS')
        n = self.notifier()
        self.assertEqual(n.observe(store, {}, {}, {})['status'], 'ok')
        self.assertEqual(self.calls, [])
        store.record_event('invoice-private', 12, '9.2', 'imported', 'PRIVATE VIN GPS')
        self.notifier().observe(store, {}, {}, {})
        self.assertEqual(len(self.calls), 1)
        self.assertIn('12', self.calls[0][3])
        self.assertIn('$9.20 USD', self.calls[0][3])
        self.assertNotIn('PRIVATE', str(self.calls))
        self.assertNotIn('invoice', str(self.calls))
        self.now += 31*86400
        self.notifier().observe(store, {}, {}, {})
        self.assertEqual(len(self.calls), 1)
        store.record_event('badamount', 13, 'NaN', 'imported')
        store.record_event('badamount2', 14, '1.123', 'imported')
        n.observe(store, {}, {}, {})
        self.assertEqual(len(self.calls), 1)

    def test_historical_preview_conflicts_do_not_create_daily_alert_spam(self):
        class Store:
            def events(self): return []
            def snapshot(self): return {'rows':[{'session_id':9,'status':'conflict'}]}
        self.notifier().observe(Store(),{}, {}, {})
        self.now+=86400
        self.notifier().observe(Store(),{}, {}, {})
        self.assertEqual(self.calls,[])

    def test_monitor_canonical_issues_once_per_utc_day_and_no_routine_noise(self):
        class Store:
            def events(self): return []
            def snapshot(self):
                return {'rows': [{'session_id': 42, 'status': 'conflict', 'reason': 'VIN PRIVATE'}]}
        n = self.notifier()
        event = {'jobs': [{'session_id': 42, 'state': 'conflict'},
                          {'session_id': 43, 'state': 'waiting'},
                          {'session_id': 44, 'state': 'needs_review'}]}
        daily = {'status': 'completed', 'jobs': [{'session_id': 42, 'state': 'conflict'}]}
        for _ in range(3):
            self.notifier().observe(Store(), event, daily, {'renewal': {'status': 'fresh'}})
            self.now += 30
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('PRIVATE', str(self.calls))
        self.now += 86400
        n.observe(Store(), event, daily, {})
        self.assertEqual(len(self.calls), 4)

    def test_monitor_daily_failure_auth_and_budget_are_actionable_not_healthy_empty(self):
        class Store:
            def events(self): return []
            def snapshot(self): return {'rows': []}
        n = self.notifier()
        for status in ('not_due', 'completed', 'waiting', 'busy', 'ok'):
            n.observe(Store(), {}, {'status': status, 'jobs': []}, {})
        self.assertEqual(self.calls, [])
        n.observe(Store(), {'jobs': [{'session_id': 7, 'state': 'budget_paused'}]},
                  {'status': 'needs_review', 'reason': 'PRIVATE'},
                  {'renewal': {'status': 'blocked', 'reason': 'PRIVATE'}})
        self.assertEqual(len(self.calls), 3)
        n.observe(Store(), {'jobs': [{'session_id': 7, 'state': 'budget_paused'}]},
                  {'status': 'needs_review'}, {'renewal': {'status': 'blocked'}})
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn('PRIVATE', str(self.calls))
        class Broken:
            def events(self): raise RuntimeError('PRIVATE')
        self.assertEqual(n.observe(Broken(), {}, {}, {}), {'status': 'blocked'})
    def test_concurrent_processes_send_one_request(self):
        import subprocess
        import sys
        script = '''
import os, sys
from notifications import Notifier
def send(*args):
    fd = os.open(sys.argv[3], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.write(fd, b'called\\n')
    os.close(fd)
    return 200, b'{"status":1}'
n = Notifier(sys.argv[1], sys.argv[2], transport=send)
assert n.emit('same-event', 'T', 'M')['status'] in ('sent', 'duplicate')
'''
        marker = self.base / 'requests'
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.root), str(self.keys), str(marker)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        results = [process.communicate(timeout=20) for process in processes]
        for process, (out, err) in zip(processes, results):
            self.assertEqual(process.returncode, 0, (out, err))
        self.assertEqual(marker.read_text(), 'called\n')

    def test_idle_monitor_prunes_expired_states_and_disabled_first_enable_baselines(self):
        class Store:
            def events(self): return [{'id': 8, 'session_id': 8, 'outcome': 'imported', 'amount': '1.00'}]
            def snapshot(self): return {'rows': []}
        n = self.notifier()
        n.emit('old', 'T', 'M')
        self.now += 30*86400 + 1
        self.assertEqual(n.observe(Store(), {}, {}, {})['status'], 'ok')
        self.assertEqual(n.snapshot()['counts'], {})
        self.assertEqual(len(self.calls), 1)
        keys_backup = self.keys.read_text()
        self.keys.unlink()
        other = notifications.Notifier(self.base / 'other', self.keys, transport=self.send)
        self.assertEqual(other.observe(Store(), {}, {}, {})['status'], 'disabled')
        self.keys.write_text(keys_backup)
        self.keys.chmod(0o600)
        other.observe(Store(), {}, {}, {})
        self.assertEqual(len(self.calls), 1)

    def test_new_audit_conflict_is_canonical_with_current_status(self):
        class Store:
            events_list = []
            def events(self): return self.events_list
            def snapshot(self): return {'rows': []}
        store = Store()
        n = self.notifier()
        n.observe(store, {}, {}, {})
        store.events_list = [{'id': 1, 'session_id': 17, 'outcome': 'conflict', 'detail': 'PRIVATE'}]
        n.observe(store, {'jobs': [{'session_id': 17, 'state': 'conflict'}]}, {}, {})
        self.assertEqual(len(self.calls), 1)
        # A new audit-only review outcome is still actionable.
        store.events_list.append({'id': 2, 'session_id': 18, 'outcome': 'uncertain', 'detail': 'PRIVATE'})
        n.observe(store, {}, {}, {})
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('PRIVATE', str(self.calls))
    def test_new_manual_save_notifies_once_after_baseline(self):
        class Store:
            events_list = []
            def events(self): return self.events_list
            def snapshot(self): return {'rows': []}
        store = Store()
        n = self.notifier()
        n.observe(store, {}, {}, {})
        store.events_list = [{'id': 2, 'session_id': 31, 'outcome': 'manual_saved',
                             'amount': '8.2', 'external_id': 'manual-private'}]
        self.notifier().observe(store, {}, {}, {})
        self.notifier().observe(store, {}, {}, {})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2], 'Ghost Ledger')
        self.assertEqual(self.calls[0][3], 'Manual charging cost saved: $8.20 USD for session 31.')
    def test_monitor_uncertain_transport_is_not_retried_every_cycle(self):
        class Store:
            def events(self): return []
            def snapshot(self): return {'rows': []}
        calls = []
        def fail(*args):
            calls.append(1)
            raise TimeoutError('PRIVATE')
        n = self.notifier(transport=fail)
        for _ in range(4):
            n = self.notifier(transport=fail)
            self.assertEqual(n.observe(Store(), {}, {'status': 'needs_review'}, {})['status'], 'ok')
            self.now += 30
        self.assertEqual(calls, [1])
        self.assertEqual(n.snapshot()['counts'], {'uncertain': 1})

    def test_credential_symlink_parent_and_state_hardlinks_are_rejected(self):
        linked = self.base / 'parent-alias'
        linked.symlink_to(self.base, target_is_directory=True)
        n = notifications.Notifier(self.root, linked / 'keys', transport=self.send)
        self.assertEqual(n.emit('test', 'T', 'M')['status'], 'blocked')
        n = self.notifier()
        os.link(n.path, self.base / 'db-alias')
        self.assertEqual(n.emit('test', 'T', 'M')['status'], 'blocked')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
