#!/usr/bin/env python3
"""Deterministic triage for finished review trials. No model, no platform calls.

A trial produces `report.json` plus hashed evidence. This gate decides whether a
report is strong enough to be an **automatic pass candidate**, or must go to a
human:

  human             any check `failed` (an observed failure is never auto-scored,
                    and never auto-failed either: evaluator mistakes happen)
  human             schema/identity problem, missing evidence, hash mismatch,
                    or not every dimension `verified`
  auto_pass_candidate  six dimensions verified, every referenced evidence file
                    matches its hash, identity matches the staged task, and the
                    recommendation is `pass`/`review` with a non-empty summary

Default output is a report only. `--submit-auto-pass` exists but additionally
requires `--max N` and `--yes`; it is meant for an operator who has already
accepted this policy, and it never submits a `failed` or `uncertain` trial.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
CHECKS = {'runtime', 'minimax', 'context', 'database', 'state', 'code_change'}
STATUSES = {'verified', 'failed', 'blocked', 'not_run', 'uncertain'}
BASES = {'rerun', 'static', 'supplied_record', 'not_run'}
HEX = re.compile(r'^[0-9a-f]{64}$')


class TriageError(RuntimeError):
    """The report cannot be judged safely."""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def staged_manifest(task_id: int, commit: str) -> Path | None:
    """Locate the staged task manifest for this identity, if it still exists."""
    for candidate in sorted((ROOT / 'workdir/harbor').glob(f'review-{task_id}-{commit[:12]}')):
        manifest = candidate / 'environment/input/manifest.json'
        if manifest.is_file():
            return manifest
    return None


def gate(report_path: Path, *, check_manifest: bool = True) -> dict:
    """Return a classification with machine-readable reasons."""
    try:
        report = json.loads(report_path.read_text())
    except (OSError, ValueError):
        raise TriageError('report is missing or not JSON') from None
    reasons: list[str] = []
    facts: dict = {'report': str(report_path)}

    task_id, commit = report.get('task_id'), report.get('commit_id')
    if type(task_id) is not int or task_id <= 0:
        reasons.append('invalid task_id')
    if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        reasons.append('invalid commit_id')
    if report.get('scope') != 'minimax':
        reasons.append('scope is not minimax')

    if check_manifest and not reasons:
        manifest_path = staged_manifest(task_id, commit)
        if manifest_path is None:
            reasons.append('staged manifest not found for this identity')
        else:
            manifest = json.loads(manifest_path.read_text())
            if manifest.get('task_id') != task_id or manifest.get('commit_id') != commit:
                reasons.append('report identity differs from the staged task')

    checks = report.get('checks')
    if not isinstance(checks, dict) or set(checks) != CHECKS:
        reasons.append('checks must contain exactly the six dimensions')
        checks = checks if isinstance(checks, dict) else {}

    statuses = {}
    for name, check in sorted(checks.items()):
        if not isinstance(check, dict):
            reasons.append(f'{name}: check is not an object')
            continue
        status, basis = check.get('status'), check.get('basis')
        statuses[name] = status
        if status not in STATUSES:
            reasons.append(f'{name}: unknown status')
        if basis not in BASES:
            reasons.append(f'{name}: unknown basis')
        if not str(check.get('detail') or '').strip():
            reasons.append(f'{name}: empty detail')
        for evidence in check.get('evidence') or []:
            rel = evidence.get('path') if isinstance(evidence, dict) else None
            want = evidence.get('sha256') if isinstance(evidence, dict) else None
            if not isinstance(rel, str) or '..' in Path(rel).parts or not rel.startswith('evidence/'):
                reasons.append(f'{name}: evidence path must live under evidence/')
                continue
            target = report_path.parent / rel
            if not target.is_file():
                reasons.append(f'{name}: evidence file missing ({rel})')
                continue
            if not isinstance(want, str) or not HEX.fullmatch(want):
                reasons.append(f'{name}: evidence hash malformed ({rel})')
                continue
            if sha256(target) != want:
                reasons.append(f'{name}: evidence hash mismatch ({rel})')

    summary = str(report.get('summary') or '').strip()
    if not summary:
        reasons.append('summary is empty')
    recommendation = report.get('recommendation')
    if recommendation not in {'pass', 'fail', 'review', 'error'}:
        reasons.append('invalid recommendation')

    failed = sorted(name for name, status in statuses.items() if status == 'failed')
    not_verified = sorted(name for name, status in statuses.items() if status != 'verified')
    facts.update({'task_id': task_id, 'commit_id': commit, 'recommendation': recommendation,
                  'statuses': statuses, 'failed': failed, 'not_verified': not_verified,
                  'reasons': reasons})

    if failed:
        facts['classification'] = 'human'
        facts['why'] = 'observed failure: a person decides, the gate never auto-scores a failure'
    elif reasons:
        facts['classification'] = 'human'
        facts['why'] = 'report is not clean enough to trust automatically'
    elif not_verified:
        facts['classification'] = 'human'
        facts['why'] = 'not every dimension is verified'
    elif recommendation not in {'pass', 'review'}:
        facts['classification'] = 'human'
        facts['why'] = f'recommendation {recommendation!r} is not a pass candidate'
    else:
        facts['classification'] = 'auto_pass_candidate'
        facts['why'] = 'six dimensions verified, evidence hashes match, identity matches'
    return facts


def collect(batch: Path) -> list[Path]:
    """Reports for a batch: the state dir, or the sibling runs/harbor/<id>-* jobs."""
    reports = sorted(batch.rglob('artifacts/output/report.json'))
    if reports:
        return reports
    jobs = ROOT / 'runs/harbor'
    return sorted(p for p in jobs.glob(f'{batch.name}-*/**/artifacts/output/report.json'))


def submitted_records() -> dict[int, str]:
    records = {}
    for path in sorted((ROOT / 'workdir').glob('task-*.submitted.json')):
        try:
            data = json.loads(path.read_text())
            if type(data.get('task_id')) is int:
                records[data['task_id']] = str(data.get('verdict'))
        except (OSError, ValueError):
            continue
    return records


def submit(task_id: int, reason: str) -> int:
    """Delegate to the existing guarded submit path; never used for a failure."""
    import subprocess
    command = [sys.executable, str(ROOT / 'judge/review.py'), 'submit',
               '--task-id', str(task_id), '--verdict', 'pass', '--reason', reason]
    return subprocess.run(command, cwd=ROOT).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, required=True,
                        help='runs/harbor-batches/<batch-id>')
    parser.add_argument('--compare-submitted', action='store_true',
                        help='compare classifications with local submitted verdicts')
    parser.add_argument('--submit-auto-pass', action='store_true',
                        help='submit auto-pass candidates as pass (irreversible)')
    parser.add_argument('--max', type=int, default=0)
    parser.add_argument('--yes', action='store_true')
    args = parser.parse_args(argv)
    batch = args.batch if args.batch.is_absolute() else ROOT / args.batch
    if not batch.is_dir():
        parser.error('batch directory not found')
    reports = collect(batch)
    if not reports:
        parser.error('no report.json under this batch')
    results = []
    for report in reports:
        try:
            results.append(gate(report))
        except TriageError as exc:
            results.append({'report': str(report), 'classification': 'human',
                            'why': str(exc), 'reasons': [str(exc)]})
    for item in results:
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    candidates = [r for r in results if r['classification'] == 'auto_pass_candidate']
    print(f'\nsummary: {len(results)} reports, {len(candidates)} auto-pass candidates, '
          f'{len(results) - len(candidates)} need a human', file=sys.stderr)
    if args.compare_submitted:
        known = submitted_records()
        agree = disagree = unknown = 0
        for item in results:
            actual = known.get(item.get('task_id'))
            if actual is None:
                unknown += 1
            elif item['classification'] == 'auto_pass_candidate' and actual == 'pass':
                agree += 1
            elif item['classification'] == 'auto_pass_candidate' and actual == 'fail':
                disagree += 1
            elif item['classification'] == 'human' and actual == 'pass':
                disagree += 1
            else:
                agree += 1
        print(f'compare: agree={agree} disagree={disagree} unknown={unknown}', file=sys.stderr)
    if args.submit_auto_pass:
        if not args.yes or args.max <= 0:
            parser.error('--submit-auto-pass requires --yes and a positive --max')
        if not candidates:
            print('nothing to submit', file=sys.stderr)
            return 0
        for item in candidates[:args.max]:
            reason = (f'通过（自动闸门）。taskId {item["task_id"]} 固定提交六维独立复核均为 verified，'
                      f'报告证据哈希全部匹配且与暂存任务身份一致。自动判定依据：'
                      f'六维状态、证据文件哈希、任务身份；未使用模型替代执行证据。')
            code = submit(item['task_id'], reason)
            print(json.dumps({'task_id': item['task_id'], 'submit_exit': code}), file=sys.stderr)
            if code != 0:
                print('stopping after a failed submit; remaining candidates untouched',
                      file=sys.stderr)
                return code
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
