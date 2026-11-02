"""Offline refresh safety tests; never contact Tesla."""
import json
import multiprocessing
import os
import queue
from typing import Callable
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from auth import refresh, runtime


def _process_refresh(root, events, ready, crash=False):
    """Spawn-safe worker with an entirely synthetic transport."""
    def transport(*args):
        events.put(('request', None))
        if crash:
            # Flush the test queue before deliberately simulating process loss.
            events.close()
            events.join_thread()
            os._exit(17)
        return 200, b'{"access_token":"process-access","refresh_token":"process-refresh","token_type":"bearer","expires_in":3600}'
    ready.set()
    result = refresh.ensure_fresh(root, 'client-id', transport=transport, clock=lambda: 2001)
    events.put(('result', result))


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path.home() / '.hermes/cache/scratch')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'state'
        runtime.save_tokens(self.root, {'access_token': 'access-old', 'refresh_token': 'refresh-old',
                                       'expires_in': 1000}, clock=lambda: 1000)
        self.calls = []
        self.transport: Callable[..., tuple[int, bytes]] = self.good_transport

    def good_transport(self, url, data, headers, timeout):
        self.calls.append((url, data, headers, timeout))
        return 200, json.dumps({'access_token': 'access-new', 'refresh_token': 'refresh-new',
                                'token_type': 'Bearer', 'expires_in': 3600}).encode()

    def ensure(self, **kwargs):
        return refresh.ensure_fresh(self.root, 'client-id', transport=self.transport,
                                    clock=lambda: 2001, **kwargs)

    def test_expired_token_rotates_once_and_saves_pair(self):
        result = self.ensure()
        self.assertEqual(result['status'], 'refreshed')
        self.assertEqual(len(self.calls), 1)
        url, data, headers, timeout = self.calls[0]
        self.assertEqual(url, runtime.TOKEN_URL)
        self.assertEqual(parse_qs(data.decode()), {'grant_type': ['refresh_token'],
                         'client_id': ['client-id'], 'refresh_token': ['refresh-old']})
        self.assertEqual(headers['Content-Type'], 'application/x-www-form-urlencoded')
        self.assertEqual(runtime._read_private(self.root / 'tokens.json'),
                         {'access_token': 'access-new', 'refresh_token': 'refresh-new',
                          'expires_at': 5601})
        self.assertEqual(self.ensure()['status'], 'fresh')
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('access-new', json.dumps(result))
        for name in ('tokens.json', 'refresh-state.json', 'token-rotation.lock'):
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)

    def test_network_failure_blocks_restart_without_leaking_exception(self):
        def broken(*args):
            self.calls.append(args)
            raise OSError('SECRET access-old refresh-old')
        self.transport = broken
        result = self.ensure()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('refresh-old', (self.root / 'refresh-state.json').read_text())

    def test_http_401_requires_signin_and_does_not_retry(self):
        def unauthorized(*args):
            self.calls.append(args)
            return 401, b'SECRET'
        self.transport = unauthorized
        self.assertEqual(self.ensure()['reason'], 'sign_in_required')
        self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(len(self.calls), 1)

    def test_crash_after_reservation_blocks_same_token(self):
        class Crash(BaseException):
            pass
        def crash(*args):
            self.calls.append(args)
            self.assertEqual(runtime._read_private(self.root / 'refresh-state.json')['status'], 'pending')
            raise Crash()
        self.transport = crash
        with self.assertRaises(Crash):
            self.ensure()
        self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(len(self.calls), 1)

    def test_new_signin_unblocks_failed_fingerprint(self):
        self.transport = lambda *args: (401, b'')
        self.ensure()
        runtime.save_tokens(self.root, {'access_token': 'authorized-access',
                            'refresh_token': 'authorized-refresh', 'expires_in': 1}, clock=lambda: 1000)
        self.transport = self.good_transport
        self.assertEqual(self.ensure()['status'], 'refreshed')

    def test_pending_with_already_saved_rotation_is_resolved(self):
        old = __import__('hashlib').sha256(b'refresh-old').hexdigest()
        runtime._atomic_json(self.root, 'refresh-state.json', {'status': 'pending', 'fingerprint': old})
        runtime.save_tokens(self.root, {'access_token': 'saved-access', 'refresh_token': 'saved-refresh',
                                       'expires_in': 4000}, clock=lambda: 2000)
        self.assertEqual(self.ensure()['status'], 'fresh')
        self.assertEqual(self.calls, [])
        self.assertEqual(runtime._read_private(self.root / 'refresh-state.json')['status'], 'resolved')

    def test_token_save_failure_consumes_attempt(self):
        with patch.object(refresh, 'save_tokens', side_effect=OSError('SECRET')):
            self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(runtime._read_private(self.root / 'tokens.json')['refresh_token'], 'refresh-old')

    def test_invalid_responses_are_blocked_without_retry(self):
        base = {'access_token': 'access-new', 'refresh_token': 'refresh-new',
                'expires_in': 3600, 'token_type': 'Bearer'}
        bodies = [b'{"access_token":"a","access_token":"b","refresh_token":"r",'
                  b'"token_type":"Bearer","expires_in":3600}', b'[]', b'null', b'X' * 65537]
        for key, values in {'token_type': [None, 'Basic', 3],
                            'access_token': ['', None, 'a' * 16385, 'a\nSECRET', 'a\x7f'],
                            'refresh_token': ['', None, 'r\u0085', 'r\x00'],
                            'expires_in': [True, 0, -1, 86401, float('nan'), float('inf'), '3600']}.items():
            for value in values:
                bodies.append(json.dumps({**base, key: value}).encode())
        bodies.append(json.dumps({key: value for key, value in base.items() if key != 'token_type'}).encode())
        for body in bodies:
            with self.subTest(body=body[:80]):
                (self.root / 'refresh-state.json').unlink(missing_ok=True)
                self.calls.clear()
                def invalid(*args):
                    self.calls.append(args)
                    return 200, body
                self.transport = invalid
                self.assertEqual(self.ensure()['status'], 'blocked')
                self.assertEqual(self.ensure()['status'], 'blocked')
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(runtime._read_private(self.root / 'tokens.json')['access_token'], 'access-old')

    def test_invalid_inputs_do_not_consume_attempt(self):
        for client in ('', None, 'x\nSECRET', 'x' * 1025):
            with self.subTest(client=client):
                result = refresh.ensure_fresh(self.root, client, transport=self.transport, clock=lambda: 2001)
                self.assertEqual(result['status'], 'blocked')
                self.assertFalse((self.root / 'refresh-state.json').exists())
        for margin in (-1, True, float('nan'), float('inf'), '300'):
            self.assertEqual(self.ensure(margin=margin)['status'], 'blocked')
        for now in (True, float('nan'), float('inf'), None):
            self.assertEqual(refresh.ensure_fresh(self.root, 'client-id', transport=self.transport,
                                                clock=lambda: now)['status'], 'blocked')
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / 'refresh-state.json').exists())

    def test_invalid_stored_credentials_do_not_consume_attempt(self):
        base = runtime._read_private(self.root / 'tokens.json')
        for key, value in [('access_token', ''), ('refresh_token', 'r\n'),
                           ('expires_at', 'tomorrow'), ('expires_at', True)]:
            runtime._atomic_json(self.root, 'tokens.json', {**base, key: value})
            self.assertEqual(self.ensure()['status'], 'blocked')
            self.assertFalse((self.root / 'refresh-state.json').exists())
        self.assertEqual(self.calls, [])

    def test_unsafe_private_files_fail_closed(self):
        for name in ('tokens.json', 'refresh-state.json', 'token-rotation.lock'):
            with self.subTest(name=name):
                path = self.root / name
                if not path.exists():
                    runtime._atomic_json(self.root, name, {})
                os.chmod(path, 0o644)
                self.assertEqual(self.ensure()['status'], 'blocked')
                self.assertEqual(self.calls, [])
                if name == 'tokens.json':
                    os.chmod(path, 0o600)
                else:
                    path.unlink()

    def test_symlink_state_files_fail_closed(self):
        for name in ('tokens.json', 'refresh-state.json', 'token-rotation.lock'):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes() if path.exists() else None
                path.unlink(missing_ok=True)
                path.symlink_to(self.root / 'nonexistent-target')
                self.assertEqual(self.ensure()['status'], 'blocked')
                self.assertEqual(self.calls, [])
                self.assertFalse((self.root / 'nonexistent-target').exists())
                path.unlink()
                if original is not None:
                    path.write_bytes(original)
                    path.chmod(0o600)

    def test_unknown_or_malformed_marker_blocks(self):
        for state in ({}, {'status': 'surprise'}, {'status': 'pending', 'fingerprint': 'bad'},
                      {'status': 'failed', 'fingerprint': __import__('hashlib').sha256(b'refresh-old').hexdigest(),
                       'reason': 'SECRET'}):
            runtime._atomic_json(self.root, 'refresh-state.json', state)
            result = self.ensure()
            self.assertEqual(result['status'], 'blocked')
            self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(self.calls, [])

    def test_login_can_share_rotation_lock(self):
        with refresh.rotation_lock(self.root) as locked_root:
            self.assertEqual(locked_root, self.root)
            runtime.save_tokens(locked_root, {'access_token': 'login-a', 'refresh_token': 'login-r',
                                              'expires_in': 4000}, clock=lambda: 2000)
        self.assertEqual(self.ensure()['status'], 'fresh')
        self.assertEqual(self.calls, [])

    def test_same_refresh_token_response_is_not_reusable(self):
        self.transport = lambda *args: (200, json.dumps({'access_token': 'new-access',
                            'refresh_token': 'refresh-old', 'expires_in': 3600, 'token_type': 'Bearer'}).encode())
        self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(runtime._read_private(self.root / 'tokens.json')['access_token'], 'access-old')

    def test_competing_processes_send_only_one_refresh(self):
        ctx = multiprocessing.get_context('spawn')
        events = ctx.Queue()
        self.addCleanup(events.close)
        ready = ctx.Event()
        processes = [ctx.Process(target=_process_refresh, args=(self.root, events, ready))
                     for _ in range(4)]
        for process in processes:
            process.start()
            self.addCleanup(lambda p=process: p.kill() if p.is_alive() else None)
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        records = [events.get(timeout=2) for _ in range(5)]
        self.assertEqual(sum(kind == 'request' for kind, _ in records), 1)
        statuses = [value['status'] for kind, value in records if kind == 'result']
        self.assertCountEqual(statuses, ['refreshed', 'fresh', 'fresh', 'fresh'])

    def test_login_lock_excludes_refresh_process(self):
        ctx = multiprocessing.get_context('spawn')
        events = ctx.Queue()
        self.addCleanup(events.close)
        ready = ctx.Event()
        with refresh.rotation_lock(self.root):
            process = ctx.Process(target=_process_refresh, args=(self.root, events, ready))
            process.start()
            self.addCleanup(lambda: process.kill() if process.is_alive() else None)
            self.assertTrue(ready.wait(5))
            with self.assertRaises(queue.Empty):
                events.get(timeout=0.1)
            runtime.save_tokens(self.root, {'access_token': 'login-a', 'refresh_token': 'login-r',
                                            'expires_in': 4000}, clock=lambda: 2000)
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        kind, result = events.get(timeout=2)
        self.assertEqual(kind, 'result')
        self.assertEqual(result['status'], 'fresh')

    def test_process_loss_after_pending_reservation_does_not_retry(self):
        ctx = multiprocessing.get_context('spawn')
        events = ctx.Queue()
        self.addCleanup(events.close)
        ready = ctx.Event()
        process = ctx.Process(target=_process_refresh, args=(self.root, events, ready, True))
        process.start()
        self.addCleanup(lambda: process.kill() if process.is_alive() else None)
        process.join(10)
        self.assertEqual(process.exitcode, 17)
        self.assertEqual(events.get(timeout=2)[0], 'request')
        self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(self.calls, [])

    def test_pending_write_failure_sends_no_request(self):
        with patch.object(refresh, '_atomic_json', side_effect=OSError('SECRET')):
            self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(self.calls, [])

    def test_failed_marker_write_preserves_pending_reservation(self):
        real_write = refresh._atomic_json
        def write(root, name, data):
            if data['status'] == 'failed':
                raise OSError('SECRET')
            real_write(root, name, data)
        self.transport = lambda *args: (500, b'SECRET')
        with patch.object(refresh, '_atomic_json', side_effect=write):
            self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(runtime._read_private(self.root / 'refresh-state.json')['status'], 'pending')
        self.transport = self.good_transport
        self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(self.calls, [])

    def test_saved_rotation_survives_resolved_marker_failure(self):
        real_write = refresh._atomic_json
        def write(root, name, data):
            if data['status'] == 'resolved':
                raise OSError('SECRET')
            real_write(root, name, data)
        with patch.object(refresh, '_atomic_json', side_effect=write):
            self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(self.ensure()['status'], 'fresh')
        self.assertEqual(len(self.calls), 1)

    def test_unsafe_root_or_symlink_parent_blocks(self):
        self.root.chmod(0o755)
        self.assertEqual(self.ensure()['status'], 'blocked')
        self.root.chmod(0o700)
        link = Path(self.tmp.name) / 'alias'
        link.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(refresh.ensure_fresh(link, 'client-id', transport=self.transport)['status'], 'blocked')
        self.assertEqual(self.calls, [])

    def test_margin_boundary_is_due(self):
        result = refresh.ensure_fresh(self.root, 'client-id', transport=self.transport, clock=lambda: 1700)
        self.assertEqual(result['status'], 'refreshed')
        self.assertTrue(result['refreshed'])
        self.assertEqual(result['expires_at'], 5300)
        self.assertEqual(len(self.calls), 1)

    def test_duplicate_keys_in_private_json_are_rejected(self):
        for name, body in (
                ('tokens.json', b'{"access_token":"a","refresh_token":"r1","refresh_token":"r2","expires_at":1}'),
                ('refresh-state.json', b'{"status":"pending","status":"resolved","fingerprint":"' + b'0' * 64 + b'"}')):
            with self.subTest(name=name):
                original = (self.root / 'tokens.json').read_bytes()
                path = self.root / name
                path.write_bytes(body)
                path.chmod(0o600)
                self.assertEqual(self.ensure()['status'], 'blocked')
                self.assertEqual(self.calls, [])
                (self.root / 'tokens.json').write_bytes(original)
                if name != 'tokens.json':
                    path.unlink()

    def test_nonregular_state_files_fail_closed_without_blocking(self):
        for name in ('tokens.json', 'refresh-state.json', 'token-rotation.lock'):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes() if path.exists() else None
                path.unlink(missing_ok=True)
                os.mkfifo(path, 0o600)
                self.assertEqual(self.ensure()['status'], 'blocked')
                self.assertEqual(self.calls, [])
                path.unlink()
                if original is not None:
                    path.write_bytes(original)
                    path.chmod(0o600)

    def test_hardlinked_lock_is_rejected(self):
        lock = self.root / 'token-rotation.lock'
        runtime._atomic_json(self.root, lock.name, {})
        os.link(lock, self.root / 'lock-alias')
        self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(self.calls, [])

    def test_foreign_owned_lock_is_rejected(self):
        real_fstat = os.fstat
        def foreign(fd):
            values = list(real_fstat(fd))
            values[4] = os.getuid() + 1
            return os.stat_result(values)
        with patch.object(refresh.os, 'fstat', side_effect=foreign):
            self.assertEqual(self.ensure()['status'], 'blocked')
        self.assertEqual(self.calls, [])

    def test_valid_token_makes_no_request(self):
        result = refresh.ensure_fresh(self.root, 'client-id', transport=self.transport,
                                      clock=lambda: 1000)
        self.assertEqual(result['status'], 'fresh')
        self.assertEqual(self.calls, [])
        self.assertNotIn('access-old', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
