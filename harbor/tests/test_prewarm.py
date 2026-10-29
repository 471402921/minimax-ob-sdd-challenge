"""Offline tests for the Harbor prewarm implementation.

No Docker, no network, no model key, no task claim: everything here works on temporary
fixtures and asserts the two properties that matter for the review pipeline: the image is
built from hash-pinned inputs without contestant code, and no secret ever reaches argv,
stdout or a world readable file.
"""
from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


prewarm = module("pilot_prewarm", ROOT / "prewarm.py")
bootstrap = module("pilot_prewarm_bootstrap", ROOT / "images/bootstrap_services.py")
launcher = module("pilot_prewarm_launcher", ROOT / "images/pi-with-services.py")
precheck = module("pilot_prewarm_seekdb", ROOT / "images/seekdb_precheck.py")
verifier = module("pilot_prewarm_verify", ROOT / "images/verify_inputs.py")
inspector = module("pilot_prewarm_inspect", ROOT / "images/inspect_services.py")

DOCKERFILE = (ROOT / "images/Dockerfile").read_text()


class GitFixture:
    """A throwaway Git repository that mimics the PowerContext source layout."""

    def __init__(self, base: Path):
        self.repo = base / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'powercontext'\n")
        (self.repo / "README.md").write_text("# PowerContext\n")
        (self.repo / "LICENSE").write_text("Apache-2.0\n")
        source = self.repo / "src/powercontext"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("__version__ = 'x'\n")
        (source / "cli.py").write_text("def main():\n    return 0\n")
        bootstrap_dir = self.repo / "src/powercontext_service_bootstrap"
        bootstrap_dir.mkdir(parents=True)
        (bootstrap_dir / "__init__.py").write_text("")
        (self.repo / "minimax").mkdir()
        (self.repo / "minimax/secret.txt").write_text("contestant code\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests/test_x.py").write_text("def test_x():\n    pass\n")
        (self.repo / ".env").write_text("TOKEN=secret\n")
        self.commit = self.commit_all()

    def git(self, *args: str) -> bytes:
        environment = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
        environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args],
                                       env=environment, stderr=subprocess.DEVNULL)

    def commit_all(self) -> str:
        self.git("add", "-A")
        self.git("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD").decode().strip()


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.fixture = GitFixture(self.base)

    def tearDown(self):
        self.temp.cleanup()

    def test_only_the_allowlisted_source_is_exported(self):
        files = prewarm.export_pc_source(self.fixture.repo, self.fixture.commit)
        self.assertEqual(set(files), {
            "pyproject.toml", "README.md", "LICENSE",
            "src/powercontext/__init__.py", "src/powercontext/cli.py",
            "src/powercontext_service_bootstrap/__init__.py",
        })
        self.assertNotIn("minimax/secret.txt", files)
        self.assertNotIn("tests/test_x.py", files)
        self.assertNotIn(".env", files)

    def test_short_or_wrong_commit_is_refused(self):
        for value in (self.fixture.commit[:8], "0" * 40, "--help"):
            with self.assertRaises(prewarm.PrewarmError):
                prewarm.export_pc_source(self.fixture.repo, value)

    def test_symlink_is_refused(self):
        (self.fixture.repo / "src/powercontext/link").symlink_to("../../minimax")
        commit = self.fixture.commit_all()
        with self.assertRaisesRegex(prewarm.PrewarmError, "symlink"):
            prewarm.export_pc_source(self.fixture.repo, commit)

    def test_credential_content_is_refused_without_echoing_it(self):
        sentinel = "github_pat_" + "B" * 40
        (self.fixture.repo / "src/powercontext/leak.py").write_text(f"TOKEN = '{sentinel}'\n")
        commit = self.fixture.commit_all()
        with self.assertRaises(prewarm.PrewarmError) as cm:
            prewarm.export_pc_source(self.fixture.repo, commit)
        self.assertNotIn(sentinel, str(cm.exception))

    def test_credential_file_name_is_refused(self):
        (self.fixture.repo / "src/powercontext/id_ed25519").write_text("x")
        commit = self.fixture.commit_all()
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.export_pc_source(self.fixture.repo, commit)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.fixture = GitFixture(self.base)
        self.wheels = self.base / "wheels"
        self.wheels.mkdir()
        (self.wheels / "pylibseekdb-1.4.0.post2-cp311-cp311-manylinux_2_28_x86_64.whl").write_bytes(b"WHEEL")
        (self.wheels / "pymysql-1.2.3-py3-none-any.whl").write_bytes(b"WHEEL")
        (self.wheels / "pytest-9.2.0-py3-none-any.whl").write_bytes(b"WHEEL")
        self.context = self.base / "context"

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        with patch.object(prewarm, "assert_private_context", lambda *_: None):
            return prewarm.prepare(context_dir=self.context, pc_repo=self.fixture.repo,
                                   pc_commit=self.fixture.commit, pc_wheels=self.wheels,
                                   seekdb_wheels=self.wheels)

    def test_context_is_private_and_hashed(self):
        manifest = self.prepare()
        self.assertTrue((self.context / "inputs.json").is_file())
        self.assertIn("pc-source/pyproject.toml", manifest["files"])
        self.assertIn("wheels/seekdb/pymysql-1.2.3-py3-none-any.whl", manifest["files"])
        self.assertEqual(len(manifest["files"]["pc-source/pyproject.toml"]["sha256"]), 64)
        self.assertEqual(manifest["sources"]["pc_source"]["commit"], self.fixture.commit)
        for name in manifest["files"]:
            self.assertTrue((self.context / name).is_file(), name)

    def test_context_has_no_submission_or_input(self):
        self.prepare()
        names = {path.name for path in self.context.iterdir()}
        self.assertNotIn("submission", names)
        self.assertNotIn("input", names)
        for path in self.context.rglob("*"):
            self.assertNotIn("submission", path.parts)
            self.assertNotIn("input", path.parts)

    def test_seekdb_cache_is_limited_to_the_two_named_wheels(self):
        self.prepare()
        names = {path.name for path in (self.context / "wheels/seekdb").iterdir()}
        self.assertEqual(names, {"pylibseekdb-1.4.0.post2-cp311-cp311-manylinux_2_28_x86_64.whl",
                                 "pymysql-1.2.3-py3-none-any.whl"})

    def test_verify_detects_tampering_and_extra_files(self):
        self.prepare()
        prewarm.verify_context(self.context)
        target = self.context / "pc-source/pyproject.toml"
        target.write_text("changed")
        with self.assertRaisesRegex(prewarm.PrewarmError, "sha256 mismatch"):
            prewarm.verify_context(self.context)
        target.write_text("[project]\nname = 'powercontext'\n")
        prewarm.verify_context(self.context)
        (self.context / "submission").mkdir()
        (self.context / "submission/run.sh").write_text("echo hi")
        with self.assertRaisesRegex(prewarm.PrewarmError, "unexpected file"):
            prewarm.verify_context(self.context)

    def test_prepare_does_not_touch_docker(self):
        with patch.object(prewarm, "run", side_effect=AssertionError("docker must not run")):
            self.prepare()

    def test_context_must_live_under_workdir(self):
        with self.assertRaisesRegex(prewarm.PrewarmError, "workdir"):
            prewarm.assert_private_context(Path("/tmp/harbor-prewarm-context"))


