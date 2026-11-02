import unittest

class WorkerTests(unittest.TestCase):
    def test_scheduler_preserves_receipts_for_the_billing_callback(self):
        from billing_scheduler import Scheduler
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            clock=[1000.0]
            scheduler=Scheduler(Path(tmp).resolve(),clock=lambda:clock[0])
            scheduler.sync([{'status':'confirmed_away','car_id':1,'session_id':42,
                             'started_at':900.0,'observed_at':1000.0,'geofence_id':2}])
            clock[0]=1300.0
            calls=[]
            scheduler.run_due(lambda jobs:(calls.extend(jobs) or {42:'waiting'}))
            self.assertEqual(calls[0]['started_at'],900.0)
            self.assertEqual(calls[0]['observed_at'],1000.0)

    def test_due_batches_are_bounded_for_the_callback(self):
        from billing_scheduler import Scheduler
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            clock=[1000.0]; scheduler=Scheduler(Path(tmp).resolve(),clock=lambda:clock[0])
            scheduler.sync([{'status':'confirmed_away','car_id':1,'session_id':i,
                             'started_at':900.0,'observed_at':1000.0,'geofence_id':2} for i in range(1,26)])
            clock[0]=1300.0; sizes=[]
            scheduler.run_due(lambda jobs:(sizes.append(len(jobs)) or {j['session_id']:'protected' for j in jobs}))
            self.assertEqual(sizes,[20])

    def test_daily_and_notifications_are_added_without_removing_event_checks(self):
        from billing_worker import maintenance_cycle
        calls=[]
        class Detector:
            def snapshot(self):return {'rows':[]}
        class Scheduler:
            def sync(self,rows):calls.append('sync')
            def run_due(self,check):calls.append('event');return {'status':'ok','jobs':[]}
        class Daily:
            def run_due(self,check):calls.append('daily');return {'status':'ok'}
        class Notifier:
            def observe(self,store,event,daily,passive):calls.append('notify');return {'status':'ok'}
        result=maintenance_cycle(Detector(),Scheduler(),lambda jobs:None,Daily(),Notifier(),object(),{})
        self.assertEqual(calls,['daily','sync','event','notify'])
        self.assertEqual(result['event']['status'],'ok')

    def test_notification_failure_does_not_block_billing_or_daily(self):
        from billing_worker import maintenance_cycle
        from unittest.mock import Mock
        detector=Mock();detector.snapshot.return_value={'rows':[]}
        scheduler=Mock();scheduler.run_due.return_value={'status':'ok','jobs':[]}
        daily=Mock();daily.run_due.side_effect=RuntimeError('private database error')
        notifier=Mock();notifier.observe.side_effect=RuntimeError('private delivery error')
        result=maintenance_cycle(detector,scheduler,lambda jobs:None,daily,notifier,object(),{})
        scheduler.run_due.assert_called_once()
        self.assertEqual(result['event']['status'],'ok')
        self.assertEqual(result['daily']['status'],'needs_review')
        self.assertEqual(result['notifications']['status'],'blocked')
        self.assertNotIn('private',str(result))

    def test_empty_detection_never_calls_billing(self):
        from billing_worker import cycle
        calls=[]
        class Detector:
            def snapshot(self):return {'rows':[]}
        class Scheduler:
            def sync(self,rows):self.rows=rows
            def run_due(self,check):return {'status':'idle'}
        result=cycle(Detector(),Scheduler(),lambda jobs:calls.append(jobs))
        self.assertEqual(result,{'status':'idle'})
        self.assertEqual(calls,[])

    def test_worker_passes_detector_rows_to_scheduler_not_to_fleet(self):
        from billing_worker import cycle
        rows=[{'event_id':'synthetic','session_id':42,'status':'skipped_home'}]
        class Detector:
            def snapshot(self):return {'rows':rows}
        class Scheduler:
            def sync(self,values):self.rows=values
            def run_due(self,check):
                self.assert_same=self.rows==rows
                return {'status':'idle'}
        scheduler=Scheduler()
        cycle(Detector(),scheduler,lambda jobs:self.fail('not due'))
        self.assertTrue(scheduler.assert_same)
