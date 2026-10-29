"""Offline checks for explicitly confirmed inaccessible-repository zero scores."""
import argparse
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import review


class UnavailableTests(unittest.TestCase):
    def run_case(self, *, confirmed=True, verdict='fail', reason='Operator confirmed inaccessible',
                 stage='fetch_failed', fail_msg=4, expired=False, mismatch=False, ack=8):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            pending = dict(task_id=8, stage=stage, fetch_fail_msg=fail_msg,
                           branch_requested='main', commit_requested=('b' if mismatch else 'a')*40,
                           platform_deadline_epoch=0 if expired else time.time()+600)
            review._atomic_write_json(review._pending_path(work, 8), pending)
            (work/'claims').mkdir()
            (work/'claims/claim-8.json').write_text(json.dumps(dict(
                taskId=8, branch='main', commitId='a'*40, resultType=1,
                subQuestions=[dict(subQuestionId=800514, maxScore=20)])))
            args = argparse.Namespace(workdir=work, runlog=work/'log', task_id=8,
                                      verdict=verdict, reason=reason, fail_msg=4,
                                      confirmed_repo_unavailable=confirmed)
            client = Mock()
            client.report_success.return_value = dict(code=200, data=dict(taskId=ack))
            with patch.object(review, '_client_from_env', return_value=client), \
                 patch.object(review.credential_store, 'delete') as delete, \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = review.cmd_submit(args)
            return rc, client, delete.called, review._pending_path(work, 8).exists()

    def test_explicit_zero_without_checkout(self):
        rc, client, cleaned, pending = self.run_case()
        self.assertEqual(rc, 0)
        self.assertEqual(client.report_success.call_args.kwargs['verdicts'], {800514: False})
        self.assertEqual(client.report_success.call_args.kwargs['commit_id'], 'a'*40)
        client.report_failure.assert_not_called()
        self.assertTrue(cleaned)
        self.assertFalse(pending)

    def test_guards(self):
        for changes in [dict(confirmed=False), dict(verdict='pass'), dict(verdict='error'),
                        dict(reason=''), dict(stage='claimed'), dict(fail_msg=5),
                        dict(mismatch=True), dict(expired=True)]:
            with self.subTest(changes=changes):
                rc, client, cleaned, pending = self.run_case(**changes)
                self.assertNotEqual(rc, 0)
                client.report_success.assert_not_called()
                client.report_failure.assert_not_called()
                self.assertFalse(cleaned)
                self.assertTrue(pending)

    def test_uncertain_ack_preserves_pending(self):
        rc, client, cleaned, pending = self.run_case(ack=9)
        self.assertEqual(rc, 7)
        self.assertFalse(cleaned)
        self.assertTrue(pending)


if __name__ == '__main__':
    unittest.main()