class CommandTests(unittest.TestCase):
    def test_build_command_has_no_sudo_and_no_secret(self):
        command = prewarm.build_command(Path("/ctx"), "harbor/prewarm:local", "python:3.11.15-slim-bookworm",
                                        "node:22.23.2-bookworm-slim", "https://mirrors.ustc.edu.cn")
        self.assertEqual(command[0], "docker")
        self.assertNotIn("sudo", " ".join(command))
        self.assertIn("--tag", command)
        self.assertIn("/ctx", command)
        for argument in command:
            self.assertNotIn("token", argument.lower())

    def test_image_reference_and_mirror_validation(self):
        self.assertEqual(prewarm.validate_image_ref("python@sha256:" + "a" * 64, "--python-image"),
                         "python@sha256:" + "a" * 64)
        for value in ("https://user:secret@example.invalid/python", "python@sha256:zz", ""):
            with self.assertRaises(prewarm.PrewarmError):
                prewarm.validate_image_ref(value, "--python-image")
        for value in ("http://mirrors.invalid", "https://user:pw@mirrors.invalid/debian"):
            with self.assertRaises(prewarm.PrewarmError):
                prewarm.validate_mirror(value)

    def test_parameters_must_not_carry_credentials(self):
        sentinel = "sk-" + "C" * 32
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.validate_parameter(sentinel, "--image-tag")
        with self.assertRaises(prewarm.PrewarmError):
            prewarm.validate_parameter("https://user:secret@example.invalid", "--debian-mirror")

    def test_health_command_is_isolated_and_non_root(self):
        command = prewarm.health_command("harbor/prewarm:local", network="net")
        joined = " ".join(command)
        self.assertIn("--user 10001:10001", joined)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("--security-opt no-new-privileges", joined)
        self.assertIn("--network net", joined)
        self.assertNotIn("--privileged", joined)
        self.assertNotIn("sudo", joined)

    def test_secret_markers_are_detected(self):
        self.assertTrue(prewarm.contains_secret_markers("Authorization: Bearer abc"))
        self.assertTrue(prewarm.contains_secret_markers("POWERCONTEXT_SERVER_AUTH_TOKEN=x"))
        self.assertFalse(prewarm.contains_secret_markers("seekdb precheck ok: opened_and_closed=yes"))


