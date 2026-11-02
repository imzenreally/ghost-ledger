#!/usr/bin/env python3
"""Manual owner CLI. Cached history by default; --fetch requests ONE latest page.

No scheduling, automatic refresh, retries, or pagination. Every explicit import
requires --apply and its own verified narrow backup before workflow writes.
"""
import argparse
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import sys
import uuid

from auth import runtime
from charging_costs.postgres import PgRepository
from ledger_store import LedgerStore
from manual_reader import ManualReaderError, fetch_latest, private_path, read_history
from owner_database import OwnerDatabase, OwnerDatabaseError
from workflow import run_import

DEFAULT_ROOT = Path.home() / '.local/share/ghost-ledger'
STATUSES = frozenset(('ready', 'imported', 'uncertain', 'protected', 'conflict', 'invalid', 'unmatched', 'pending', 'skipped'))


@contextmanager
def import_lock(state_path):
    path = private_path(Path(state_path).parent / '.manual-import.lock', create=True)
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ManualReaderError('another manual ledger operation is running; no retry') from None
        yield
    finally:
        os.close(fd)


def _safe_row(row):
    safe = {'status': row['status'] if row.get('status') in STATUSES else 'invalid'}
    if type(row.get('session_id')) is int:
        safe['session_id'] = row['session_id']
    try:
        value = row.get('amount')
        if isinstance(value, (str, int, Decimal)) and not isinstance(value, bool) and len(str(value)) <= 64:
            amount = Decimal(value)
            if amount.is_finite():
                safe['amount'] = str(amount)
    except (InvalidOperation, ValueError):
        pass
    return safe


def main(argv=None, *, database_factory=OwnerDatabase):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('preview', 'import'):
        sub = commands.add_parser(command)
        sub.add_argument('--history', type=Path, default=DEFAULT_ROOT / 'history.json')
        sub.add_argument('--state', type=Path, default=DEFAULT_ROOT / 'ledger.sqlite3')
        sub.add_argument('--fetch', action='store_true', help='Explicitly fetch ONE latest history page; consumes manual allowance.')
        if command == 'import':
            sub.add_argument('--apply', action='store_true', required=True, help='Explicit missing-cost writes after backup.')
    args = parser.parse_args(argv)
    try:
        state = private_path(args.state)
        # Validate SQLite sidecars before LedgerStore can follow them.
        for suffix in ('-journal', '-wal', '-shm'):
            private_path(Path(str(state) + suffix))
        with import_lock(state):
            history_path = private_path(args.history)
            if args.fetch:
                fetch_latest(history_path, token_root=DEFAULT_ROOT)
            history = read_history(history_path)
            store = LedgerStore(state)
            with database_factory() as database:
                repo = PgRepository(database.connect)
                applying = args.command == 'import'
                if applying:
                    backups = runtime._private_dir(state.parent / 'backups')
                    database.backup(backups / (uuid.uuid4().hex + '.dump'))
                report = run_import(history, repo, store, apply=applying)
            # Do not echo reasons, VINs, external billing IDs, tokens or evidence.
            output = {'imported': report['imported'], 'uncertain': report['uncertain'],
                      'statuses': {key: value for key, value in report['statuses'].items() if key in STATUSES},
                      'results': [_safe_row(row) for row in report['results']],
                      'sessions': [_safe_row(row) for row in store.snapshot()['rows']]}
            print(json.dumps(output, sort_keys=True))
            return 4 if report['uncertain'] else 0
    except runtime.BudgetExceeded:
        print('Stopped: manual UTC request allowance exhausted; no request sent.', file=sys.stderr)
        return 3
    except (ManualReaderError, OwnerDatabaseError) as error:
        print('Stopped: ' + str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Cancelled; inspect local audit before another import.', file=sys.stderr)
        return 130
    except Exception:
        # Database/network exception strings can contain secrets: never echo them.
        print('Stopped: ledger operation failed; check private paths, database access, and Ghost Ledger sign-in. No automatic retry.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
