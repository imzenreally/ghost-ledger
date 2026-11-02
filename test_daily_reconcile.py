"""Offline synthetic tests: no Tesla, SSH, or production database access."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest


def epoch(text):
    return datetime.fromisoformat(text).timestamp()


class Database:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries = []
        self.closed = self.cursor_closed = self.rolled_back = False
        self.enters = 0

    def __enter__(self):
        self.enters += 1
        return self

    def __exit__(self, *args):
        pass

    def connect(self):
        return self

    def cursor(self):
        return Cursor(self)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class Cursor:
    def __init__(self, db):
        self.db = db

    def execute(self, sql, params=()):
        self.db.queries.append((sql, params))

    def fetchall(self):
        return self.db.rows

    def close(self):
        self.db.cursor_closed = True


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.now = epoch('2026-02-03T07:59:59+00:00')
        self.database = Database()
        self.calls = []

    def daily(self, **kwargs):
        from daily_reconcile import DailyReconciler
        return DailyReconciler(self.root, clock=lambda: self.now, **kwargs)

    def check(self, jobs):
        self.calls.append(jobs)
        return {j['session_id']: 'waiting' for j in jobs}

    def run_daily(self, daily=None):
        return (daily or self.daily()).run_due(self.check, database_factory=lambda: self.database)

    def row(self, sid=1, **changes):
        row = dict(id=sid, car_id=3,
                   start_date=datetime.fromtimestamp(self.now - 14400, timezone.utc).replace(tzinfo=None),
                   end_date=datetime.fromtimestamp(self.now - 10800, timezone.utc).replace(tzinfo=None),
                   geofence_id=None, cost=None)
        row.update(changes)
        return row

    def test_recent_discovery_query_and_actual_timestamps_batched_twenty(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row(i) for i in range(1, 44)]
        result = self.run_daily()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([len(batch) for batch in self.calls], [20, 20, 3])
        first = self.calls[0][0]
        self.assertEqual(first, dict(session_id=1, car_id=3, started_at=self.now - 14400,
                                     observed_at=self.now - 10800, source='daily_db_reconciliation'))
        sql, params = self.database.queries[1]
        self.assertEqual(self.database.queries[0][0], 'SET TRANSACTION READ ONLY')
        self.assertTrue(sql.startswith('SELECT cp.id, cp.car_id, cp.start_date, cp.end_date, cp.geofence_id, cp.cost FROM charging_processes cp'))
        for fragment in ('cp.cost IS NULL', 'cp.geofence_id IS DISTINCT FROM 1',
                         'cp.end_date >= %s', 'cp.end_date <= %s', 'LIMIT 1001'):
            self.assertIn(fragment, sql)
        self.assertEqual(params, (datetime.fromtimestamp(self.now - 7 * 86400, timezone.utc).replace(tzinfo=None),
                                  datetime.fromtimestamp(self.now - 7200, timezone.utc).replace(tzinfo=None)))
        self.assertFalse(self.database.autocommit)
        self.assertTrue(self.database.closed and self.database.cursor_closed and self.database.rolled_back)
        self.assertEqual(result['counts'], {'waiting': 43})

    def test_daily_backstop_does_not_preempt_initial_two_hour_window(self):
        self.now=epoch('2026-02-03T12:00:00+00:00')
        self.database.rows=[self.row(9, start_date=datetime.fromtimestamp(self.now-3600,timezone.utc),
                                     end_date=datetime.fromtimestamp(self.now-60,timezone.utc))]
        self.assertEqual(self.run_daily()['status'],'completed')
        self.assertEqual(self.calls,[])

    def test_defensive_discovery_rejects_home_cost_old_future_and_invalid(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row(1, geofence_id=1), self.row(2, cost=0),
                              self.row(3, end_date=datetime.fromtimestamp(self.now - 8 * 86400, timezone.utc)),
                              self.row(4, end_date=datetime.fromtimestamp(self.now + 1, timezone.utc)),
                              self.row(5, start_date=None), self.row(6, car_id=True),
                              self.row(7, start_date=datetime.fromtimestamp(self.now, timezone.utc))]
        self.assertEqual(self.run_daily()['status'], 'completed')
        self.assertEqual(self.calls, [])

    def test_overflow_is_explicit_no_truncated_processing(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row(i) for i in range(1, 1002)]
        result = self.run_daily()
        self.assertEqual(result['status'], 'overflow')
        self.assertTrue(result['last_run']['overflow'])
        self.assertEqual(result['last_run']['discovered'], 1001)
        self.assertEqual(self.calls, [])

    def test_ambiguous_callback_stops_remaining_batches_and_persists_review(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row(i) for i in range(1, 44)]
        daily = self.daily()
        def unknown_commit(jobs):
            self.assertEqual(daily.snapshot()['last_run']['state'], 'pending')
            self.assertEqual(daily.snapshot()['counts']['pending'], 20)
            raise RuntimeError('private exception must not escape')
        result = daily.run_due(unknown_commit, lambda: self.database)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['counts'], {'needs_review': 20, 'scheduled': 23})
        self.assertNotIn('private exception', str(result))
        self.assertEqual(self.run_daily()['status'], 'needs_review')
        self.assertEqual(self.database.enters, 1)
        self.assertEqual(self.calls, [])

    def test_abrupt_death_pending_recovers_without_repeating_callback(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row()]
        def crash(jobs):
            raise SystemExit()
        with self.assertRaises(SystemExit):
            self.daily().run_due(crash, lambda: self.database)
        self.assertEqual(self.daily().snapshot()['counts'], {'pending': 1})
        self.assertEqual(self.run_daily()['status'], 'needs_review')
        self.now += 86400
        self.assertEqual(self.run_daily()['counts'], {'needs_review': 1})
        self.assertEqual(self.calls, [])

    def test_discovery_failure_is_sanitized_consumes_daily_reservation(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        def fail():
            self.assertEqual(self.daily().snapshot()['last_run']['state'], 'pending')
            raise ValueError('secret database message')
        result = self.daily().run_due(self.check, fail)
        self.assertEqual(result['status'], 'needs_review')
        self.assertNotIn('secret', str(result))
        self.assertEqual(self.run_daily()['status'], 'needs_review')
        self.assertEqual(self.database.enters, 0)

    def test_partial_callback_results_stop_for_review(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row()]
        result = self.daily().run_due(lambda jobs: {}, lambda: self.database)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['counts'], {'needs_review': 1})

    def test_budget_paused_stops_round_and_retries_only_next_local_day(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row(i) for i in range(1, 44)]
        calls = []
        def pause(jobs):
            calls.append(jobs)
            return {j['session_id']: 'budget_paused' for j in jobs}
        result = self.daily().run_due(pause, lambda: self.database)
        self.assertEqual(result['status'], 'budget_paused')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.run_daily()['status'], 'budget_paused')
        self.assertEqual(self.calls, [])
        self.now += 86400
        self.assertEqual(self.run_daily()['status'], 'completed')
        self.assertEqual([len(b) for b in self.calls], [20, 20, 3])

    def test_waiting_retries_next_day_but_manual_review_is_not_retried(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row(1), self.row(2)]
        self.daily().run_due(lambda jobs: {1: 'waiting', 2: 'manual_review'}, lambda: self.database)
        self.run_daily()
        self.assertEqual(self.calls, [])
        self.now += 86400
        self.run_daily()
        self.assertEqual([j['session_id'] for j in self.calls[0]], [1])

    def test_finite_scheduler_blocked_states_read_only_waiting_exhausted_retry(self):
        import sqlite3
        from billing_scheduler import Scheduler
        self.now = epoch('2026-02-03T12:00:00+00:00')
        scheduler = Scheduler(self.root, clock=lambda: self.now)
        states = ['manual_review', 'auth_blocked', 'conflict', 'needs_review', 'pending', 'waiting', 'exhausted']
        with sqlite3.connect(scheduler.path) as db:
            for sid, state in enumerate(states, 1):
                db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?)', (sid, 3, self.now - 10800, state, 3, None, self.now - 14400))
        before = scheduler.snapshot()
        self.database.rows = [self.row(i) for i in range(1, 9)]
        self.run_daily()
        self.assertEqual([j['session_id'] for j in self.calls[0]], [6, 7, 8])
        self.assertEqual(scheduler.snapshot(), before)

    def test_retention_seven_days_and_sqlite_reclaims_pages(self):
        import sqlite3
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row()]
        daily = self.daily()
        self.run_daily(daily)
        self.now += 8 * 86400
        self.database.rows = []
        self.run_daily(daily)
        self.assertEqual(daily.snapshot()['jobs'], [])
        with sqlite3.connect(daily.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA auto_vacuum').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA freelist_count').fetchone()[0], 0)

    def test_invalid_clocks_rejected_before_database_or_callback(self):
        for value in (True, float('nan'), float('inf'), -1, 'later'):
            with self.subTest(value=value):
                self.now = value
                with self.assertRaises(ValueError):
                    self.run_daily()
        self.assertEqual(self.database.enters, 0)
        self.assertEqual(self.calls, [])

    def test_local_eight_am_dst_and_midnight_not_fixed_utc_or_twentyfour_hours(self):
        daily = self.daily(timezone_name='Europe/Berlin')
        for before, due in [('2026-03-28T06:59:59+00:00', '2026-03-28T07:00:00+00:00'),
                            ('2026-03-29T05:59:59+00:00', '2026-03-29T06:00:00+00:00'),
                            ('2026-10-24T05:59:59+00:00', '2026-10-24T06:00:00+00:00'),
                            ('2026-10-25T06:59:59+00:00', '2026-10-25T07:00:00+00:00')]:
            entered = self.database.enters
            self.now = epoch(before)
            self.run_daily(daily)
            self.assertEqual(self.database.enters, entered)
            self.now = epoch(due)
            self.run_daily(daily)
            self.assertEqual(self.database.enters, entered + 1)
        self.now = epoch('2026-10-25T23:30:00+00:00')  # next local date, before 08
        self.run_daily(daily)
        self.assertEqual(self.database.enters, 4)

    def test_folded_hour_runs_only_once_for_same_local_date(self):
        daily = self.daily(timezone_name='Europe/Berlin', hour=2)
        self.now = epoch('2026-10-25T00:30:00+00:00')
        self.run_daily(daily)
        self.now = epoch('2026-10-25T01:30:00+00:00')
        self.run_daily(daily)
        self.assertEqual(self.database.enters, 1)

    def test_reservation_commit_failure_never_discovers_and_is_durable_review(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        import sqlite3
        self.now = epoch('2026-02-03T12:00:00+00:00')
        daily = self.daily()
        original = daily._connect
        for commit_known in (False, True):
            with self.subTest(commit_known=commit_known):
                self.now += 86400
                calls = 0
                @contextmanager
                def fail_reservation():
                    nonlocal calls
                    calls += 1
                    this_call = calls
                    with original() as db:
                        yield db
                        if this_call == 2 and not commit_known:
                            raise sqlite3.OperationalError('synthetic private commit failure')
                    if this_call == 2 and commit_known:
                        raise sqlite3.OperationalError('synthetic private commit failure')
                with patch.object(daily, '_connect', fail_reservation):
                    result = self.run_daily(daily)
                self.assertEqual(result['status'], 'needs_review')
                self.assertEqual(self.run_daily()['status'], 'needs_review')
        self.assertEqual(self.database.enters, 0)
        self.assertEqual(self.calls, [])

    def test_process_lock_blocks_discovery_and_reentrant_callback(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row()]
        daily = self.daily()
        def check(jobs):
            self.assertEqual(self.run_daily()['status'], 'busy')
            return {1: 'waiting'}
        self.assertEqual(daily.run_due(check, lambda: self.database)['status'], 'completed')
        self.assertEqual(self.database.enters, 1)

    def test_private_state_paths_reject_links_and_public_modes(self):
        import os
        from daily_reconcile import DailyReconciler
        daily = self.daily()
        self.assertEqual(daily.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(daily.lock_path.stat().st_mode & 0o777, 0o600)
        daily.path.chmod(0o644)
        with self.assertRaises(ValueError):
            DailyReconciler(self.root)
        daily.path.chmod(0o600)
        link = self.root / 'link'
        link.symlink_to(daily.path)
        daily.path.unlink()
        daily.path.symlink_to(link)
        with self.assertRaises(ValueError):
            DailyReconciler(self.root)
        daily.path.unlink()
        link.unlink()
        daily.path.touch(mode=0o600)
        os.link(daily.path, link)
        with self.assertRaises(ValueError):
            DailyReconciler(self.root)

    def test_discovery_query_exception_closes_cursor_rolls_back_and_closes(self):
        from unittest.mock import patch
        self.now = epoch('2026-02-03T12:00:00+00:00')
        with patch.object(Cursor, 'fetchall', side_effect=RuntimeError('private database error')):
            result = self.run_daily()
        self.assertEqual(result['status'], 'needs_review')
        self.assertTrue(self.database.closed and self.database.cursor_closed and self.database.rolled_back)
        self.assertEqual(self.calls, [])

    def test_real_callback_shares_four_daily_requests_and_stops_without_overrun(self):
        from automatic_billing import check_jobs
        from auth import runtime
        from test_automatic_billing import SyntheticDatabase
        from decimal import Decimal
        import sqlite3
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row(i) for i in range(1, 121)]
        targets = [dict(r, vin='SYNTHETIC-VEHICLE', charge_energy_used=Decimal('20')) for r in self.database.rows]
        target_database = SyntheticDatabase(targets)
        runtime.save_tokens(self.root, dict(access_token='synthetic', refresh_token='synthetic-refresh', expires_in=3600), clock=lambda: self.now)
        requests = []
        def transport(*args):
            requests.append(args)
            return 200, b'{"response":{"data":[]}}'
        def check(jobs):
            return check_jobs(self.root, jobs, database_factory=lambda: target_database,
                              clock=lambda: self.now, transport=transport)
        result = self.daily().run_due(check, lambda: self.database)
        self.assertEqual(result['status'], 'budget_paused')
        self.assertEqual(len(requests), 4)
        self.assertEqual(result['counts'], {'waiting': 80, 'budget_paused': 20, 'scheduled': 20})
        with sqlite3.connect(self.root / 'manual-budget.sqlite3') as db:
            self.assertEqual(db.execute("SELECT used FROM allowance WHERE kind='day'").fetchone()[0], 4)
        self.assertEqual(target_database.backups, [])

    def test_all_callback_errors_stop_remaining_batches_without_retrying_review(self):
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [self.row(i) for i in range(1, 44)]
        calls = []
        def blocked(jobs):
            calls.append(jobs)
            return {j['session_id']: 'auth_blocked' for j in jobs}
        result = self.daily().run_due(blocked, lambda: self.database)
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['counts'], {'auth_blocked': 20, 'scheduled': 23})

    def test_tuple_rows_supported_and_duplicate_identity_fails_closed(self):
        from daily_reconcile import COLUMNS
        self.now = epoch('2026-02-03T12:00:00+00:00')
        self.database.rows = [tuple(self.row()[key] for key in COLUMNS)]
        self.assertEqual(self.run_daily()['counts'], {'waiting': 1})
        self.now += 86400
        self.database.rows = [self.row(2), self.row(2)]
        self.assertEqual(self.run_daily()['status'], 'needs_review')
        self.assertEqual(len(self.calls), 1)

    def test_callback_crossing_midnight_keeps_reserved_date_and_real_end_time(self):
        self.now = epoch('2026-02-03T23:59:59+00:00')
        self.database.rows = [self.row()]
        def slow(jobs):
            self.assertEqual(jobs[0]['observed_at'], self.now - 10800)
            self.now += 2
            return {1: 'waiting'}
        result = self.daily().run_due(slow, lambda: self.database)
        self.assertEqual(result['last_run']['local_date'], '2026-02-03')
        self.run_daily()
        self.assertEqual(self.database.enters, 1)

    def test_unavailable_state_store_returns_safe_review_without_discovery(self):
        from unittest.mock import patch
        import sqlite3
        self.now = epoch('2026-02-03T12:00:00+00:00')
        daily = self.daily()
        with patch.object(daily, '_connect', side_effect=sqlite3.OperationalError('private state pathname')):
            result = self.run_daily(daily)
        self.assertEqual(result['status'], 'needs_review')
        self.assertNotIn('pathname', str(result))
        self.assertEqual(self.database.enters, 0)
        self.assertEqual(self.calls, [])

    def test_eight_am_empty_scan_reserved_once_with_no_callback(self):
        daily = self.daily()
        self.assertEqual(self.run_daily(daily)['status'], 'not_due')
        self.assertEqual(self.database.enters, 0)
        self.now += 1
        self.assertEqual(self.run_daily(daily)['status'], 'completed')
        self.now += 4 * 3600
        self.assertEqual(self.run_daily()['status'], 'completed')
        self.assertEqual(self.database.enters, 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(daily.snapshot()['last_run']['local_date'], '2026-02-03')


if __name__ == '__main__':
    unittest.main()
