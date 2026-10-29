#!/usr/bin/env python3
"""Trusted service entry: load the dedicated key from a private file, then run batch.

The key is read from a 0600 file whose PATH (never the value) is passed in argv,
so `sudo --preserve-env` is not used and the secret cannot be echoed into the
systemd journal or `ps` output. Everything else inherited is discarded.
"""
import os
from pathlib import Path
import re
import runpy
import shlex
import sys

ROOT = Path(__file__).resolve().parent.parent
KEY_NAME = 'OB_EVAL_MODEL_KEY'
FLAG = '--key-file'


def read_key(path: Path) -> str:
    """Return OB_EVAL_MODEL_KEY from a shell-style env file without executing it."""
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        raise SystemExit(f'{KEY_NAME} file is not readable; no task started') from None
    value = ''
    pattern = re.compile(r'^\s*(?:export\s+)?' + KEY_NAME + r'=(.*)$')
    for line in text.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        raw = match.group(1).strip()
        try:
            parts = shlex.split(raw)
        except ValueError:
            raise SystemExit(f'{KEY_NAME} file is not parseable; no task started') from None
        value = (parts[0] if parts else '').strip()
    if not value:
        raise SystemExit(f'{KEY_NAME} missing from the key file; no task started')
    return value


def main(argv: list[str]) -> int:
    args = list(argv)
    key_file = os.environ.get('OB_HARBOR_KEY_FILE', '')
    if FLAG in args:
        index = args.index(FLAG)
        if index + 1 >= len(args):
            raise SystemExit(f'{FLAG} needs a path')
        key_file = args[index + 1]
        del args[index:index + 2]
    if not key_file:
        raise SystemExit(f'{FLAG} is required; no task started')
    key = read_key(Path(key_file))
    trace = os.environ.get('HARBOR_BATCH_TRACE') == '1'
    os.environ.clear()
    os.environ.update(PATH='/usr/local/bin:/usr/bin:/bin', HOME=str(ROOT / 'workdir'),
                      PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    os.environ[KEY_NAME] = key
    if trace:
        os.environ['HARBOR_BATCH_TRACE'] = '1'
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT / 'harbor'))
    sys.argv = [str(ROOT / 'harbor/batch.py'), *args]
    runpy.run_path(str(ROOT / 'harbor/batch.py'), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
