"""Bounded billing worker, isolated from the MQTT subscriber's event loop.

No polling without a confirmed completion job. No vehicle endpoints, invoice
PDF requests, or MQTT publishing. Token renewal is owned by the passive service.
"""
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import signal
import time

from auth.runtime import _private_dir, _atomic_json
from manual_reader import private_path


def cycle(detector, scheduler, check):
    scheduler.sync(detector.snapshot()['rows'])
    return scheduler.run_due(check)


def run(root):
    from charging_detector import Detector
    from billing_scheduler import Scheduler
    from automatic_billing import check_jobs
    root=_private_dir(root)
    fd=os.open(private_path(root/'billing-worker.lock',create=True),os.O_RDWR|os.O_NOFOLLOW)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    detector=Detector(root)
    scheduler=Scheduler(root)
    running=True
    def stop(*args):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        while running:
            try:
                result=cycle(detector,scheduler,lambda jobs:check_jobs(root,jobs))
            except Exception:
                # Keep provider/database exception strings out of process logs.
                result={'status':'blocked','reason':'local_operation_failed_requires_review'}
            _atomic_json(root,'billing-status.json',{'updated_at':datetime.now(timezone.utc).isoformat(),
                'enabled':True,'delay_minutes':[5,30,120],'bill_check':result,
                'vehicle_requests_enabled':False,'invoice_downloads_enabled':False})
            deadline=time.monotonic()+30
            while running and time.monotonic()<deadline:
                time.sleep(.2)
    finally:
        detector.close()
        if hasattr(scheduler,'close'):scheduler.close()
        os.close(fd)


if __name__=='__main__':
    try:
        run(Path.home()/'.local/share/ghost-ledger')
    except Exception:
        raise SystemExit('Billing worker stopped: unsafe configuration or state; inspect private local status') from None
