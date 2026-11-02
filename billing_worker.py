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

from auth.runtime import _private_dir, _atomic_json, _read_private
from manual_reader import private_path


def cycle(detector, scheduler, check):
    scheduler.sync(detector.snapshot()['rows'])
    return scheduler.run_due(check)


def maintenance_cycle(detector, scheduler, check, daily, notifier, store, passive_status):
    # Daily safety net runs before event work so it can use the shared allowance.
    # Failures stay separate: a notification outage must never prevent billing.
    try:
        daily_result=daily.run_due(check)
    except Exception:
        daily_result={'status':'needs_review','reason':'daily_operation_failed'}
    try:
        event_result=cycle(detector,scheduler,check)
    except Exception:
        event_result={'status':'blocked','reason':'local_operation_failed_requires_review'}
    try:
        notification_result=notifier.observe(store,event_result,daily_result,passive_status)
    except Exception:
        notification_result={'status':'blocked','reason':'notification_operation_failed'}
    return {'event':event_result,'daily':daily_result,'notifications':notification_result}


def run(root):
    from charging_detector import Detector
    from billing_scheduler import Scheduler
    from automatic_billing import check_jobs
    from daily_reconcile import DailyReconciler
    from notifications import Notifier
    from ledger_store import LedgerStore
    root=_private_dir(root)
    fd=os.open(private_path(root/'billing-worker.lock',create=True),os.O_RDWR|os.O_NOFOLLOW)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    detector=Detector(root)
    scheduler=Scheduler(root)
    daily=DailyReconciler(root,timezone_name=os.environ.get('GHOST_LEDGER_TIMEZONE','UTC'),hour=8)
    notifier=Notifier(root,credential_path=os.environ.get('GHOST_LEDGER_PUSHOVER_KEYS_FILE'))
    store=LedgerStore(root/'ledger.sqlite3')
    running=True
    def stop(*args):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    try:
        while running:
            try:
                passive=_read_private(root/'passive-status.json')
            except Exception:
                passive={'renewal':{'status':'unknown'}}
            results=maintenance_cycle(detector,scheduler,lambda jobs:check_jobs(root,jobs),
                                      daily,notifier,store,passive)
            result=results['event']
            _atomic_json(root,'billing-status.json',{'updated_at':datetime.now(timezone.utc).isoformat(),
                'enabled':True,'delay_minutes':[5,30,120],'bill_check':result,
                'daily_reconciliation':results['daily'],'notifications':results['notifications'],
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
