"""Offline supervisor contracts. No platform, model or Docker calls."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import batch


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.private = self.root/'workdir/harbor'
        self.private.mkdir(parents=True)
        self.patches = [patch.object(batch, 'ROOT', self.root),
                        patch.object(batch, 'PRIVATE', self.private),
                        patch.object(batch, 'BATCHES', self.root/'runs/harbor-batches')]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def fixture(self, tid=1, synthetic=True):
        task = self.private/f'review-{tid}'
        (task/'environment/input').mkdir(parents=True)
        (task/'task.toml').write_text('schema_version="1.4"\n')
        ident = dict(task_id=tid,commit_id=('0' if synthetic else 'a')*40,
                     scope='minimax',auto_callback=False,synthetic=synthetic)
        (task/'environment/input/manifest.json').write_text(json.dumps(ident))
        manifest = self.root/'batch.json'
        manifest.write_text(json.dumps({'tasks':[{'path':str(task),'profile':'light'}]}))
        return manifest

    def test_manifest(self):
        tasks=batch.load_tasks(self.fixture())
        self.assertEqual(tasks[0].task_id,1)
        self.assertTrue(tasks[0].synthetic)
        self.assertEqual(tasks[0].profile,'light')

    def test_reject_outside_duplicate_and_unknown_fields(self):
        m=self.fixture();d=json.loads(m.read_text())
        for rows in [[{'path':'/tmp'}],d['tasks']*2,[{**d['tasks'][0],'command':'echo dangerous'}]]:
            m.write_text(json.dumps({'tasks':rows}))
            with self.assertRaises(ValueError):batch.load_tasks(m)

    def test_symlink_and_mutation(self):
        task=batch.load_tasks(self.fixture())[0]
        (task.path/'new').write_text('changed')
        self.assertNotEqual(batch.tree_digest(task.path),task.digest)
        (task.path/'link').symlink_to('/tmp')
        with self.assertRaises(ValueError):batch.tree_digest(task.path)

    def test_lease_guards(self):
        task=batch.load_tasks(self.fixture(synthetic=False))[0]
        path=self.root/'workdir/task-1.pending.json'
        base=dict(task_id=1,stage='awaiting_review',commit_id='a'*40,
                  deadline_epoch=5000,platform_deadline_epoch=5200)
        path.write_text(json.dumps(base));batch.check_lease(task,1000,now=1000)
        for change in [dict(stage='fetch_failed'),dict(commit_id='b'*40),dict(deadline_epoch=1500)]:
            path.write_text(json.dumps({**base,**change}))
            with self.assertRaises(ValueError):batch.check_lease(task,1000,now=1000)
        path.write_text(json.dumps(base))
        (self.root/'workdir/task-1.submitted.json').write_text('{}')
        with self.assertRaises(ValueError):batch.check_lease(task,1,now=1000)

    def test_result_not_score(self):
        job=self.root/'job';job.mkdir()
        self.assertEqual(batch.result_status(job,0)['status'],'needs_attention')
        (job/'result.json').write_text(json.dumps(dict(finished_at='now',n_total_trials=1,
            stats=dict(n_completed_trials=1,n_errored_trials=0,n_running_trials=0))))
        result=batch.result_status(job,0)
        self.assertEqual(result['status'],'finished')
        self.assertNotIn('verdict',result)
        self.assertEqual(batch.result_status(job,1)['status'],'needs_attention')

    def test_oracle_only_synthetic(self):
        task=batch.load_tasks(self.fixture(synthetic=False))[0]
        with self.assertRaises(ValueError):batch.argv(task,Path('/harbor'),'job',oracle=True)

    def test_command_no_uvx_and_one_trial(self):
        # run_pilot.command checks its own ROOT; patch it too.
        import run_pilot
        task=batch.load_tasks(self.fixture())[0]
        with patch.object(run_pilot,'ROOT',self.root):
            a=batch.argv(task,Path('/bin/harbor'),'batch-1',oracle=False)
        self.assertEqual(a[:3],['/bin/harbor','run','--config'])
        self.assertNotIn('uvx',a)
        self.assertEqual(a[a.index('--n-concurrent')+1],'1')
        self.assertIn('OB_EVAL_MODEL_KEY',a)

    def test_supervisor_fake_harbor_no_model(self):
        m=self.fixture();tasks=batch.load_tasks(m)
        exe=self.root/'fake-harbor'
        exe.write_text('#!/usr/bin/python3\nimport sys,json,pathlib\n'
            'if "--version" in sys.argv: print("0.25.0");sys.exit(0)\n'
            'a=sys.argv;p=pathlib.Path(a[a.index("--jobs-dir")+1])/a[a.index("--job-name")+1];p.mkdir(parents=True)\n'
            'p.joinpath("result.json").write_text(json.dumps({"finished_at":"now","n_total_trials":1,"stats":{"n_completed_trials":1,"n_errored_trials":0,"n_running_trials":0}}))\n')
        exe.chmod(0o755)
        args=argparse.Namespace(trials=4,backends=2,timeout=60,wait_seconds=0,
            batch_id='offline',execute=True,oracle=True,harbor=exe)
        from dataclasses import make_dataclass
        S=make_dataclass('S',[('memory_available_gib',float)])
        with patch.object(batch,'capture',return_value=S(60)),patch.object(batch,'AdmissionPolicy') as policy:
            policy.return_value.evaluate.return_value=[]
            self.assertEqual(batch.run(args,tasks),0)
        d=json.loads((self.root/'runs/harbor-batches/offline/state.json').read_text())
        self.assertEqual(d['tasks']['1']['status'],'finished')
        self.assertFalse(d['auto_callback'])

    def test_lock_exclusion(self):
        with batch.acquire(self.root/'lock'):
            with self.assertRaises(BlockingIOError): batch.acquire(self.root/'lock')
        with batch.acquire(self.root/'lock'):pass


if __name__=='__main__':unittest.main()
