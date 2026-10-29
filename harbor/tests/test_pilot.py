"""Offline staging/report-protocol tests. Never contact OB or a model."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


prepare = module("pilot_prepare", ROOT / "prepare.py")
validator = module("pilot_validator", ROOT / "template/tests/validate_report.py")
runner = module("pilot_runner", ROOT / "run_pilot.py")


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "minimax").mkdir()
        (self.repo / "minimax/run.sh").write_text("#!/bin/sh\nprintf 'fixture\\n'\n")
        (self.repo / "minimax/run.sh").chmod(0o755)
        (self.repo / "qoder").mkdir()
        (self.repo / "qoder/private.txt").write_text("out-of-scope")
        self.commit = self.commit_all()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args], env=env, stderr=subprocess.DEVNULL)

    def commit_all(self):
        self.git("add", "-A")
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD").decode().strip()

    def test_fixed_commit_not_dirty_tree_or_other_scope(self):
        (self.repo / "minimax/run.sh").write_text("dirty")
        (self.repo / "minimax/untracked.txt").write_text("do not export")
        files = prepare.snapshot(self.repo, self.commit)
        self.assertEqual(set(files), {"minimax/run.sh"})
        self.assertIn(b"fixture", files["minimax/run.sh"][0])
        self.assertEqual(files["minimax/run.sh"][1], 0o755)

    def test_full_sha_required(self):
        for value in (self.commit[:8], "--help", "0" * 40):
            with self.assertRaises(ValueError):
                prepare.snapshot(self.repo, value)

    def test_reject_symlink(self):
        (self.repo / "minimax/link").symlink_to("../../private")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            prepare.snapshot(self.repo, self.commit_all())

    def test_reject_submodule(self):
        self.git("update-index", "--add", "--cacheinfo", f"160000,{self.commit},minimax/submodule")
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "submodule")
        with self.assertRaisesRegex(ValueError, "submodules"):
            prepare.snapshot(self.repo, self.git("rev-parse", "HEAD").decode().strip())

    def test_reject_secret_without_echo(self):
        sentinel = "github_pat_" + "A" * 50
        (self.repo / "minimax/notes.txt").write_text(sentinel)
        with self.assertRaises(ValueError) as cm:
            prepare.snapshot(self.repo, self.commit_all())
        self.assertNotIn(sentinel, str(cm.exception))

    def test_reject_credential_filename(self):
        (self.repo / "minimax/.env").write_text("PASSWORD=fixture")
        with self.assertRaisesRegex(ValueError, "credential-like"):
            prepare.snapshot(self.repo, self.commit_all())

    def test_size_limit(self):
        with patch.object(prepare, "MAX_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "limit"):
                prepare.snapshot(self.repo, self.commit)

    def test_prepare_private_output_and_no_git(self):
        with patch.object(prepare, "PRIVATE_ROOT", self.root / "private"):
            dest = prepare.prepare(7, self.repo, self.commit)
            self.assertFalse((dest / "environment/submission/.git").exists())
            manifest = json.loads((dest / "environment/input/manifest.json").read_text())
            self.assertEqual(manifest["commit_id"], self.commit)
            self.assertFalse(manifest["auto_callback"])
            self.assertEqual((dest / "environment/submission/minimax/run.sh").stat().st_mode & 0o777, 0o755)
            with self.assertRaises(ValueError):
                prepare.prepare(7, self.repo, self.commit)

    def test_synthetic_has_no_private_code(self):
        with patch.object(prepare, "PRIVATE_ROOT", self.root / "private"):
            dest = prepare.prepare(9, synthetic=True)
            self.assertTrue(json.loads((dest / "environment/input/manifest.json").read_text())["synthetic"])
            self.assertIn("Synthetic", (dest / "environment/submission/minimax/README.md").read_text())

    def test_memory_profile_and_base_image_are_explicit(self):
        image = "sha256:" + "a" * 64
        with patch.object(prepare, "PRIVATE_ROOT", self.root / "private"):
            light = prepare.prepare(11, self.repo, self.commit)
            self.assertIn("memory_mb = 2048", (light / "task.toml").read_text())
            self.assertNotIn("runtime-profile.json", [p.name for p in (light / "environment/input").iterdir()])
            heavy = prepare.prepare(12, self.repo, self.commit, base_image=image, services=True)
            self.assertIn("memory_mb = 4096", (heavy / "task.toml").read_text())
            profile = json.loads((heavy / "environment/input/runtime-profile.json").read_text())
            self.assertEqual(profile["base_image"], image)
            self.assertTrue(profile["services"])
            # 1 GiB completed but repeatedly hit the cgroup limit; refuse it as a default.
            for bad in (1024, 512, 16384):
                with self.assertRaises(ValueError):
                    prepare.prepare(13, self.repo, self.commit, memory_mb=bad)
            with self.assertRaises(ValueError):
                prepare.prepare(14, self.repo, self.commit, base_image="harbor/prewarm:latest")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        (self.output / "evidence").mkdir()
        self.expected = {"task_id": 7, "commit_id": "a" * 40, "scope": "minimax"}
        self.report = dict(self.expected, schema_version=1, summary="Fixture only",
                           recommendation="review", environment_adjustments=[], checks={})
        for name in validator.CHECKS:
            self.report["checks"][name] = dict(status="not_run", basis="not_run", detail="Not executed", evidence=[])

    def tearDown(self):
        self.temp.cleanup()

    def run_report(self, report=None):
        (self.output / "report.json").write_text(json.dumps(report or self.report))
        return validator.validate(self.output, self.expected)

    def add_evidence(self):
        data = b"exit_code=1; expected behavior failed\n"
        (self.output / "evidence/test.log").write_bytes(data)
        ref = dict(path="evidence/test.log", sha256=hashlib.sha256(data).hexdigest())
        self.report["checks"]["runtime"] = dict(status="failed", basis="rerun", detail="Observed failure", evidence=[ref])
        return ref

    def test_honest_incomplete_report_is_valid_not_pass(self):
        result = self.run_report()
        self.assertTrue(result["report_valid"])
        self.assertEqual(result["recommendation"], "review")
        self.assertFalse(result["auto_callback"])

    def test_failure_report_can_be_valid(self):
        self.add_evidence()
        self.report["recommendation"] = "fail"
        self.assertTrue(self.run_report()["report_valid"])

    def test_wrong_identity_missing_check_and_false_pass(self):
        mutations = []
        for key, value in (("task_id", True), ("commit_id", "b" * 40), ("scope", "qoder"), ("schema_version", True), ("recommendation", "pass")):
            m = copy.deepcopy(self.report)
            m[key] = value
            mutations.append(m)
        m = copy.deepcopy(self.report)
        del m["checks"]["database"]
        mutations.append(m)
        for m in mutations:
            with self.assertRaises(ValueError):
                self.run_report(m)

    def test_observed_status_requires_evidence(self):
        self.report["checks"]["runtime"].update(status="verified", basis="static")
        with self.assertRaises(ValueError):
            self.run_report()

    def test_tampered_hash(self):
        self.add_evidence()
        (self.output / "evidence/test.log").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "hash"):
            self.run_report()

    def test_evidence_traversal_and_absolute_rejected(self):
        ref = self.add_evidence()
        for path in ("evidence/../../etc/passwd", "/etc/passwd", "evidence/..\\secret"):
            ref["path"] = path
            with self.assertRaises(ValueError):
                self.run_report()

    def test_symlink_report_and_evidence_rejected(self):
        ref = self.add_evidence()
        (self.output / "evidence/link").symlink_to("test.log")
        ref["path"] = "evidence/link"
        with self.assertRaises(ValueError):
            self.run_report()
        (self.output / "report.json").unlink()
        (self.output / "report.json").symlink_to("evidence/test.log")
        with self.assertRaises(ValueError):
            validator.validate(self.output, self.expected)

    def test_cli_missing_report_records_invalid_not_success(self):
        expected = self.output / "expected.json"
        expected.write_text(json.dumps(self.expected))
        logs = self.output / "logs"
        p = subprocess.run([os.sys.executable, str(ROOT / "template/tests/validate_report.py"),
                            "--expected", str(expected), "--output", str(self.output), "--logs", str(logs)],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads((logs / "reward.json").read_text())["reward"], 0)


class TemplateTests(unittest.TestCase):
    def test_verifier_has_docker_network_none(self):
        text = (ROOT / 'template/tests/docker-compose.yaml').read_text()
        self.assertIn('network_mode: none', text)
        self.assertNotIn('privileged:', text)
        script = (ROOT / 'template/tests/test.sh').read_text()
        self.assertNotIn('pip install', script.replace('# No model access, network, pip install or candidate code execution here.', ''))

    def test_reviewer_forbids_secret_enumeration_and_empty_fixture_probes(self):
        text = (ROOT / 'template/instruction.md').read_text()
        self.assertIn('Do not run\n`env`, `printenv`', text)
        self.assertIn('Do not install database', text)
        self.assertIn('do not solve or repair', text.lower())


class RunnerTests(unittest.TestCase):
    def test_no_fallback_to_pi_or_platform_credentials(self):
        with self.assertRaises(ValueError):
            runner.environment(Path('/private'), {'ANTHROPIC_API_KEY': 'pi-key', 'OB_JUDGE_TOKEN': 'platform-key'})

    def test_only_dedicated_key_is_forwarded(self):
        parent = {runner.KEY_NAME: 'dedicated-eval', 'OB_JUDGE_TOKEN': 'platform',
                  'GITHUB_TOKEN': 'repo', 'ANTHROPIC_API_KEY': 'pi-key',
                  'PI_CODING_AGENT_DIR': '/host/pi', 'AWS_SECRET_ACCESS_KEY': 'cloud'}
        env = runner.environment(Path('/private'), parent)
        self.assertEqual(set(env), {'PATH', 'HOME', runner.KEY_NAME, 'PYTHONUNBUFFERED', 'UV_NO_PROGRESS', 'PYTHONPATH', 'LITELLM_LOCAL_MODEL_COST_MAP'})
        self.assertEqual(env['HOME'], '/private')
        self.assertNotIn('platform', env.values())

    def test_command_contains_variable_name_not_key(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            task = root / 'workdir/harbor/trial'
            task.mkdir(parents=True)
            (task / 'task.toml').write_text('')
            with patch.object(runner, 'ROOT', root):
                cmd = runner.command(task, 'provider/model', 'https://gateway.example.invalid/v1',
                                     'openai_chat_completions', '/bin/uvx', dry_run=True)
                self.assertIn('OB_EVAL_MODEL_KEY', cmd)
                self.assertIn('--dry-run', cmd)
                self.assertEqual(cmd[cmd.index('--agent') + 1], 'pi_preinstalled:PreinstalledPi')
                self.assertIn('version=1.4.0', cmd)
                self.assertEqual(cmd[cmd.index('--model') + 1], 'provider/model')
                for url in ('http://gateway.invalid', 'https://user:secret@gateway.invalid', 'https://gateway.invalid?key=secret'):
                    with self.assertRaises(ValueError):
                        runner.command(task, 'provider/model', url, 'openai_chat_completions', '/bin/uvx')


if __name__ == "__main__":
    unittest.main()
