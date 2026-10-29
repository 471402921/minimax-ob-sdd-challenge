"""Offline tests for the deterministic auto-pass gate. No model, platform or Docker."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('triage', ROOT / 'triage.py')
triage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(triage)

COMMIT = 'a' * 40


def evidence(directory: Path, name: str, body: str) -> dict:
    path = directory / 'evidence' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return {'path': f'evidence/{name}', 'sha256': hashlib.sha256(body.encode()).hexdigest()}


def report(directory: Path, *, statuses=None, recommendation='review', task_id=7,
           commit=COMMIT, summary='ok', drop=(), bad_hash=False, extra_check=False) -> Path:
    statuses = statuses or {name: 'verified' for name in triage.CHECKS}
    checks = {}
    for name in triage.CHECKS:
        if name in drop:
            continue
        evidence_items = [evidence(directory, f'{name}.log', f'{name} output')]
        if bad_hash:
            evidence_items[0]['sha256'] = 'b' * 64
        checks[name] = {'status': statuses[name],
                        'basis': 'rerun' if statuses[name] == 'verified' else 'not_run',
                        'detail': f'{name} detail', 'evidence': evidence_items}
    if extra_check:
        checks['extra'] = {'status': 'verified', 'basis': 'rerun', 'detail': 'x', 'evidence': []}
    payload = {'schema_version': 1, 'task_id': task_id, 'commit_id': commit,
               'scope': 'minimax', 'recommendation': recommendation, 'summary': summary,
               'environment_adjustments': [], 'checks': checks}
    path = directory / 'report.json'
    path.write_text(json.dumps(payload))
    return path


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_clean_review_is_auto_pass_candidate(self):
        result = triage.gate(report(self.root), check_manifest=False)
        self.assertEqual(result['classification'], 'auto_pass_candidate')
        self.assertEqual(result['reasons'], [])

    def test_failed_dimension_goes_to_human_and_is_never_auto_scored(self):
        result = triage.gate(report(self.root, statuses={**{n: 'verified' for n in triage.CHECKS},
                                                        'state': 'failed'},
                                    recommendation='fail'), check_manifest=False)
        self.assertEqual(result['classification'], 'human')
        self.assertIn('state', result['failed'])
        self.assertIn('never auto-scores', result['why'])

    def test_hash_mismatch_blocks(self):
        result = triage.gate(report(self.root, bad_hash=True), check_manifest=False)
        self.assertEqual(result['classification'], 'human')
        self.assertTrue(any('hash mismatch' in reason for reason in result['reasons']))

    def test_missing_and_extra_dimensions_block(self):
        for kwargs in ({'drop': ('state',)}, {'extra_check': True}):
            with self.subTest(kwargs=kwargs):
                sub = self.root / str(kwargs)
                sub.mkdir()
                result = triage.gate(report(sub, **kwargs), check_manifest=False)
                self.assertEqual(result['classification'], 'human')

    def test_empty_summary_and_bad_identity_block(self):
        for kwargs in ({'summary': '   '}, {'commit': 'nothex'}, {'task_id': 0}):
            with self.subTest(kwargs=kwargs):
                sub = self.root / str(sorted(kwargs.items()))
                sub.mkdir()
                result = triage.gate(report(sub, **kwargs), check_manifest=False)
                self.assertEqual(result['classification'], 'human')

    def test_uncertain_dimension_blocks(self):
        result = triage.gate(report(self.root, statuses={**{n: 'verified' for n in triage.CHECKS},
                                                        'database': 'uncertain'}),
                             check_manifest=False)
        self.assertEqual(result['classification'], 'human')

    def test_traversal_and_foreign_evidence_refused(self):
        directory = self.root / 'trav'
        directory.mkdir()
        path = report(directory)
        payload = json.loads(path.read_text())
        payload['checks']['runtime']['evidence'] = [{'path': '../outside.log', 'sha256': 'c' * 64}]
        path.write_text(json.dumps(payload))
        result = triage.gate(path, check_manifest=False)
        self.assertEqual(result['classification'], 'human')
        self.assertTrue(any('under evidence/' in reason for reason in result['reasons']))

    def test_manifest_identity_is_checked_when_available(self):
        directory = self.root / 'staged'
        directory.mkdir()
        staged = self.root / 'workdir/harbor/review-7-aaaaaaaaaaaa'
        (staged / 'environment/input').mkdir(parents=True)
        (staged / 'environment/input/manifest.json').write_text(json.dumps(
            {'task_id': 7, 'commit_id': COMMIT}))
        with patch.object(triage, 'ROOT', self.root):
            self.assertEqual(triage.gate(report(directory))['classification'],
                             'auto_pass_candidate')
            other = self.root / 'other'
            other.mkdir()
            result = triage.gate(report(other, task_id=8))
        self.assertEqual(result['classification'], 'human')
        self.assertTrue(any('manifest' in reason for reason in result['reasons']))

    def test_submit_requires_yes_and_cap(self):
        batch = self.root / 'batch'
        batch.mkdir()
        report(batch.parent / 'staged', check_manifest=False) if False else None
        with self.assertRaises(SystemExit):
            triage.main(['--batch', str(batch), '--submit-auto-pass'])
        with self.assertRaises(SystemExit):
            triage.main(['--batch', str(self.root / 'absent')])


if __name__ == '__main__':
    unittest.main()
