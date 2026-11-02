"""Own-token maintenance and subscribe-only TeslaMate detection. NO billing actions.

All Detector operations and MQTT callbacks run on this one owner thread. Local
receipt times are hints, never fabricated Tesla session timestamps. A live-only
subscriber may miss sessions during downtime; no historical backfill is implied.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import time
import ipaddress
import socket
import subprocess

import paho.mqtt.client as mqtt
from auth.refresh import ensure_fresh
from auth.runtime import _private_dir, _read_private, _atomic_json
from charging_detector import Detector
from manual_reader import private_path
from owner_database import OwnerDatabase


def read_client_id(path):
    path=Path(path).expanduser().absolute()
    if any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('unsafe credential path')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd) as f:
        info=os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
            raise ValueError('credential file must be private and owned')
        text=f.read(16385)
    if len(text)>16384:
        raise ValueError('invalid credential configuration')
    for line in text.splitlines():
        if line.startswith('TESLA_CLIENT_ID='):
            value=line.split('=',1)[1].strip().strip('\"\'')
            if value and len(value)<=1024 and all(ord(c)>=33 for c in value):
                return value
    raise ValueError('client ID required')


def confirm_candidates(detector, database_factory=OwnerDatabase):
    candidates=[r for r in detector.snapshot()['rows'] if r['status']=='needs_db_confirmation'][:20]
    if not candidates:
        return {'confirmed':0,'pending':0}
    confirmed=0
    with database_factory() as database:
        conn=database.connect()
        try:
            cursor=conn.cursor()
            try:
                cursor.execute('SET TRANSACTION READ ONLY')
                for event in candidates:
                    # DB timestamps are naive UTC; MQTT first charging receipt can
                    # be later than actual start. A completed DB interval must cover
                    # that receipt and end close to the observed state transition.
                    dt=lambda seconds: datetime.fromtimestamp(seconds,timezone.utc).replace(tzinfo=None)
                    cursor.execute('''SELECT cp.id, cp.geofence_id,
                        extract(epoch FROM cp.end_date AT TIME ZONE 'UTC')
                        FROM charging_processes cp
                        WHERE cp.car_id = %s AND cp.end_date IS NOT NULL
                          AND cp.start_date <= %s AND cp.end_date >= %s
                          AND cp.end_date BETWEEN %s AND %s
                        ORDER BY cp.end_date DESC LIMIT 2''',
                        (event['car_id'],dt(event['started_at']+5),dt(event['started_at']-5),
                         dt(event['observed_at']-300),dt(event['observed_at']+60)))
                    rows=cursor.fetchall()
                    if len(rows)==1:
                        sid,geofence,ended=rows[0]
                        detector.confirm(event['event_id'],sid,geofence,True,ended_at=float(ended))
                        confirmed+=1
                conn.rollback()
            finally:
                cursor.close()
        finally:
            conn.close()
    return {'confirmed':confirmed,'pending':len(candidates)-confirmed}


def broker_tunnel_arguments(config, port):
    host=str(ipaddress.IPv4Address(config['host']))
    remote_port=int(config['port'])
    if not 1 <= remote_port <= 65535 or not 1 <= port <= 65535:
        raise ValueError('invalid MQTT port')
    return ['/usr/bin/ssh','-N','-o','BatchMode=yes','-o','ConnectTimeout=5',
            '-o','ExitOnForwardFailure=yes','-o','ControlMaster=no','-o','ControlPath=none',
            '-o','ControlPersist=no','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=2',
            '-L',f'127.0.0.1:{port}:{host}:{remote_port}','dockerhost']


def open_broker_tunnel(config):
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
    process=subprocess.Popen(broker_tunnel_arguments(config,port),stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    deadline=time.monotonic()+8
    while time.monotonic()<deadline and process.poll() is None:
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.2):
                return process,port
        except OSError:
            time.sleep(.05)
    process.terminate()
    try:process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill();process.wait(timeout=3)
    raise ValueError('private MQTT tunnel failed')


def run(root, credential_path):
    root=_private_dir(root)
    lockpath=private_path(root/'passive-service.lock',create=True)
    fd=os.open(lockpath,os.O_RDWR|os.O_NOFOLLOW)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    config=_read_private(root/'mqtt-config.json')
    client_id=read_client_id(credential_path)
    detector=Detector(root,namespace=config.get('namespace',''))
    connected=False; generation=0; running=True; renewal={}; db_status={'status':'idle'}
    mqtt_status={'phase':'starting','subscription_confirmed':False,'retained_packets':0,'live_packets':0}
    tunnel=None; tunnel_port=None
    client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id='ghost-ledger-passive',clean_session=True)
    if config.get('username'):
        client.username_pw_set(config['username'],config.get('password'))
    def on_connect(c,u,flags,reason,properties):
        nonlocal connected,generation
        detector.disconnected();generation+=1;connected=not reason.is_failure
        mqtt_status.update(phase='connected' if connected else 'rejected',reason_code=reason.value)
        if connected:
            prefix='teslamate/'+(config['namespace']+'/' if config.get('namespace') else '')+'cars/'
            c.subscribe(prefix+'+/state',qos=1)
    def on_disconnect(c,u,flags,reason,properties):
        nonlocal connected
        connected=False;detector.disconnected()
        mqtt_status.update(phase='disconnected',reason_code=reason.value,subscription_confirmed=False)
    def on_subscribe(c,u,mid,reasons,properties):
        mqtt_status['subscription_confirmed']=bool(reasons) and all(not r.is_failure for r in reasons)
    def on_message(c,u,message):
        key='retained_packets' if message.retain else 'live_packets'
        mqtt_status[key]=min(mqtt_status[key]+1,1000000000)
        detector.on_message(message.topic,message.payload,retained=message.retain,connection_generation=generation)
    client.on_connect=on_connect;client.on_disconnect=on_disconnect;client.on_message=on_message
    client.on_subscribe=on_subscribe
    def stop(*args):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    next_refresh=next_confirm=next_connect=next_status=0
    try:
        while running:
            now=time.monotonic()
            if now>=next_refresh:
                renewal=ensure_fresh(root,client_id)
                next_refresh=time.monotonic()+60
            if not connected and now>=next_connect:
                detector.disconnected()
                try:
                    if tunnel is None or tunnel.poll() is not None:
                        tunnel,tunnel_port=open_broker_tunnel(config)
                    client.connect('127.0.0.1',tunnel_port,keepalive=120)
                except Exception as error:
                    mqtt_status.update(phase='connection_failed',error_type=type(error).__name__,errno=getattr(error,'errno',None))
                next_connect=time.monotonic()+30
            result=client.loop(timeout=1)
            if result!=mqtt.MQTT_ERR_SUCCESS:
                connected=False;detector.disconnected();time.sleep(.2)
            now=time.monotonic()
            if now>=next_confirm:
                try:
                    db_status={'status':'ok',**confirm_candidates(detector)}
                except Exception:
                    db_status={'status':'unavailable','reason':'read_only_confirmation_failed'}
                next_confirm=time.monotonic()+60
            if now>=next_status:
                snap=detector.snapshot()
                _atomic_json(root,'passive-status.json',{
                    'updated_at':datetime.now(timezone.utc).isoformat(),
                    'mqtt_connected':connected,'mqtt_status':mqtt_status,'subscription':'state_only','mqtt_publish_enabled':False,
                    'renewal':renewal,'confirmation':db_status,'coverage':snap['coverage'],
                    'candidate_counts':{s:sum(r['status']==s for r in snap['rows']) for s in sorted({r['status'] for r in snap['rows']})},
                    'billing_actions_in_this_process':False,'billing_worker_status_file':'billing-status.json'})
                next_status=time.monotonic()+10
    finally:
        client.disconnect();detector.disconnected();detector.close();os.close(fd)
        if tunnel is not None:
            tunnel.terminate()
            try:tunnel.wait(timeout=3)
            except subprocess.TimeoutExpired:
                tunnel.kill();tunnel.wait(timeout=3)


if __name__=='__main__':
    try:
        run(Path.home()/'.local/share/ghost-ledger',Path(__file__).resolve().parent/'.env')
    except Exception:
        # Exception strings may include private paths/broker information.
        raise SystemExit('Passive service stopped: configuration or local state failure; no billing calls enabled') from None
