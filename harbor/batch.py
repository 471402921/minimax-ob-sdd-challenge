#!/usr/bin/env python3
"""Bounded supervisor for ALREADY STAGED Harbor tasks. Never claims or callbacks.

A manifest is {"tasks": [{"path": "workdir/harbor/review-...", "profile":
"light"|"backend"}]}. backend reserves a scarce service slot for the entire trial.
No shell commands, credentials or arbitrary Harbor arguments accepted in manifest.
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any

from run_pilot import ROOT, environment, command
from resources import AdmissionPolicy, capture

MAX_TRIALS = 4
MAX_BACKENDS = 2
PRIVATE = ROOT / 'workdir/harbor'
BATCHES = ROOT / 'runs/harbor-batches'


@dataclass(frozen=True)
class Task:
    path: Path
    task_id: int
    commit: str
    profile: str
    synthetic: bool
    digest: str


def atomic(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        os.chmod(tmp, 0o600)
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def tree_digest(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(path.rglob('*')):
        if p.is_symlink():
            raise ValueError('staged tasks must not contain symlinks')
        if p.is_file():
            h.update(str(p.relative_to(path)).encode() + b'\0')
            h.update(p.read_bytes())
    return h.hexdigest()


def load_tasks(manifest: Path) -> list[Task]:
    data = json.loads(manifest.read_text())
    if set(data) != {'tasks'} or not isinstance(data['tasks'], list) or not data['tasks']:
        raise ValueError('manifest must contain a nonempty tasks list')
    result: list[Task] = []
    seen: set[int] = set()
    for row in data['tasks']:
        if not isinstance(row, dict) or set(row) - {'path', 'profile'} or 'path' not in row:
            raise ValueError('unknown task fields')
        path = Path(row['path'])
        path = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
        if not path.is_relative_to(PRIVATE.resolve()) or path == PRIVATE.resolve():
            raise ValueError('task must be inside workdir/harbor')
        if not (path / 'task.toml').is_file():
            raise ValueError('task.toml missing')
        ident = json.loads((path / 'environment/input/manifest.json').read_text())
        tid, commit = ident['task_id'], ident['commit_id']
        if type(tid) is not int or tid <= 0 or tid in seen:
            raise ValueError('invalid or duplicate task identity')
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
            raise ValueError('full lowercase commit required')
        if ident.get('scope') != 'minimax' or ident.get('auto_callback') is not False:
            raise ValueError('unsafe manifest scope/callback')
        profile = row.get('profile', 'backend')  # unknown requirements reserve heavy slot
        if profile not in ('light', 'backend'):
            raise ValueError('profile must be light or backend')
        if ident.get('runtime_profile') == 'backend' and profile != 'backend':
            raise ValueError('backend task cannot be scheduled in a light slot')
        synthetic = ident.get('synthetic') is True
        if not synthetic and commit == '0' * len(commit):
            raise ValueError('invalid real commit')
        seen.add(tid)
        result.append(Task(path, tid, commit, profile, synthetic, tree_digest(path)))
    return result


def check_lease(task: Task, required_seconds: float, *, now: float | None = None) -> None:
    if task.synthetic:
        return
    now = time.time() if now is None else now
    pending = ROOT / f'workdir/task-{task.task_id}.pending.json'
    if (ROOT / f'workdir/task-{task.task_id}.submitted.json').exists():
        raise ValueError('task already submitted')
    p = json.loads(pending.read_text())
    if (p.get('task_id') != task.task_id or p.get('stage') != 'awaiting_review'
            or p.get('commit_id') != task.commit):
        raise ValueError('pending identity/stage/version mismatch')
    deadline = min(float(p['deadline_epoch']), float(p['platform_deadline_epoch']))
    if deadline - now < required_seconds:
        raise ValueError('insufficient lease for execution plus review/callback reserve')


def result_status(job: Path, exit_code: int) -> dict:
    """Never translate a report reward into a contestant verdict."""
    try:
        d = json.loads((job / 'result.json').read_text())
        s = d['stats']
        complete = (bool(d.get('finished_at')) and d.get('n_total_trials') == 1
                    and s.get('n_completed_trials') == 1 and s.get('n_errored_trials') == 0
                    and s.get('n_running_trials') == 0)
        return {'status': 'finished' if exit_code == 0 and complete else 'needs_attention',
                'exit_code': exit_code, 'harbor_finished_at': d.get('finished_at'),
                'completed_trials': s.get('n_completed_trials'),
                'errored_trials': s.get('n_errored_trials'),
                'result_path': str(job / 'result.json')}
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'needs_attention', 'exit_code': exit_code, 'result_missing_or_invalid': True}


def argv(task: Task, harbor: Path, job_name: str, *, oracle: bool) -> list[str]:
    if oracle:
        if not task.synthetic:
            raise ValueError('oracle permitted only for synthetic fixtures')
        return [str(harbor), 'run', '--path', str(task.path), '--agent', 'oracle',
                '--env', 'docker', '--n-concurrent', '1', '--max-retries', '0',
                '--jobs-dir', str(ROOT / 'runs/harbor'), '--job-name', job_name]
    # Reuse model URL/credential checks from pilot; use pinned installed Harbor,
    # not uvx / per-run package downloads.
    tail = command(task.path, 'anthropic/MiniMax-M3.1-Flash',
                   'https://api.minimax.cn/anthropic', 'anthropic_messages', 'uvx')[5:]
    return [str(harbor), 'run', *tail, '--n-concurrent', '1', '--max-retries', '0',
            '--jobs-dir', str(ROOT / 'runs/harbor'), '--job-name', job_name]


def acquire(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    f = os.fdopen(fd, 'r+')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        f.close()
        raise
    return f


def signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def run(args, tasks: list[Task]) -> int:
    if not 1 <= args.trials <= MAX_TRIALS or not 1 <= args.backends <= MAX_BACKENDS:
        raise ValueError('hard caps: 4 trials, 2 backend trials')
    if not 0 <= args.wait_seconds <= 300:
        raise ValueError('wait budget must be 0..300 seconds; never preclaim a long queue')
    if not 60 <= args.timeout <= 2700:
        raise ValueError('wall timeout must be 60..2700 seconds')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,60}', args.batch_id):
        raise ValueError('unsafe batch id')
    for task in tasks:
        check_lease(task, args.timeout + 900)
        if args.oracle and not task.synthetic:
            raise ValueError('oracle is synthetic-only')
    policy = AdmissionPolicy()
    snapshot = capture(ROOT)
    reasons = policy.evaluate(snapshot)
    plan = {'batch_id': args.batch_id, 'trials': args.trials, 'backend_slots': args.backends,
            'resource_snapshot': asdict(snapshot), 'admission_blocks': reasons,
            'auto_claim': False, 'auto_callback': False,
            'tasks': [{'task_id': t.task_id, 'commit': t.commit, 'profile': t.profile,
                       'digest': t.digest} for t in tasks]}
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if not reasons else 2
    if reasons:
        raise ValueError('resource admission blocked: ' + '; '.join(reasons))
    harbor = args.harbor.resolve()
    if not harbor.is_file() or not os.access(harbor, os.X_OK):
        raise ValueError('installed Harbor executable missing')
    version = subprocess.run([str(harbor), '--version'], capture_output=True, text=True,
                             timeout=15, env={'PATH': '/usr/local/bin:/usr/bin:/bin'})
    if version.returncode or version.stdout.strip() != '0.25.0':
        raise ValueError('Harbor 0.25.0 required')
    # Content-named local tags are required by BuildKit. Validate immediately
    # before starting; never silently pull a mutable substitute.
    for task in tasks:
        runtime = task.path / 'environment/input/runtime-profile.json'
        if runtime.exists():
            image_id = json.loads(runtime.read_text())['base_image']
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
                raise ValueError('invalid pinned image ID')
            tag = 'harbor/prewarm:sha-' + image_id.split(':', 1)[1]
            found = subprocess.run(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'],
                                   capture_output=True, text=True, timeout=15)
            if found.returncode or found.stdout.strip() != image_id:
                raise ValueError('prewarmed image tag is absent or changed')
    # One supervisor per repo across sessions, plus locks retained per active task.
    with acquire(ROOT / 'workdir/harbor-batch.lock'):
        out = BATCHES / args.batch_id
        out.mkdir(parents=True, mode=0o700, exist_ok=False)
        atomic(out / 'plan.json', plan)
        state: dict = {'status': 'running', 'tasks': {}, 'pid': os.getpid(), 'auto_callback': False}
        stop = [False]
        def stopping(_signum, _frame):
            stop[0] = True
        old = {s: signal.signal(s, stopping) for s in [signal.SIGINT, signal.SIGTERM]}
        pending = deque(tasks)
        active: list[dict] = []
        started = time.monotonic()
        last_sample = 0.0
        base_available = asdict(snapshot).get('memory_available_gib')
        try:
            while pending or active:
                now = time.monotonic()
                if now - last_sample >= 5:
                    snap = capture(ROOT)
                    reasons = policy.evaluate(snap)
                    state['resources'] = asdict(snap)
                    state['admission_blocks'] = reasons
                    last_sample = now
                    with (out / 'resources.jsonl').open('a') as f:
                        f.write(json.dumps({'at': time.time(), **asdict(snap)}) + '\n')
                for entry in active[:]:
                    proc = entry['proc']
                    code = proc.poll()
                    timed_out = now - entry['started'] >= args.timeout
                    if (stop[0] or timed_out) and code is None:
                        if entry.get('term_at') is None:
                            signal_group(proc, signal.SIGTERM)
                            entry['term_at'] = now
                            state['tasks'][str(entry['task'].task_id)]['status'] = 'stopping'
                        elif now - entry['term_at'] >= 20:
                            signal_group(proc, signal.SIGKILL)
                    if code is not None:
                        tid = str(entry['task'].task_id)
                        summary = result_status(entry['job'], code)
                        if entry.get('term_at') is not None:
                            summary.update(status='interrupted', cleanup_required=True)
                            stop[0] = True  # Do not admit more work with potentially orphaned containers.
                        state['tasks'][tid].update(summary)
                        entry['log'].close()
                        entry['lock'].close()
                        active.remove(entry)
                        # No restart/retry, no guessing success on missing output.
                if stop[0]:
                    while pending:
                        state['tasks'][str(pending.popleft().task_id)] = {'status': 'not_started_cancelled'}
                # Count reservation rather than relying only on lagging MemAvailable.
                reserved = sum(6 if x['task'].profile == 'backend' else 4 for x in active)
                baseline_budget = max(0, float(base_available or 0) - 16)  # 12 host + 4 control/verifier
                if pending and not stop[0] and not reasons and len(active) < args.trials:
                    heavy = sum(x['task'].profile == 'backend' for x in active)
                    selected = next((t for t in pending if
                        (t.profile != 'backend' or heavy < args.backends)
                        and reserved + (6 if t.profile == 'backend' else 4) <= baseline_budget
                        and asdict(snap)['memory_available_gib'] - (6 if t.profile == 'backend' else 4) >= 16), None)
                    if selected:
                        pending.remove(selected)
                        task = selected
                        tid = str(task.task_id)
                        lock = log = None
                        try:
                            check_lease(task, args.timeout + 900)
                            if tree_digest(task.path) != task.digest:
                                raise ValueError('staged task changed after planning')
                            lock = acquire(ROOT / f'workdir/harbor-locks/task-{task.task_id}.lock')
                            home = out / f'home-{task.task_id}'
                            home.mkdir(mode=0o700)
                            env = ({'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(home)}
                                   if args.oracle else environment(home, dict(os.environ)))
                            jobname = f'{args.batch_id}-{task.task_id}'
                            job = ROOT / 'runs/harbor' / jobname
                            if job.exists():
                                raise ValueError('job already exists; refusing overwrite')
                            log = (out / f'task-{task.task_id}.log').open('xb')
                            os.chmod(log.name, 0o600)
                            proc = subprocess.Popen(argv(task, harbor, jobname, oracle=args.oracle),
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
                            active.append(dict(task=task, proc=proc, started=now, log=log, lock=lock, job=job))
                            state['tasks'][tid] = {'status': 'running', 'pid': proc.pid,
                                                  'started_at': time.time(), 'job': str(job)}
                        except (OSError, ValueError) as exc:
                            if log is not None:
                                log.close()
                            if lock is not None:
                                lock.close()
                            state['tasks'][tid] = {'status': 'not_started', 'reason': type(exc).__name__}
                if pending and not active and (reasons or now - started > args.wait_seconds):
                    while pending:
                        state['tasks'][str(pending.popleft().task_id)] = {'status': 'deferred_resource_or_wait_budget'}
                atomic(out / 'state.json', state)
                if pending or active:
                    time.sleep(1)
            state['status'] = 'stopped' if stop[0] else 'finished'
            atomic(out / 'state.json', state)
            return 0 if all(v['status'] == 'finished' for v in state['tasks'].values()) else 1
        finally:
            # On supervisor exception, do not orphan a running process group.
            for entry in active:
                signal_group(entry['proc'], signal.SIGTERM)
            for entry in active:
                try:
                    entry['proc'].wait(timeout=20)
                except subprocess.TimeoutExpired:
                    signal_group(entry['proc'], signal.SIGKILL)
                    entry['proc'].wait()
                entry['log'].close()
                entry['lock'].close()
                state['tasks'][str(entry['task'].task_id)].update(status='interrupted', cleanup_required=True)
            if active:
                state['status'] = 'interrupted'
                atomic(out / 'state.json', state)
            for s, handler in old.items():
                signal.signal(s, handler)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--batch-id', required=True)
    p.add_argument('--execute', action='store_true', help='default is a read-only plan')
    p.add_argument('--oracle', action='store_true', help='synthetic-only transport checks, no model calls')
    p.add_argument('--trials', type=int, default=4)
    p.add_argument('--backends', type=int, default=2)
    p.add_argument('--timeout', type=int, default=2400)
    p.add_argument('--wait-seconds', type=int, default=60)
    p.add_argument('--harbor', type=Path, default=ROOT/'workdir/harbor-tools/harbor/bin/harbor')
    args = p.parse_args()
    os.umask(0o077)
    try:
        return run(args, load_tasks(args.manifest))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Untrusted manifest/paths must not leak embedded secrets through exceptions.
        print(f'Batch refused: {type(exc).__name__}; check manifest, lease, resources and executable.')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
