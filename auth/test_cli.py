import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT=Path(__file__).with_name('ghost_ledger.py')

class CLITests(unittest.TestCase):
    def test_help_runs_without_credentials(self):
        result=subprocess.run([sys.executable,str(SCRIPT),'--help'],capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('history-once',result.stdout)
        self.assertIn('login',result.stdout)

    def test_missing_login_credentials_stops_before_listening(self):
        env={k:v for k,v in os.environ.items() if k not in ('TESLA_CLIENT_ID','TESLA_CLIENT_SECRET')}
        result=subprocess.run([sys.executable,str(SCRIPT),'login'],env=env,capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertIn('TESLA_CLIENT_ID',result.stderr)
        self.assertNotIn('Traceback',result.stderr)

    def test_history_without_signin_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp).resolve()/'private'
            result=subprocess.run([sys.executable,str(SCRIPT),'--state-dir',str(state),'history-once'],capture_output=True,text=True,timeout=3)
            self.assertEqual(result.returncode,2,result.stderr)
            self.assertIn('sign-in required',result.stderr)
            self.assertFalse((state/'probe-budget.sqlite3').exists())
