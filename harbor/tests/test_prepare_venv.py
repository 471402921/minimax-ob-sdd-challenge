"""Offline tests for the idempotent venv helper. No pip, no network, no Docker."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prepare_venv', ROOT / 'reviewer-skill/prepare_venv.py')
pv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pv)


class Completed:
    def __init__(self, returncode=0):
        self.returncode = returncode


class PrepareVenvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.venv = self.root / 'venv'
        (self.venv / 'bin').mkdir(parents=True)
        (self.venv / 'bin' / 'python').write_text('#!/bin/sh\n')
        self.stamp = self.root / 'stamp.json'
        self.req = self.root / 'requirements.txt'
        self.req.write_text('powercontext[cli,server]==1.4.0\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_reuse_when_stamp_matches(self):
        pv.write_stamp(self.stamp, self.req, self.venv, pv.digest(self.req))
        with patch.object(pv.subprocess, 'run') as run:
            result = pv.prepare(self.req, self.venv, self.stamp)
        self.assertEqual(result['status'], 'reuse')
        run.assert_not_called()

    def test_install_once_then_reuse_then_reinstall_on_change(self):
        with patch.object(pv.subprocess, 'run', return_value=Completed(0)):
            first = pv.prepare(self.req, self.venv, self.stamp)
            self.assertEqual(first['status'], 'installed')
            self.assertEqual(first['venv'], str(self.venv))
            code = pv.main(['--requirements', str(self.req), '--venv', str(self.venv),
                            '--stamp', str(self.stamp)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(self.stamp.read_text())['sha256'], pv.digest(self.req))
            self.req.write_text('powercontext[cli,server]==1.4.0\npytest>=8,<10\n')
            changed = pv.prepare(self.req, self.venv, self.stamp)
            self.assertEqual(changed['status'], 'installed')
            self.assertNotEqual(changed['sha256'], first['sha256'])

    def test_failure_is_reported_and_not_stamped(self):
        with patch.object(pv.subprocess, 'run', return_value=Completed(1)):
            result = pv.prepare(self.req, self.venv, self.stamp)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(self.stamp.exists())

    def test_guard_wrapping_only_when_present(self):
        with patch.object(pv.Path, 'is_file', return_value=False):
            self.assertEqual(pv.guarded(['pip']), ['pip'])
        with patch.object(pv.Path, 'is_file', return_value=True):
            wrapped = pv.guarded(['pip'])
        self.assertIn('runtime_guard.py', wrapped[1])
        self.assertIn('budget', wrapped)
        self.assertIn('--', wrapped)

    def test_rejects_missing_inputs_and_requirements(self):
        with self.assertRaises(ValueError):
            pv.prepare(None, self.venv, self.stamp)
        with self.assertRaises(ValueError):
            pv.prepare(self.root / 'absent.txt', self.venv, self.stamp)

    def test_offline_flag_is_surfaced(self):
        with patch.dict(pv.os.environ, {'PIP_NO_INDEX': '1'}), \
             patch.object(pv.subprocess, 'run', return_value=Completed(0)):
            result = pv.prepare(self.req, self.venv, self.stamp)
        self.assertTrue(result['offline'])


if __name__ == '__main__':
    unittest.main()
