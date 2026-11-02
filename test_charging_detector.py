import tempfile
import unittest
from pathlib import Path


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'private'
        self.now = 1000000.0

    def detector(self):
        from charging_detector import Detector
        detector = Detector(self.root, clock=lambda: self.now)
        self.addCleanup(detector.close)
        return detector

    def state(self, detector, value, **kwargs):
        return detector.on_message('teslamate/cars/1/state', value, **kwargs)

    def test_live_transition_is_durable_pending_and_duplicate_safe(self):
        detector = self.detector()
        self.state(detector, 'charging')
        result = self.state(detector, 'online')
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(self.state(detector, 'online')['status'], 'ignored')
        rows = self.detector().snapshot()['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'needs_db_confirmation')
        self.assertEqual(rows[0]['location'], 'unknown')
        self.assertEqual(rows[0]['event_id'], result['event_id'])

    def test_retained_messages_never_arm_or_finish(self):
        detector = self.detector()
        self.state(detector, 'charging', retained=True)
        self.state(detector, 'online')
        self.assertEqual(detector.snapshot()['rows'], [])
        self.state(detector, 'charging')
        self.state(detector, 'online', retained=True)
        self.assertEqual(detector.snapshot()['rows'], [])
        self.assertEqual(self.state(detector, 'online')['status'], 'candidate')

    def test_disconnect_and_restart_forget_arming(self):
        detector = self.detector()
        self.state(detector, 'charging')
        detector.disconnected()
        self.state(detector, 'online')
        self.state(detector, 'charging')
        replacement = self.detector()
        self.state(replacement, 'online')
        self.assertEqual(replacement.snapshot()['rows'], [])

    def test_invalid_inputs_do_not_finish_and_car_contexts_are_isolated(self):
        detector = self.detector()
        self.state(detector, b'charging')
        for topic, value in [(None, 'online'), ('bad', 'online'),
                             ('teslamate/cars/1/state', b'\xff'),
                             ('teslamate/cars/1/state', 'unavailable'),
                             ('teslamate/cars/1/state', 'x' * 5000),
                             ('teslamate/cars/01/state', 'online'),
                             ('teslamate/cars/1/state', {}),
                             ('teslamate/cars/1/state/extra', 'online'),
                             ('teslamate/cars/2/state', 'online')]:
            self.assertEqual(detector.on_message(topic, value)['status'], 'ignored')
        self.assertEqual(self.state(detector, 'asleep')['status'], 'candidate')

    def test_out_of_order_and_obsolete_generation_cannot_finish(self):
        detector = self.detector()
        self.state(detector, 'charging', received_at=self.now, connection_generation=1)
        self.state(detector, 'online', received_at=self.now - 1, connection_generation=1)
        self.assertEqual(detector.snapshot()['rows'], [])
        self.state(detector, 'online', connection_generation=2)
        self.state(detector, 'charging', connection_generation=2)
        self.state(detector, 'online', connection_generation=1)
        self.assertEqual(detector.snapshot()['rows'], [])
        self.assertEqual(self.state(detector, 'online', connection_generation=2)['status'], 'candidate')

    def test_confirmation_requires_completed_matching_db_evidence(self):
        detector = self.detector()
        self.state(detector, 'charging')
        event = self.state(detector, 'online')['event_id']
        result = detector.confirm(event, session_id=10, geofence_id=None, completed=False)
        self.assertEqual(result['status'], 'needs_db_confirmation')
        result = detector.confirm(event, session_id=10, geofence_id=None, completed=True)
        self.assertEqual(result['status'], 'unknown_location')
        result = detector.confirm(event, session_id=10, geofence_id=2, completed=True,
                                  ended_at=self.now)
        self.assertEqual(result['status'], 'confirmed_away')
        self.assertEqual(self.detector().snapshot()['rows'][0]['session_id'], 10)
        self.assertEqual(detector.confirm(event, 10, 1, True)['status'], 'confirmed_away')
        self.assertEqual(detector.confirm('missing', 10, 1, True)['status'], 'not_found')

    def test_home_and_unordered_geofence_hints_do_not_drop_candidates(self):
        detector = self.detector()
        detector.on_message('teslamate/cars/1/geofence', 'Home')
        self.state(detector, 'charging')
        detector.on_message('teslamate/cars/1/geofence', '')
        event = self.state(detector, 'online')['event_id']
        self.assertEqual(detector.snapshot()['rows'][0]['location'], 'unknown')
        self.assertEqual(detector.confirm(event, 2, 1, True)['status'], 'skipped_home')
        self.assertEqual(detector.snapshot()['rows'][0]['location'], 'home')

    def test_store_is_private_and_rejects_symlinks(self):
        detector = self.detector()
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(detector.path.stat().st_mode & 0o777, 0o600)
        from charging_detector import Detector
        linked = self.root.parent / 'link'
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            Detector(linked)
        other = self.root.parent / 'other'
        other.mkdir(mode=0o700)
        target = other / 'target'
        target.write_text('untouched')
        (other / 'charging_candidates.sqlite3').symlink_to(target)
        with self.assertRaises(ValueError):
            Detector(other)
        self.assertEqual(target.read_text(), 'untouched')

    def test_retention_and_count_bound_are_durable(self):
        detector = self.detector()
        detector.MAX_CANDIDATES = 3
        for _ in range(5):
            self.state(detector, 'charging')
            self.state(detector, 'online')
            self.now += 1
        self.assertEqual(len(detector.snapshot()['rows']), 3)
        self.assertEqual(len(self.detector().snapshot()['rows']), 3)
        self.now += detector.RETENTION_SECONDS + 1
        self.assertEqual(detector.snapshot()['rows'], [])
        self.assertEqual(self.detector().snapshot()['rows'], [])

    def test_namespace_is_exact_and_configurable(self):
        from charging_detector import Detector
        detector = Detector(self.root, clock=lambda: self.now, namespace='garage')
        self.addCleanup(detector.close)
        self.state(detector, 'charging')
        topic = 'teslamate/garage/cars/1/state'
        detector.on_message(topic, 'online')
        self.assertEqual(detector.snapshot()['rows'], [])
        detector.on_message(topic, 'charging')
        self.assertEqual(detector.on_message(topic, 'online')['status'], 'candidate')
        with self.assertRaises(ValueError):
            Detector(self.root, namespace='../bad')

    def test_storage_failure_does_not_acknowledge_or_disarm(self):
        detector = self.detector()
        self.state(detector, 'charging')
        import sqlite3
        blocker = sqlite3.connect(detector.path)
        self.addCleanup(blocker.close)
        detector._db.execute('PRAGMA busy_timeout=1')
        blocker.execute('BEGIN IMMEDIATE')
        self.assertEqual(self.state(detector, 'online')['status'], 'storage_error')
        blocker.rollback()
        self.assertEqual(self.state(detector, 'online')['status'], 'candidate')
        self.assertEqual(len(detector.snapshot()['rows']), 1)

    def test_unsafe_existing_permissions_and_sidecars_are_rejected(self):
        from charging_detector import Detector
        self.root.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            Detector(self.root)
        self.root.chmod(0o700)
        detector = self.detector()
        detector.close()
        sidecar = Path(str(detector.path) + '-journal')
        sidecar.symlink_to(self.root / 'unrelated')
        with self.assertRaises(ValueError):
            Detector(self.root)
        sidecar.unlink()
        detector.path.chmod(0o644)
        with self.assertRaises(ValueError):
            Detector(self.root)

    def test_nonsensical_timestamps_do_not_escape_callback(self):
        detector = self.detector()
        self.state(detector, 'charging')
        for value in [float('nan'), float('inf'), True, 'yesterday', 10 ** 1000]:
            self.assertEqual(self.state(detector, 'online', received_at=value)['status'], 'ignored')
        self.assertEqual(detector.snapshot()['rows'], [])

    def test_default_limit_is_one_thousand_with_stable_recent_ids(self):
        detector = self.detector()
        latest = None
        for _ in range(1005):
            self.state(detector, 'charging')
            latest = self.state(detector, 'online')['event_id']
        rows = detector.snapshot()['rows']
        self.assertEqual(len(rows), 1000)
        self.assertEqual(rows[-1]['event_id'], latest)
        self.assertEqual(len({row['event_id'] for row in rows}), 1000)
        self.assertEqual(self.detector().snapshot()['rows'][-1]['event_id'], latest)

    def test_confirmation_rejects_session_rebinding_and_oversized_ids(self):
        detector = self.detector()
        self.state(detector, 'charging')
        event = self.state(detector, 'online')['event_id']
        detector.confirm(event, 10, None, True)
        self.assertEqual(detector.confirm(event, 11, 2, True)['status'], 'invalid_confirmation')
        self.assertEqual(detector.confirm(event, 10 ** 100, 2, True)['status'], 'invalid_confirmation')
        self.assertEqual(detector.snapshot()['rows'][0]['session_id'], 10)
        self.assertEqual(detector.confirm(event, 10, 2, True)['status'], 'confirmed_away')


if __name__ == '__main__':
    unittest.main()