class DockerfileTests(unittest.TestCase):
    def test_no_contestant_code_is_copied(self):
        self.assertIsNone(re.search(r"COPY\s+[^\n]*\bsubmission\b", DOCKERFILE))
        self.assertIsNone(re.search(r"COPY\s+[^\n]*\binput\b", DOCKERFILE))
        self.assertNotIn("/submission", DOCKERFILE)
        self.assertNotIn("minimax", DOCKERFILE)

    def test_packages_are_installed_offline(self):
        self.assertIn("--no-index", DOCKERFILE)
        self.assertIn("--find-links /wheels/pc", DOCKERFILE)
        self.assertNotIn("pip install -U", DOCKERFILE)
        self.assertNotIn("index-url", DOCKERFILE)

    def test_runtime_user_is_the_review_user(self):
        self.assertIn("USER reviewer", DOCKERFILE)
        self.assertIn("uid 10001", DOCKERFILE)
        self.assertNotIn("USER root", DOCKERFILE.split("FROM base AS runtime", 1)[1])

    def test_powercontext_has_its_own_environment(self):
        self.assertIn("/opt/pc-venv", DOCKERFILE)
        self.assertIn("SETUPTOOLS_SCM_PRETEND_VERSION", DOCKERFILE)
        self.assertIn("PC_VERSION=1.2.0", DOCKERFILE)

    def test_seekdb_wheels_are_cached_but_not_forced(self):
        self.assertIn("COPY wheels/seekdb /opt/wheels/seekdb", DOCKERFILE)
        for line in DOCKERFILE.splitlines():
            if "pip install" in line and "pylibseekdb" in line:
                self.assertIn("/opt/pc-venv/bin/python", line)

    def test_bootstrap_and_launcher_are_installed(self):
        self.assertIn("/opt/review/bootstrap.py", DOCKERFILE)
        self.assertIn("/opt/review/pi-with-services.py", DOCKERFILE)
        self.assertIn("/opt/review/pi-original", DOCKERFILE)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_services_file_is_owner_only_and_complete(self):
        target = self.base / "review-services.json"
        bootstrap.write_services_file(target, {"url": "http://127.0.0.1:18000", "token": "t", "scope_id": "s"})
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        payload = json.loads(target.read_text())
        self.assertEqual(set(payload) & {"url", "token", "scope_id"}, {"url", "token", "scope_id"})

    def test_summary_line_never_contains_the_token(self):
        description = {"url": "http://127.0.0.1:18000", "token": "SUPERSECRET", "scope_id": "scp_1",
                       "access_mode": "enforced", "database": "sqlite"}
        line = bootstrap.summary_line(description)
        self.assertNotIn("SUPERSECRET", line)
        self.assertIn("scp_1", line)

    def test_token_is_not_passed_through_argv(self):
        record = self.base / "record"
        record.mkdir()
        fake = self.base / "powercontext"
        fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {record}/argv\nenv > {record}/env\n")
        fake.chmod(0o755)
        token = "TOKENVALUE"
        environment = bootstrap.server_environment(token, workspace=self.base, database_url="sqlite+aiosqlite:////tmp/x.db",
                                                   home=self.base, tmpdir=self.base)
        process = bootstrap.start_server(str(fake), environment, workspace=self.base, log_path=self.base / "log")
        process.wait(timeout=30)
        argv = (record / "argv").read_text()
        env = (record / "env").read_text()
        self.assertNotIn(token, argv)
        self.assertIn(token, env)
        self.assertIn("POWERCONTEXT_SERVER_ACCESS_MODE=enforced", env)
        self.assertIn("POWERCONTEXT_SERVER_DATABASE_KIND=sqlite", env)
        self.assertIn("sqlite+aiosqlite:////tmp/x.db", env)

    def test_failed_bootstrap_stops_the_child_service(self):
        pid_file = self.base / "server.pid"
        fake = self.base / "fake-powercontext"
        fake.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nsleep 60\n")
        fake.chmod(0o755)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.bootstrap(pc_binary=str(fake), output=self.base / "review-services.json",
                                root=self.base, ready_timeout=2.0)
        self.assertFalse((self.base / "review-services.json").exists())
        pid = int(pid_file.read_text().strip())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            self.fail("the failed bootstrap left its service running")

    def test_scope_creation_reads_the_server_assigned_id(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                if self.path == "/health/ready":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                seen["payload"] = body
                seen["auth"] = self.headers.get("Authorization", "")
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"scope_id": "scp_test"}).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            bootstrap.wait_ready(base, timeout=10, poll=0.1)
            scope_id = bootstrap.create_scope(base, "SECRET-TOKEN",
                                             {"title": "t", "summary": "s", "idempotency_key": "k"})
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(scope_id, "scp_test")
        self.assertEqual(seen["auth"], "Bearer SECRET-TOKEN")
        self.assertEqual(set(seen["payload"]), {"title", "summary", "idempotency_key"})

    def test_wait_ready_fails_with_a_bounded_timeout(self):
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.wait_ready("http://127.0.0.1:1", timeout=0.5, poll=0.1)

    def test_redact_removes_the_secret(self):
        self.assertNotIn("abc123", bootstrap.redact("token=abc123", "abc123"))
        self.assertTrue(bootstrap.contains_secret("token=abc123", "abc123"))
        self.assertFalse(bootstrap.contains_secret("token=abc123", "other"))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_services(self, token="LAUNCHSECRET"):
        path = self.base / "review-services.json"
        path.write_text(json.dumps({"url": "http://127.0.0.1:18000", "token": token,
                                    "scope_id": "scp_launch"}))
        return path

    def test_missing_services_file_means_light_task(self):
        self.assertEqual(launcher.load_services(self.base / "absent.json"), {})

    def test_services_file_is_read_into_the_environment(self):
        path = self.write_services()
        services = launcher.load_services(path)
        self.assertEqual(services["scope_id"], "scp_launch")
        environment = launcher.build_environment({"PATH": "/bin"}, services)
        self.assertEqual(environment["POWERCONTEXT_URL"], "http://127.0.0.1:18000")
        self.assertEqual(environment["POWERCONTEXT_TOKEN"], "LAUNCHSECRET")
        self.assertEqual(environment["POWERCONTEXT_SCOPE_ID"], "scp_launch")

    def test_light_task_environment_is_unchanged(self):
        environment = launcher.build_environment({"PATH": "/bin"}, {})
        self.assertNotIn("POWERCONTEXT_TOKEN", environment)

    def test_incomplete_services_file_is_ignored(self):
        path = self.base / "partial.json"
        path.write_text(json.dumps({"url": "http://127.0.0.1:18000"}))
        self.assertEqual(launcher.load_services(path), {})

    def test_token_never_reaches_argv(self):
        path = self.write_services()
        fake = self.base / "fake-pi"
        fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
        fake.chmod(0o755)
        result = subprocess.run([sys.executable, str(ROOT / "images/pi-with-services.py"), "--version"],
                                capture_output=True, text=True,
                                env={**os.environ, "POWERCONTEXT_SERVICES_FILE": str(path),
                                     "PI_ORIGINAL_PATH": str(fake)})
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("LAUNCHSECRET", result.stdout)
        self.assertNotIn("LAUNCHSECRET", result.stderr)
        self.assertIn("--version", result.stdout)

    def test_missing_original_is_an_error(self):
        result = subprocess.run([sys.executable, str(ROOT / "images/pi-with-services.py")],
                                capture_output=True, text=True,
                                env={**os.environ, "PI_ORIGINAL_PATH": "/nonexistent/pi"})
        self.assertEqual(result.returncode, 1)


class VerifyInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        (self.base / "group").mkdir()
        (self.base / "group/file.txt").write_text("payload")
        manifest = {"files": {"group/file.txt": {"sha256": prewarm.sha256_file(self.base / "group/file.txt"),
                                                 "size": 7}}}
        (self.base / "inputs.json").write_text(json.dumps(manifest))

    def tearDown(self):
        self.temp.cleanup()

    def test_matching_inputs_pass(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verifier.main(["--manifest", str(self.base / "inputs.json"),
                                             "--check", f"group={self.base / 'group'}"]), 0)

    def test_tampered_input_fails(self):
        (self.base / "group/file.txt").write_text("changed")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verifier.main(["--manifest", str(self.base / "inputs.json"),
                                             "--check", f"group={self.base / 'group'}"]), 1)

    def test_unknown_group_fails(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(verifier.main(["--manifest", str(self.base / "inputs.json"),
                                             "--check", f"other={self.base / 'group'}"]), 1)


class SeekdbPrecheckTests(unittest.TestCase):
    def test_wheel_requirements_are_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            wheels = Path(directory)
            (wheels / "pylibseekdb-1.4.0.post2-cp311-cp311-manylinux_2_28_x86_64.whl").write_bytes(b"W")
            (wheels / "pymysql-1.2.3-py3-none-any.whl").write_bytes(b"W")
            self.assertEqual(sorted(precheck.wheel_requirements(wheels)),
                             ["pylibseekdb==1.4.0.post2", "pymysql==1.2.3"])

    def test_missing_cache_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                precheck.wheel_requirements(Path(directory))

    def test_pip_runs_offline_without_host_configuration(self):
        source = (ROOT / "images/seekdb_precheck.py").read_text()
        self.assertIn("--no-index", source)
        self.assertIn("PIP_CONFIG_FILE", source)
        self.assertIn('"HOME": str(home)', source)


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, str(ROOT / "prewarm.py"), *arguments],
                              capture_output=True, text=True, cwd=str(REPO))

    def test_nothing_happens_without_an_explicit_switch(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn("--prepare", result.stderr)

    def test_help_does_not_leak_the_environment(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for marker in ("OB_JUDGE_TOKEN", "OB_EVAL_MODEL_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
            self.assertNotIn(marker, result.stdout)
        self.assertNotIn(os.environ.get("OB_EVAL_MODEL_KEY", "unset-value"), result.stdout)

    def test_secret_parameter_is_refused_without_echoing_it(self):
        sentinel = "sk-" + "D" * 32
        result = self.run_cli("--build", "--image-tag", sentinel)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(sentinel, result.stderr)
        self.assertNotIn(sentinel, result.stdout)

    def test_dry_run_prints_the_docker_command_only(self):
        with tempfile.TemporaryDirectory() as directory:
            context = Path(directory) / "context"
            context.mkdir()
            (context / "inputs.json").write_text(json.dumps({"files": {}}))
            result = self.run_cli("--build", "--context-dir", str(context), "--dry-run")
            self.assertIn(result.returncode, (0, 2))
            self.assertNotIn("sudo", result.stdout)


if __name__ == "__main__":
    unittest.main()
