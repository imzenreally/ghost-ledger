import tempfile
import unittest
from pathlib import Path
from typing import Any


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'scheduler'
        self.now: Any = 1793577590.0  # epochs, independent of local midnight/DST

    def scheduler(self):
        from billing_scheduler import Scheduler
        return Scheduler(self.root, clock=lambda: self.now)

    def candidate(self, session: Any = 10, **changes) -> dict[str, Any]:
        row = dict(car_id=1, session_id=session, event_id='opaque',
                   started_at=self.now - 100, observed_at=self.now,
                   status='confirmed_away', geofence_id=2)
        row.update(changes)
        return row

    def test_durable_first_slot_eligibility_and_session_deduplication(self):
        s = self.scheduler()
        rows = [self.candidate(), self.candidate(event_id='different'),
                self.candidate(11, status='unknown_location', geofence_id=None),
                self.candidate(12, status='skipped_home', geofence_id=1),
                self.candidate(None, status='needs_db_confirmation')]
        result = s.sync(rows)
        self.assertEqual(result['counts'], {'scheduled': 2})
        self.assertEqual(result['next_due'], self.now + 300)
        self.now += 299
        calls = []
        s.run_due(lambda jobs: calls.append(jobs))
        self.assertEqual(calls, [])
        self.now += 1
        def check(jobs):
            calls.append(jobs)
            return {j['session_id']: 'imported' for j in jobs}
        result = self.scheduler().run_due(check)
        self.assertEqual(len(calls), 1)
        self.assertEqual({j['session_id'] for j in calls[0]}, {10, 11})
        self.assertEqual(result['counts'], {'imported': 2})
        self.assertIsNone(result['next_due'])
        self.assertEqual(s.sync(rows)['counts'], {'imported': 2})

    def test_waiting_has_three_finite_slots_and_no_restart_burst(self):
        s = self.scheduler()
        start = self.now
        s.sync([self.candidate()])
        calls = []
        def waiting(jobs):
            calls.append(jobs)
            return {10: 'waiting'}
        for offset, next_offset, attempts in [(300, 1800, 1), (1800, 7200, 2), (7200, None, 3)]:
            self.now = start + offset
            result = self.scheduler().run_due(waiting)
            self.assertEqual(result['next_due'], start + next_offset if next_offset else None)
            self.assertEqual(result['jobs'][0]['attempts'], attempts)
            if attempts < 3:
                self.assertEqual(result['counts'], {'waiting': 1})
        self.assertEqual(result['counts'], {'exhausted': 1})
        self.now += 10000
        s.run_due(waiting)
        self.assertEqual(len(calls), 3)
        s.sync([self.candidate(20)])
        self.now += 8000
        result = s.run_due(lambda jobs: {20: 'waiting'})
        self.assertEqual(result['jobs'][1]['attempts'], 3)
        self.assertEqual(result['jobs'][1]['state'], 'exhausted')
        self.assertIsNone(result['next_due'])

    def test_pending_is_committed_before_callback_and_crash_never_repeats(self):
        s = self.scheduler()
        s.sync([self.candidate()])
        self.now += 300
        def crash(jobs):
            state = self.scheduler().snapshot()['jobs'][0]
            self.assertEqual(state['state'], 'pending')
            self.assertEqual(state['attempts'], 1)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            s.run_due(crash)
        calls = []
        result = self.scheduler().run_due(lambda jobs: calls.append(jobs))
        self.assertEqual(calls, [])
        self.assertEqual(result['counts'], {'needs_review': 1})

    def test_callback_errors_and_partial_invalid_results_stop_whole_round(self):
        for outcome in ('exception', {10: 'imported'}, {10: 'nonsense', 11: 'waiting'}):
            with self.subTest(outcome=outcome):
                self.root = self.root / 'case'
                s = self.scheduler()
                s.sync([self.candidate(), self.candidate(11)])
                self.now += 300
                def check(jobs):
                    if outcome == 'exception':
                        raise RuntimeError('secret external id must not be stored')
                    return outcome
                result = s.run_due(check)
                self.assertEqual(result['counts'], {'needs_review': 2})
                self.assertNotIn('secret', repr(result))
                self.assertIsNone(result['next_due'])
        self.root = self.root / 'finals'
        s = self.scheduler()
        states = ['imported', 'protected', 'conflict', 'unmatched', 'manual_review']
        s.sync([self.candidate(i + 1) for i in range(len(states))])
        self.now += 300
        result = s.run_due(lambda jobs: dict(enumerate(states, 1)))
        self.assertEqual(result['counts'], dict.fromkeys(states, 1))
        self.assertIsNone(result['next_due'])

    def test_account_cooldown_persists_and_pause_refunds_slot(self):
        s = self.scheduler()
        s.sync([self.candidate()])
        self.now += 300
        s.run_due(lambda jobs: {10: 'imported'})
        s.sync([self.candidate(11, observed_at=self.now - 300, started_at=self.now - 400)])
        calls = []
        result = self.scheduler().run_due(lambda jobs: calls.append(jobs))
        self.assertEqual(calls, [])
        self.assertEqual(result['next_due'], self.now + 60)
        self.now += 60
        result = s.run_due(lambda jobs: {'status': 'paused', 'retry_at': self.now + 600})
        self.assertEqual(result['jobs'][1]['attempts'], 0)
        self.assertEqual(result['jobs'][1]['state'], 'scheduled')
        self.assertEqual(result['next_due'], self.now + 600)
        s.run_due(lambda jobs: calls.append(jobs))
        self.assertEqual(calls, [])
        self.now += 600
        self.assertEqual(s.run_due(lambda jobs: {11: 'waiting'})['jobs'][1]['attempts'], 1)

    def test_invalid_stale_future_or_unconfirmed_candidates_never_schedule(self):
        s = self.scheduler()
        rows: list[Any] = [self.candidate(i + 1, **changes) for i, changes in enumerate([
            {'observed_at': self.now + 1}, {'started_at': self.now + 1},
            {'observed_at': float('nan')}, {'observed_at': float('inf')},
            {'observed_at': True}, {'observed_at': self.now - 7 * 86400 - 1},
            {'observed_at': -1}, {'car_id': False}, {'car_id': 0},
            {'session_id': True}, {'session_id': 2**63},
            {'status': 'needs_db_confirmation', 'session_id': 22},
            {'status': 'confirmed_away', 'location': 'home'},
            {'status': 'unknown_location', 'geofence_id': 1},
        ])]
        rows.extend([None, {}, 'junk'])
        self.assertEqual(s.sync(rows)['jobs'], [])
        self.assertEqual(s.sync([self.candidate(100, status='unknown_location', geofence_id=None)])['counts'],
                         {'scheduled': 1})

    def test_capacity_is_explicit_atomic_and_retention_reclaims_storage(self):
        from billing_scheduler import CapacityError
        s = self.scheduler()
        s.sync([self.candidate(i) for i in range(1, 1001)])
        with self.assertRaises(CapacityError):
            s.sync([self.candidate(1001)])
        self.assertEqual(len(s.snapshot()['jobs']), 1000)
        self.now += 7 * 86400 + 1
        calls = []
        self.assertEqual(s.run_due(lambda jobs: calls.append(jobs))['jobs'], [])
        self.assertEqual(calls, [])
        s.sync([self.candidate(2000)])
        self.assertEqual(len(s.snapshot()['jobs']), 1)
        self.assertLess(s.path.stat().st_size, 40000)

    def test_queue_files_are_private_and_symlink_hardlink_paths_rejected(self):
        import os
        import stat
        s = self.scheduler()
        s.sync([self.candidate()])
        self.now += 300
        s.run_due(lambda jobs: {10: 'imported'})
        for path in self.root.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        linked = self.root.parent / 'linked'
        linked.symlink_to(self.root, target_is_directory=True)
        from billing_scheduler import Scheduler
        with self.assertRaises(ValueError):
            Scheduler(linked)
        os.link(s.path, self.root.parent / 'hardlink')
        with self.assertRaises(ValueError):
            self.scheduler()

    def test_process_lock_and_real_process_crash_recovery(self):
        import json
        import subprocess
        import sys
        script = (
            'import json,sys; from billing_scheduler import Scheduler; '
            's=Scheduler(sys.argv[1],clock=lambda:float(sys.argv[2])); '
            'print(json.dumps(s.run_due(lambda jobs: {j["session_id"]:"imported" for j in jobs})))'
        )
        s = self.scheduler()
        s.sync([self.candidate()])
        self.now += 300
        def check(jobs):
            child = subprocess.run([sys.executable, '-c', script, str(self.root), str(self.now)],
                                   capture_output=True, text=True, check=True, timeout=10)
            result = json.loads(child.stdout)
            self.assertEqual(result['status'], 'busy')
            self.assertEqual(result['counts'], {'pending': 1})
            return {10: 'imported'}
        self.assertEqual(s.run_due(check)['counts'], {'imported': 1})
        self.now += 60
        s.sync([self.candidate(11)])
        self.now += 300
        crash_script = (
            'import os,sys; from billing_scheduler import Scheduler; '
            'Scheduler(sys.argv[1],clock=lambda:float(sys.argv[2])).run_due(lambda jobs: os._exit(23))'
        )
        child = subprocess.run([sys.executable, '-c', crash_script, str(self.root), str(self.now)],
                               capture_output=True, timeout=10)
        self.assertEqual(child.returncode, 23)
        calls = []
        result = self.scheduler().run_due(lambda jobs: calls.append(jobs))
        self.assertEqual(calls, [])
        self.assertEqual(result['counts'], {'imported': 1, 'needs_review': 1})

    def test_pause_blocks_account_wide_and_invalid_pause_fails_closed(self):
        s = self.scheduler()
        s.sync([self.candidate()])
        self.now += 300
        s.run_due(lambda jobs: {'status': 'paused', 'retry_at': self.now + 600})
        s.sync([self.candidate(11, observed_at=self.now - 300, started_at=self.now - 400)])
        self.now += 60
        calls = []
        result = s.run_due(lambda jobs: calls.append(jobs))
        self.assertEqual(calls, [])
        self.assertEqual(result['next_due'], self.now + 540)
        self.now += 540
        result = s.run_due(lambda jobs: {'status': 'paused', 'retry_at': float('nan')})
        self.assertEqual(result['counts'], {'needs_review': 2})

    def test_parent_callback_budget_and_auth_states_are_safe_and_utc_bounded(self):
        s = self.scheduler()
        s.sync([self.candidate(10), self.candidate(11), self.candidate(12)])
        self.now += 300
        result = s.run_due(lambda jobs: {10: 'budget_paused', 11: 'auth_blocked', 12: 'protected'})
        tomorrow = (self.now // 86400 + 1) * 86400
        self.assertEqual(result['counts'], {'budget_paused': 1, 'auth_blocked': 1, 'protected': 1})
        self.assertEqual(result['jobs'][0]['attempts'], 0)
        self.assertEqual(result['next_due'], max(tomorrow, self.now + 60))
        self.now += 60
        calls = []
        self.scheduler().run_due(lambda jobs: calls.append(jobs))
        self.assertEqual(calls, [])
        self.now = result['next_due']
        result = self.scheduler().run_due(lambda jobs: {10: 'waiting'})
        self.assertEqual(result['counts'], {'exhausted': 1, 'auth_blocked': 1, 'protected': 1})
        self.assertIsNone(result['next_due'])

    def test_malformed_fields_and_invalid_clock_fail_closed(self):
        s = self.scheduler()
        malformed = [self.candidate(10, status=[]), self.candidate(11, status={}),
                     self.candidate(12, location={}), self.candidate(13, observed_at='bad')]
        self.assertEqual(s.sync(malformed)['jobs'], [])
        for bad in (float('nan'), float('inf'), -1, True, 'yesterday'):
            with self.subTest(clock=bad):
                self.now = bad
                with self.assertRaises(ValueError):
                    s.sync([])
                with self.assertRaises(ValueError):
                    s.run_due(lambda jobs: self.fail('invalid clock must not call reader'))

    def test_home_evidence_revokes_queued_work_and_wins_same_batch(self):
        s = self.scheduler()
        s.sync([self.candidate()])
        home = self.candidate(status='skipped_home', geofence_id=1, location='home')
        result = s.sync([home, self.candidate(event_id='duplicate')])
        self.assertEqual(result['counts'], {'skipped_home': 1})
        self.now += 300
        calls = []
        s.run_due(lambda jobs: calls.append(jobs))
        self.assertEqual(calls, [])
        self.assertIsNone(s.snapshot()['next_due'])

    def test_utc_midnight_pause_obeys_minimum_cooldown_and_import_is_inert(self):
        import subprocess
        import sys
        self.now = (self.now // 86400 + 1) * 86400 - 301
        s = self.scheduler()
        s.sync([self.candidate()])
        self.now += 300  # one second before UTC midnight
        result = s.run_due(lambda jobs: {10: 'budget_paused'})
        self.assertEqual(result['next_due'], self.now + 60)
        self.now += 1
        calls = []
        s.run_due(lambda jobs: calls.append(jobs))
        self.assertEqual(calls, [])
        pristine = self.root.parent / 'import_only'
        pristine.mkdir()
        source = str(Path(__file__).resolve().parent)
        subprocess.run([sys.executable, '-c',
                        'import sys; sys.path.insert(0,sys.argv[1]); import billing_scheduler', source],
                       cwd=pristine, check=True, capture_output=True, timeout=10)
        self.assertEqual(list(pristine.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
