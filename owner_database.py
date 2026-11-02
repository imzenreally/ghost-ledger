"""Short-lived owner-only SSH tunnel and narrow pre-apply backup.

Credentials are captured into memory from Docker, never CLI args, files, or logs.
The context owns the SSH child and always terminates it; connections returned by
connect() are NEW and must be closed by their caller (PgRepository does this).
"""
import ipaddress
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import time

from manual_reader import private_path


class OwnerDatabaseError(ValueError):
    """Sanitized owner database failure, safe to display."""


def _ready(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.2):
            return True
    except OSError:
        return False


class OwnerDatabase:
    def __init__(self, *, run=subprocess.run, popen=subprocess.Popen,
                 connector=None, ready=_ready, startup_timeout=8):
        self._run = run
        self._popen = popen
        self._connector = connector
        self._ready = ready
        self._startup_timeout = startup_timeout
        self._process = None
        self._password = None
        self._active = False
        self.port = None

    def _ssh_options(self):
        # Do not inherit alias settings that fork/persist a control-master daemon.
        return ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
                '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                '-o', 'ControlPersist=no', '-o', 'ForkAfterAuthentication=no']

    def _ssh(self, command):
        return ['ssh', *self._ssh_options(), 'dockerhost', command]

    def _capture(self, command):
        try:
            result = self._run(self._ssh(command), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=False)
            if result.returncode != 0 or not isinstance(result.stdout, bytes) or len(result.stdout) > 1024 * 1024:
                raise OwnerDatabaseError('owner database discovery failed')
            return result.stdout
        except Exception:
            raise OwnerDatabaseError('owner database discovery failed; check SSH/docker access') from None

    def __enter__(self):
        if self._active or self._process is not None:
            raise OwnerDatabaseError('database context already active')
        try:
            raw = self._capture('docker inspect teslamate-database-1')
            records = json.loads(raw)
            networks = records[0]['NetworkSettings']['Networks']
            ips = {str(ipaddress.IPv4Address(n['IPAddress'])) for n in networks.values() if n.get('IPAddress')}
            if len(ips) != 1:
                raise ValueError('ambiguous container address')
            address = ips.pop()
            password = self._capture('docker exec teslamate-database-1 printenv POSTGRES_PASSWORD').decode().rstrip('\r\n')
            if not password or len(password) > 16384 or '\n' in password or '\x00' in password:
                raise ValueError('invalid password output')
            self._password = password
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(('127.0.0.1', 0))
                self.port = sock.getsockname()[1]
            args = ['ssh', '-N', *self._ssh_options(),
                    '-o', 'ExitOnForwardFailure=yes', '-L',
                    f'127.0.0.1:{self.port}:{address}:5432', 'dockerhost']
            self._process = self._popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + self._startup_timeout
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise OwnerDatabaseError('SSH tunnel exited before readiness; check forwarding/access')
                if self._ready(self.port):
                    if self._process.poll() is not None:
                        raise OwnerDatabaseError('SSH tunnel exited before readiness')
                    self._active = True
                    return self
                time.sleep(0.05)
            raise OwnerDatabaseError('SSH tunnel startup timed out')
        except BaseException as error:
            self.__exit__(None, None, None)
            if isinstance(error, (KeyboardInterrupt, SystemExit, OwnerDatabaseError)):
                raise
            raise OwnerDatabaseError('owner database setup failed; check SSH/docker configuration') from None

    def __exit__(self, *exc):
        self._active = False
        self._password = None
        process, self._process = self._process, None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            except OSError:
                pass

    def connect(self):
        if not self._active or self._process.poll() is not None:
            raise OwnerDatabaseError('owner database context is not active')
        try:
            connector = self._connector
            if connector is None:
                import psycopg
                connector = psycopg.connect
            return connector(host='127.0.0.1', port=self.port, dbname='teslamate', user='teslamate',
                             password=self._password, application_name='ghost-ledger', connect_timeout=5,
                             options='-c statement_timeout=10000 -c lock_timeout=5000')
        except Exception:
            raise OwnerDatabaseError('owner database connection failed') from None

    def backup(self, path):
        if not self._active or self._process.poll() is not None:
            raise OwnerDatabaseError('owner database context is not active')
        path = private_path(path)
        # The container shell expands its own environment; no password leaves in argv.
        dump = ('PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump --format=custom --no-password '
                '--username=teslamate --dbname=teslamate '
                '--table=public.charging_processes --table=public.cars --table=public.geofences')
        command = 'docker exec teslamate-database-1 sh -c ' + shlex.quote(dump)
        created = False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            created = True
            with os.fdopen(fd, 'wb') as destination:
                result = self._run(self._ssh(command), stdin=subprocess.DEVNULL, stdout=destination,
                                   stderr=subprocess.DEVNULL, timeout=60, check=False)
                destination.flush()
                os.fsync(destination.fileno())
            with path.open('rb') as source:
                if result.returncode != 0 or source.read(5) != b'PGDMP' or path.stat().st_size <= 5:
                    raise OwnerDatabaseError('pre-apply backup failed validation; no writes authorized')
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return path
        except BaseException as error:
            if created:
                path.unlink(missing_ok=True)
            if isinstance(error, (KeyboardInterrupt, SystemExit, OwnerDatabaseError)):
                raise
            raise OwnerDatabaseError('pre-apply backup failed; no writes authorized') from None
