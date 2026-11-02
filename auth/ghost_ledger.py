"""Owner-specific sign-in and explicitly gated one-request Fleet probe."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys

from callback_server import CallbackServer
from oauth_flow import OAuthAttempt
from runtime import BudgetExceeded, history_once, save_tokens, tesla_transport
from token_exchange import exchange_code


def _configuration(path):
    env=dict(os.environ)
    if path is not None:
        path=Path(path).expanduser()
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd) as source:
            info=os.fstat(source.fileno())
            if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
                raise ValueError('credential file must be owned by you and mode 0600')
            text=source.read(16385)
            if len(text)>16384:
                raise ValueError('credential file too large')
        for line in text.splitlines():
            line=line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                raise ValueError('invalid credential file')
            key,value=line.split('=',1)
            if key not in ('TESLA_CLIENT_ID','TESLA_CLIENT_SECRET'):
                raise ValueError('unexpected credential setting')
            env[key]=value.strip().strip('\"\'')
    return env


def login(state_dir,client_id,client_secret,*,transport=tesla_transport):
    if not client_secret or any(ord(x)<32 for x in client_secret):
        raise ValueError('TESLA_CLIENT_SECRET is required in local configuration')
    attempt=OAuthAttempt(client_id)
    # Listener must bind successfully before we ask the user to sign in.
    with CallbackServer(attempt) as callback:
        print('Listening only on 127.0.0.1:8769 for five minutes.',flush=True)
        print('Open this link in a browser on this machine, or use the documented SSH loopback tunnel:',flush=True)
        print(attempt.authorize_url(),flush=True)
        form=callback.wait()
    tokens=exchange_code(form,client_secret,transport=transport)
    save_tokens(state_dir,tokens)
    print('Ghost Ledger authorization saved privately. No billing request sent.',flush=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Ghost Ledger: private Fleet sign-in and a single charging-history test.')
    parser.add_argument('--state-dir',type=Path,default=Path.home()/'.local/share/ghost-ledger')
    parser.add_argument('--env-file',type=Path,help='Optional mode-0600 local file; never put secrets on command line.')
    parser.add_argument('command',choices=['login','history-once'])
    args=parser.parse_args(argv)
    try:
        if args.command=='login':
            env=_configuration(args.env_file)
            client_id=env.get('TESLA_CLIENT_ID','')
            if not client_id:
                raise ValueError('TESLA_CLIENT_ID is required in local configuration')
            login(args.state_dir,client_id,env.get('TESLA_CLIENT_SECRET',''))
        else:
            print(json.dumps(history_once(args.state_dir),sort_keys=True))
        return 0
    except BudgetExceeded:
        print('Stopped: the one-request test allowance is already reserved. No additional call sent.',file=sys.stderr)
        return 3
    except TimeoutError:
        print('Sign-in timed out. No automatic retry and no billing request sent.',file=sys.stderr)
        return 2
    except (ValueError,OSError) as error:
        # All messages originating in our helpers are sanitized; never print network exception bodies.
        message=str(error) if isinstance(error,ValueError) else 'local/network operation failed; check configuration'
        print('Stopped: '+message,file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Cancelled.',file=sys.stderr)
        return 130

if __name__=='__main__':
    sys.exit(main())
