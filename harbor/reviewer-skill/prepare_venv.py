#!/usr/bin/env python3
"""Create ONE reusable virtualenv for a submission and never reinstall per copy.

Measured waste: reviewers re-ran `pip install` in 6-8 scratch trees, ~20 s each,
and one submission burned the whole 240 s install budget on a network retry that
could never succeed. This helper makes the install idempotent:

* the venv lives outside the submission copy (default /work/review/venv), so a
  cached interpreter path stays valid for every scratch tree;
* a stamp records the requirements hash; a repeat call prints `reuse` and does
  no work;
* every install runs through the cooperative phase budget.

It never reads credentials and never installs from a network source when the
image sets PIP_NO_INDEX (the reviewer still records this as an adjustment).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

DEFAULT_VENV = Path('/work/review/venv')
DEFAULT_STAMP = Path('/work/review/venv-stamp.json')


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_stamp(stamp: Path) -> dict:
    try:
        data = json.loads(stamp.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_stamp(stamp: Path, requirements: Path, venv: Path, digest_value: str) -> None:
    stamp.write_text(json.dumps({'requirements': str(requirements), 'sha256': digest_value,
                                 'venv': str(venv)}, sort_keys=True))


def install_command(python: Path, requirements: Path | None, package: list[str]) -> list[str]:
    base = [str(python), '-m', 'pip', 'install', '--disable-pip-version-check']
    return base + (['-r', str(requirements)] if requirements else package)


def guarded(command: list[str]) -> list[str]:
    """Route through the phase budget when the guard is present in this image."""
    guard = Path('/opt/reviewer-skill/runtime_guard.py')
    if guard.is_file():
        return [sys.executable, str(guard), 'budget', 'install', '--', *command]
    return command


def prepare(requirements: Path | None = None, venv: Path = DEFAULT_VENV,
            stamp: Path = DEFAULT_STAMP, package: list[str] | None = None,
            force: bool = False) -> dict:
    """Idempotently create the venv and install the declared dependencies once."""
    package = package or []
    if not requirements and not package:
        raise ValueError('need --requirements or a package to install')
    if requirements is not None and not requirements.is_file():
        raise ValueError('requirements file not found')
    venv_python = venv / 'bin' / 'python'
    key = digest(requirements) if requirements else '|'.join(package)
    previous = read_stamp(stamp)
    if not force and venv_python.is_file() and previous.get('sha256') == key:
        return {'status': 'reuse', 'venv': str(venv), 'sha256': key}
    if not venv_python.is_file():
        venv.parent.mkdir(parents=True, exist_ok=True)
        created = subprocess.run([sys.executable, '-m', 'venv', str(venv)], timeout=180)
        if created.returncode:
            raise ValueError('virtualenv creation failed')
    code = subprocess.run(guarded(install_command(venv_python, requirements, package))).returncode
    # pip reports failure; the caller decides whether to mark the check blocked.
    return {'status': 'installed' if code == 0 else 'failed', 'exit_code': code,
            'venv': str(venv), 'sha256': key,
            'offline': os.environ.get('PIP_NO_INDEX') == '1'}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--requirements', type=Path)
    p.add_argument('--package', action='append', default=[])
    p.add_argument('--venv', type=Path, default=DEFAULT_VENV)
    p.add_argument('--stamp', type=Path, default=DEFAULT_STAMP)
    p.add_argument('--force', action='store_true', help='reinstall even when the stamp matches')
    args = p.parse_args(argv)
    try:
        result = prepare(args.requirements, args.venv, args.stamp, args.package, args.force)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'prepare-venv: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    if result['status'] == 'failed':
        return 1
    if result['status'] == 'installed':
        write_stamp(args.stamp, args.requirements or Path('packages'), args.venv, result['sha256'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
