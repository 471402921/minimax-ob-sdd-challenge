#!/usr/bin/env python3
"""Trial-local checkpoint/metrics and cooperative cumulative command budgets.

Not a security boundary: direct shell/pip calls bypass budget(). No network or
credential reads. Never upgrades a check to verified or overwrites an agent report.

Budgets are cumulative per phase (retries debit the same allowance) and are
configurable at `start` time through a small JSON file, because a single fixed
install budget proved both too tight (slow mirrors) and too loose (endless
retries). Defaults target a warm, cached review: install 240 s, tests 120 s,
backend 120 s.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time

LIMITS = {'install': 240, 'tests': 120, 'backend': 120}
STATE = Path('/tmp/review-phase-budget.json')
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


def monitor(duration: int):
    # Metrics must NOT live under /output/evidence: the file grows while the
    # reviewer hashes it, so a live metrics file there breaks the evidence-hash
    # verifier even when the report is honest.
    metrics = Path('/tmp/review-runtime-resources.jsonl')
    end = time.monotonic() + duration
    while time.monotonic() < end:
        record = sample(Path('/sys/fs/cgroup'))
        with metrics.open('a') as f:
            f.write(json.dumps(record) + '\n')
        # Emit one line per sample: a caller that pipes us into `head` must see
        # output (and get SIGPIPE) instead of blocking for the whole duration.
        try:
            print(json.dumps({'sample': record['time'],
                              'memory_current': record.get('memory.current')}), flush=True)
        except BrokenPipeError:
            return
        time.sleep(5)


def write_limits(state: Path, limits: dict[str, int]) -> dict[str, int]:
    """Persist phase limits next to the used-seconds counters."""
    if set(limits) != set(LIMITS):
        raise ValueError('limits must name every phase exactly once')
    for phase, value in limits.items():
        if not isinstance(value, int) or not 30 <= value <= 1800:
            raise ValueError(f'{phase} budget must be an integer 30..1800 seconds')
    data = {}
    if state.exists():
        data = json.loads(state.read_text() or '{}')
    data['limits'] = limits
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(data))
    return limits


def read_limits(state: Path) -> dict[str, int]:
    try:
        return {**LIMITS, **(json.loads(state.read_text()).get('limits') or {})}
    except (OSError, ValueError):
        return dict(LIMITS)


def budget(phase: str, command: list[str], state: Path, limits: dict[str, int] | None = None):
    """Serialize commands across phases and debit elapsed wall time even on failure."""
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.open('a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        data = json.loads(f.read() or '{}')
        allowance = (limits or {**LIMITS, **(data.get('limits') or {})})[phase]
        used = data.get('used') or {}
        # Older files stored the counters at the top level; accept both shapes.
        remaining = allowance - (used.get(phase) or data.get(phase) or 0)
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
            for phase_name in LIMITS:
                data.pop(phase_name, None)
            data['used'] = used
            f.seek(0); f.truncate(); json.dump(data, f, sort_keys=True); f.flush()
        return code


def main(argv: list[str] | None = None) -> int:  # noqa: D401 - CLI entry
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='mode', required=True)
    start = sub.add_parser('start')
    start.add_argument('--duration', type=int, default=1800)
    for phase, default in LIMITS.items():
        start.add_argument(f'--{phase}-seconds', type=int, default=default,
                           help=f'cumulative {phase} budget (default {default}s)')
    limits = sub.add_parser('limits')
    limits.add_argument('--state', type=Path, default=STATE)
    b = sub.add_parser('budget')
    b.add_argument('phase', choices=list(LIMITS))
    b.add_argument('--state', type=Path, default=STATE)
    b.add_argument('command', nargs=argparse.REMAINDER)
    mon = sub.add_parser('monitor')
    # No default on purpose: a foreground `monitor` with an implicit 30-minute
    # duration once blocked a whole trial behind `| head`. Sampling is started
    # automatically by `start`; manual runs must state their duration.
    mon.add_argument('--duration', type=int, required=True)
    args = p.parse_args(argv)
    output = Path('/output')
    if args.mode == 'start':
        checkpoint(Path('/input/manifest.json'), output)
        chosen = {phase: getattr(args, f'{phase}_seconds') for phase in LIMITS}
        try:
            write_limits(STATE, chosen)
        except ValueError as exc:
            print(f'runtime guard: {exc}', file=__import__('sys').stderr)
            return 2
        subprocess.Popen(['python3', str(Path(__file__).resolve()), 'monitor',
                          '--duration', str(args.duration)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return 0
    if args.mode == 'monitor':
        monitor(args.duration)
        return 0
    if args.mode == 'limits':
        print(json.dumps(read_limits(args.state), sort_keys=True))
        return 0
    cmd = args.command
    if cmd and cmd[0] == '--': cmd = cmd[1:]
    if not cmd: p.error('command required')
    return budget(args.phase, cmd, args.state)


if __name__ == '__main__':
    raise SystemExit(main())
