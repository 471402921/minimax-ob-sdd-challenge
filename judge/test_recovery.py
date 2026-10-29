"""Offline credential persistence and recovery tests; no real credentials or HTTP."""
import argparse
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import credential_store as store
import repo_fetch
import review


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.env = patch.dict(os.environ, {'OB_JUDGE_CREDENTIAL_DIR': str(self.root / 'private')})
        self.env.start()
        self.pending = dict(task_id=8, question_id=600077, branch_requested='main',
                            commit_requested='a' * 40, claimed_epoch=time.time(),
                            platform_deadline_epoch=time.time() + 900, stage='fetch_failed',
                            checkout_dir=str(self.work / 'old'), scope_dir='minimax')
        self.url = 'https://fixture:FAKE_SECRET_ONLY@github.com/example/repo'
        self.args = argparse.Namespace(workdir=self.work, task_id=8, runlog=self.root / 'log')

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def save_pending(self):
        review._atomic_write_json(review._pending_path(self.work, 8), self.pending)

    def test_roundtrip_permissions_and_no_secret_in_workdir(self):
        store.save(self.work, self.pending, self.url)
        self.assertEqual(store.load(self.work, self.pending), self.url)
        file = next((self.root / 'private').rglob('task-8.json'))
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(file.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'private').stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(self.work.iterdir()), [])
        data = json.loads(file.read_text())
        self.assertEqual(set(data), {'binding', 'git_repo'})
        store.delete(self.work, 8)
        self.assertFalse(file.exists())

    def test_wrong_lease_and_permissions_rejected(self):
        store.save(self.work, self.pending, self.url)
        with self.assertRaises(store.CredentialStoreError):
            store.load(self.work, {**self.pending, 'claimed_epoch': 0})
        file = next((self.root / 'private').rglob('task-8.json'))
        file.chmod(0o644)
        with self.assertRaises(store.CredentialStoreError):
            store.load(self.work, self.pending)

    def test_symlink_and_repository_store_rejected(self):
        store.save(self.work, self.pending, self.url)
        file = next((self.root / 'private').rglob('task-8.json'))
        file.unlink()
        file.symlink_to(self.root / 'elsewhere')
        with self.assertRaises(store.CredentialStoreError):
            store.load(self.work, self.pending)
        with patch.dict(os.environ, {'OB_JUDGE_CREDENTIAL_DIR': str(store.REPO_ROOT / 'workdir/private')}):
            with self.assertRaises(store.CredentialStoreError):
                store.save(self.work, self.pending, self.url)

    def test_recover_missing_and_expired_never_claim(self):
        self.save_pending()
        with patch.object(review, '_client_from_env', side_effect=AssertionError('must not claim')):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(review.cmd_recover(self.args), 4)
                self.pending['platform_deadline_epoch'] = 0
                self.save_pending()
                self.assertEqual(review.cmd_recover(self.args), 6)

    def test_recover_keeps_lease_and_original_checkout(self):
        self.save_pending()
        store.save(self.work, self.pending, self.url)
        old = Path(self.pending['checkout_dir'])
        old.mkdir()
        (old / 'marker').write_text('original')
        seen = []
        def clone(url, dest, **kw):
            seen.append((url, kw))
            (dest / 'minimax').mkdir(parents=True)
            return repo_fetch.Checkout(dest, 'main', 'a' * 40)
        with patch.object(review, 'clone_and_checkout', clone), patch.object(review, '_client_from_env', side_effect=AssertionError('must not claim')), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(review.cmd_recover(self.args), 0)
        out = json.loads(review._pending_path(self.work, 8).read_text())
        self.assertEqual(out['platform_deadline_epoch'], self.pending['platform_deadline_epoch'])
        self.assertEqual(out['stage'], 'awaiting_review')
        self.assertNotEqual(out['checkout_dir'], str(old))
        self.assertEqual((old / 'marker').read_text(), 'original')
        self.assertEqual(seen[0][0], self.url)
        self.assertEqual(seen[0][1]['commit_id'], 'a' * 40)
        for p in self.work.rglob('*'):
            if p.is_file(): self.assertNotIn('FAKE_SECRET_ONLY', p.read_text())

    def test_finish_removes_secret(self):
        self.save_pending()
        store.save(self.work, self.pending, self.url)
        review._finish(self.work, self.args.runlog, self.pending, verdict='error', reason='fixture', extra={})
        self.assertFalse(list((self.root / 'private').rglob('task-8.json')))
        self.assertTrue(review._submitted_path(self.work, 8).exists())

    def test_ack_mismatch_preserves_recovery_material(self):
        store.save(self.work, self.pending, self.url)
        for response in ({}, {'code': 200, 'data': None}, {'code': 200, 'data': {'taskId': 9}},
                         {'code': 200, 'data': {'taskId': True}}):
            with self.assertRaises(review.OBProtocolError):
                review._require_ack(response, 8)
            self.assertEqual(store.load(self.work, self.pending), self.url)
        review._require_ack({'code': 200, 'data': {'taskId': 8}}, 8)

    def test_fetch_auth_error_is_not_missing_commit(self):
        def run(args, *, cwd, env, timeout, fail_msg):
            if args[0] == 'clone': Path(args[-1]).mkdir()
            if args[:2] == ['rev-parse', '--verify']:
                raise repo_fetch.RepoFetchError('missing locally', 2)
            if args[0] == 'fetch':
                raise repo_fetch.RepoFetchError('authentication unavailable', 4)
            return __import__('subprocess').CompletedProcess(args, 0, stdout='', stderr='')
        with patch.object(repo_fetch, '_run_git', run):
            with self.assertRaises(repo_fetch.RepoFetchError) as cm:
                repo_fetch.clone_and_checkout(self.url, self.work / 'new', branch='main', commit_id='a'*40)
        self.assertEqual(cm.exception.fail_msg, 4)

    def test_missing_commit_fetch_uses_same_askpass_and_clean_url(self):
        calls = []
        def run(args, *, cwd, env, timeout, fail_msg):
            calls.append((args, env.get('OB_JUDGE_GIT_PAT'), env.get('GIT_ASKPASS')))
            if args[0] == 'clone': Path(args[-1]).mkdir()
            if args[:2] == ['rev-parse', '--verify'] and sum(a[:2] == ['rev-parse', '--verify'] for a,_,_ in calls) == 1:
                raise repo_fetch.RepoFetchError('missing locally', 2)
            return __import__('subprocess').CompletedProcess(args, 0, stdout='a'*40+'\n', stderr='')
        with patch.object(repo_fetch, '_run_git', run):
            out = repo_fetch.clone_and_checkout(self.url, self.work / 'new', branch='main', commit_id='a'*40)
        fetch = [c for c in calls if c[0][0] == 'fetch'][0]
        self.assertEqual(fetch[1], 'FAKE_SECRET_ONLY')
        self.assertNotIn('FAKE_SECRET_ONLY', ' '.join(fetch[0]))
        self.assertEqual(fetch[0][-1], 'a'*40)
        self.assertFalse(Path(fetch[2]).exists())
        self.assertEqual(out.commit_id, 'a'*40)


if __name__ == '__main__':
    unittest.main()
