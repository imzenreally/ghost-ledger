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
