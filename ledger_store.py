"""Local, durable preview state. No Tesla or TeslaMate connections."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


class LedgerStore:
    MAX_ROWS = 10000

    def __init__(self, path):
        self.path = Path(path).absolute()
        self._check_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS previews (
                    external_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT NOT NULL,
                    external_id TEXT NOT NULL, session_id INTEGER, amount TEXT,
                    outcome TEXT NOT NULL, detail TEXT NOT NULL);
            ''')

    def _check_path(self):
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError('State path must not contain symlinks')
        if self.path.exists() and not self.path.is_file():
            raise ValueError('State path must be a regular file')
        for suffix in ('-journal', '-wal', '-shm'):
            if Path(str(self.path) + suffix).is_symlink():
                raise ValueError('State sidecar must not be a symlink')

    @contextmanager
    def _db(self):
        self._check_path()
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def publish(self, rows, source='saved history'):
        batch = []
        for row in rows:
            if len(batch) >= self.MAX_ROWS:
                raise ValueError('Too many preview rows')
            row = dict(row)
            if not isinstance(row.get('external_id'), str) or not row['external_id'] or len(row['external_id']) > 256:
                raise ValueError('A bounded external_id is required')
            encoded = json.dumps(row, allow_nan=False)
            if len(encoded) > 65536:
                raise ValueError('Preview row too large')
            batch.append(row)
        with self._db() as db:
            for row in batch:
                previous = db.execute('SELECT payload FROM previews WHERE external_id=?', (row['external_id'],)).fetchone()
                imported = db.execute("SELECT 1 FROM audit WHERE external_id=? AND outcome='imported' LIMIT 1", (row['external_id'],)).fetchone()
                if not row.get('review_required') and (imported or (previous and json.loads(previous[0]).get('status') == 'imported')):
                    row['status'] = 'imported'
                db.execute('INSERT INTO previews VALUES (?, ?) ON CONFLICT(external_id) DO UPDATE SET payload=excluded.payload',
                           (row['external_id'], json.dumps(row, allow_nan=False)))
            if db.execute('SELECT count(*) FROM previews').fetchone()[0] > self.MAX_ROWS:
                raise ValueError('Preview row capacity exceeded')
            for key, value in {'source': source, 'updated_at': datetime.now(timezone.utc).isoformat()}.items():
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?, ?)', (key, value))

    def record_event(self, external_id, session_id, amount, outcome, detail=''):
        """Append an explicit operation result; never invokes external writes."""
        with self._db() as db:
            db.execute('INSERT INTO audit(recorded_at,external_id,session_id,amount,outcome,detail) VALUES (?,?,?,?,?,?)',
                       (datetime.now(timezone.utc).isoformat(), external_id, session_id,
                        None if amount is None else str(amount), outcome, detail))
            if outcome == 'imported':
                found = db.execute('SELECT payload FROM previews WHERE external_id=?', (external_id,)).fetchone()
                if found:
                    row = json.loads(found[0])
                    row['status'] = 'imported'
                    db.execute('UPDATE previews SET payload=? WHERE external_id=?', (json.dumps(row), external_id))

    def events(self):
        """Local CLI audit access only. Deliberately not exposed by HTTP."""
        with self._db() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute('SELECT * FROM audit ORDER BY id')]

    def snapshot(self):
        with self._db() as db:
            meta = dict(db.execute('SELECT key, value FROM metadata'))
            rows = [json.loads(row[0]) for row in db.execute('SELECT payload FROM previews ORDER BY external_id LIMIT ?', (self.MAX_ROWS,))]
        return {'rows': rows, 'updated_at': meta.get('updated_at'),
                'source': meta.get('source', 'saved history'), 'warnings': []}
