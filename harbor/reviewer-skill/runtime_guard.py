#!/usr/bin/env python3
"""Trial-local checkpoint/metrics and cooperative cumulative command budgets.

Not a security boundary: direct shell/pip calls bypass budget(). No network or
credential reads. Never upgrades a check to verified or overwrites an agent report.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time

LIMITS = {'install': 120, 'tests': 120, 'backend': 120}
CHECKS = ['runtime', 'minimax', 'context', 'database', 'state', 'code_change']


def checkpoint(manifest: Path, output: Path):
    m = json.loads(manifest.read_text())
    output.mkdir(parents=True, exist_ok=True)
    (output / 'evidence').mkdir(exist_ok=True)
    report = dict(schema_version=1, task_id=m['task_id'], commit_id=m['commit_id'],
                  scope='minimax', recommendation='review',
                  summary='Infrastructure checkpoint only; substantive review not completed.',
                  environment_adjustments=[], checks={k: dict(status='not_run', basis='not_run',
                  detail='Awaiting reviewer evidence; checkpoint is not a verdict.', evidence=[]) for k in CHECKS})
    try:
        with (output / 'report.json').open('x') as f:
            json.dump(report, f, indent=2)
    except FileExistsError:
        pass


def sample(cgroup: Path):
    out = {'time': time.time()}
    for name in ['memory.current', 'memory.peak', 'memory.max', 'memory.swap.current',
                 'memory.events', 'cpu.stat', 'pids.current']:
        try:
            raw = (cgroup / name).read_text().strip()
            out[name] = int(raw) if raw.isdigit() else raw
        except OSError:
            out[name] = None
    return out


def monitor(output: Path, duration: int):
    end = time.monotonic() + duration
    while time.monotonic() < end:
        with (output / 'evidence/runtime-resources.jsonl').open('a') as f:
            f.write(json.dumps(sample(Path('/sys/fs/cgroup'))) + '\n')
        time.sleep(5)


def budget(phase: str, command: list[str], state: Path):
    """Serialize commands across phases and debit elapsed wall time even on failure."""
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.open('a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        used = json.loads(f.read() or '{}')
        remaining = LIMITS[phase] - used.get(phase, 0)
        if remaining <= 0:
            print(f'{phase} phase budget exhausted; report blocked and stop retrying')
            return 124
        start = time.monotonic()
        proc = subprocess.Popen(command, start_new_session=True)
        try:
            try:
                code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                code = 124
        finally:
            used[phase] = used.get(phase, 0) + time.monotonic() - start
            f.seek(0); f.truncate(); json.dump(used, f); f.flush()
        return code


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='mode', required=True)
    start = sub.add_parser('start')
    start.add_argument('--duration', type=int, default=1800)
    b = sub.add_parser('budget')
    b.add_argument('phase', choices=list(LIMITS))
    b.add_argument('command', nargs=argparse.REMAINDER)
    mon = sub.add_parser('monitor')
    mon.add_argument('--duration', type=int, default=1800)
    args = p.parse_args()
    output = Path('/output')
    if args.mode == 'start':
        checkpoint(Path('/input/manifest.json'), output)
        subprocess.Popen(['python3', str(Path(__file__).resolve()), 'monitor',
                          '--duration', str(args.duration)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return 0
    if args.mode == 'monitor':
        monitor(output, args.duration)
        return 0
    cmd = args.command
    if cmd and cmd[0] == '--': cmd = cmd[1:]
    if not cmd: p.error('command required')
    return budget(args.phase, cmd, Path('/tmp/review-phase-budget.json'))


if __name__ == '__main__':
    raise SystemExit(main())
