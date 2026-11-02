"""Native private LAN dashboard launcher; credentials never appear in argv."""
import ipaddress
import os
from pathlib import Path
import sys
from auth.runtime import _read_private, _private_dir


def server_settings(path):
    path = Path(path).expanduser().absolute()
    _private_dir(path.parent)
    config = _read_private(path)
    address = ipaddress.ip_address(config['bind_host'])
    if address.version != 4 or address.is_unspecified or not (address.is_private or address.is_loopback):
        raise ValueError('dashboard must bind a specific private IPv4 address')
    env = os.environ.copy()
    env.update(GHOST_LEDGER_PASSWORD_HASH=config['password_hash'],
               GHOST_LEDGER_SECRET_KEY=config['secret_key'],
               GHOST_LEDGER_STATE=config['state'],
               GHOST_LEDGER_MANUAL_COSTS='true' if config.get('manual_cost_entry') else 'false',
               GHOST_LEDGER_ALLOWED_HOSTS=','.join((str(address)+':8897','localhost:8897','127.0.0.1:8897')))
    args = [sys.executable, '-m', 'gunicorn', '--bind', str(address)+':8897',
            '--workers', '1', '--threads', '4', '--umask', '0077',
            '--worker-tmp-dir', str(path.parent), 'dashboard:app']
    return args, env


if __name__ == '__main__':
    config_path = os.environ.get('GHOST_LEDGER_CONFIG', str(Path.home()/'.local/share/ghost-ledger/dashboard-config.json'))
    args, env = server_settings(config_path)
    os.chdir(Path(__file__).resolve().parent)
    os.execve(sys.executable, args, env)
