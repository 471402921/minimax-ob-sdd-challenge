"""No model/Docker. Cooperative phase budget and honest fallback checks."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('runtime_guard', Path(__file__).resolve().parents[1]/'reviewer-skill/runtime_guard.py')
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class GuardTests(unittest.TestCase):
    def test_checkpoint_never_claims_verified_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); m=root/'manifest.json';out=root/'output'
            m.write_text(json.dumps(dict(task_id=8,commit_id='a'*40)))
            guard.checkpoint(m,out)
            d=json.loads((out/'report.json').read_text())
            self.assertEqual(d['recommendation'],'review')
            self.assertTrue(all(c['status']=='not_run' for c in d['checks'].values()))
            (out/'report.json').write_text('preserved')
            guard.checkpoint(m,out)
            self.assertEqual((out/'report.json').read_text(),'preserved')

    def test_budget_exhaustion_prevents_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'budget';p.write_text('{"install":120}')
            with patch.object(guard.subprocess,'Popen') as start:
                self.assertEqual(guard.budget('install',['echo','no'],p),124)
                start.assert_not_called()

    def test_failed_attempt_debits_same_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'budget'
            self.assertEqual(guard.budget('tests',[sys.executable,'-c','import sys;sys.exit(3)'],p),3)
            first=json.loads(p.read_text())['tests']
            self.assertGreater(first,0)
            self.assertEqual(guard.budget('tests',[sys.executable,'-c','pass'],p),0)
            self.assertGreater(json.loads(p.read_text())['tests'],first)

    def test_timeout_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(guard.LIMITS,{'install':0.05}):
            p=Path(tmp)/'budget'
            self.assertEqual(guard.budget('install',[sys.executable,'-c','import time;time.sleep(5)'],p),124)
            self.assertGreaterEqual(json.loads(p.read_text())['install'],0.05)

    def test_monitor_duration_is_required(self):
        # A default duration let a foreground `monitor | head` block a trial.
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            guard.main(['monitor'])
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_metrics_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'memory.peak').write_text('12345')
            d=guard.sample(p)
            self.assertEqual(d['memory.peak'],12345)
            self.assertIsNone(d['memory.events'])


if __name__=='__main__':unittest.main()
