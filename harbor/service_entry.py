#!/usr/bin/env python3
"""Trusted service entry: discard inherited credentials before running batch.

Called by the user systemd service via explicit sudo for Docker permissions.
Only the dedicated evaluation key crosses into the batch supervisor.
"""
import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent.parent
key = os.environ.get('OB_EVAL_MODEL_KEY', '').strip()
if not key:
    raise SystemExit('Dedicated evaluation key missing; no task started')
os.environ.clear()
os.environ.update(PATH='/usr/local/bin:/usr/bin:/bin', HOME=str(ROOT / 'workdir'),
                  OB_EVAL_MODEL_KEY=key, PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
os.chdir(ROOT)
sys.path.insert(0, str(ROOT / 'harbor'))
sys.argv = [str(ROOT / 'harbor/batch.py'), *sys.argv[1:]]
runpy.run_path(str(ROOT / 'harbor/batch.py'), run_name='__main__')
