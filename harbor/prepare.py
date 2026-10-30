#!/usr/bin/env python3
"""Stage a fixed minimax snapshot as a local Harbor review task. Never claim/callback."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
PRIVATE_ROOT = HERE.parent / "workdir" / "harbor"
MAX_BYTES = 50 * 1024 * 1024
MAX_FILES = 20000
SECRET = re.compile(rb"(?:github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|https?://[^\s/@:]+:[^\s/@]+@)")


def git(repo: Path, *args: str) -> bytes:
    # Git object reads only; no credentials, hooks, global helper or inherited PI environment.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR") if k in os.environ}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    proc = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "--literal-pathspecs", "-C", str(repo), *args],
                          env=env, capture_output=True, timeout=60)
    if proc.returncode:
        raise ValueError("git object read failed (details suppressed to protect credentials)")
    return proc.stdout


def snapshot(repo: Path, commit: str, acknowledge_fixture_secrets: bool = False,
             acknowledge_secret_files: bool = False) -> dict[str, tuple[bytes, int]]:
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", commit):
        raise ValueError("provide a full 40/64-hex commit SHA")
    if git(repo, "rev-parse", "--verify", commit + "^{commit}").decode().strip().lower() != commit.lower():
        raise ValueError("commit mismatch")
    entries = git(repo, "ls-tree", "-rz", commit, "--", "minimax").split(b"\0")
    result: dict[str, tuple[bytes, int]] = {}
    size = 0
    for entry in filter(None, entries):
        fields, raw_path = entry.split(b"\t", 1)
        mode, kind, sha = fields.decode().split()
        path = raw_path.decode("utf-8")
        parts = Path(path).parts
        if len(parts) < 2 or parts[0] != "minimax" or any(p in ("..", ".git") for p in parts):
            raise ValueError("invalid scoped file path")
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("symlinks/submodules require explicit staging review")
        name = parts[-1].lower()
        secret_name = (name in {".env", "auth.json", "id_rsa", "id_ed25519"}
                       or name.endswith((".pem", ".key", ".token"))
                       or (name.startswith(".env.") and not name.endswith(".example")))
        if secret_name and not acknowledge_secret_files:
            raise ValueError("credential-like file requires staging review")
        # Bound memory before reading any candidate blob.
        blob_size = int(git(repo, "cat-file", "-s", sha))
        if len(result) >= MAX_FILES or size + blob_size > MAX_BYTES:
            raise ValueError("snapshot exceeds pilot size/file limit")
        data = git(repo, "cat-file", "blob", sha)
        # An explicit acknowledgement skips only the content heuristic; filename rules
        # and size bounds still apply, and the exception is recorded in the manifest.
        if SECRET.search(data) and not acknowledge_fixture_secrets:
            raise ValueError("possible credential in submission; quarantine for human review")
        result[path] = (data, 0o755 if mode == "100755" else 0o644)
        size += len(data)
    if not result:
        raise ValueError("no minimax files at the specified commit")
    return result


def prepare(task_id: int, repo: Path | None = None, commit: str | None = None, *, synthetic: bool = False,
            base_image: str | None = None, services: bool = False,
            memory_mb: int | None = None, offline_install: bool = False,
            pip_index: str | None = None, acknowledge_fixture_secrets: bool = False,
            acknowledge_secret_files: bool = False) -> Path:
    # An immutable local image ID, not an arbitrary registry/tag or Dockerfile fragment.
    if base_image is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", base_image):
        raise ValueError("prewarmed base image must be an immutable local sha256 image ID")
    if services and not base_image:
        raise ValueError("services require an explicitly prewarmed image")
    if offline_install and not base_image:
        raise ValueError("offline install requires the prewarmed wheel image")
    if pip_index is not None:
        parsed = urlsplit(pip_index)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("pip index must be a credential-free HTTPS URL")
    if offline_install and pip_index:
        raise ValueError("offline install and a pip index are contradictory")
    # 2 GiB is the measured floor for a real submission (install + service +
    # sample). 1 GiB completed but hit the cgroup limit thousands of times.
    memory_mb = memory_mb if memory_mb is not None else 2048
    if not 2048 <= memory_mb <= 8192:
        raise ValueError("memory_mb must be 2048..8192; 1 GiB is measured but not accepted as a default")
    if task_id <= 0:
        raise ValueError("task_id must be positive")
    if synthetic:
        files = {"minimax/README.md": (b"Synthetic transport fixture, not a contestant. No database, model transcript or runnable application supplied. Report missing evidence honestly.\n", 0o644)}
        commit = "0" * 40  # explicitly synthetic, never a valid platform receipt
    else:
        if repo is None or commit is None:
            raise ValueError("repo and full commit required")
        files = snapshot(repo, commit, acknowledge_fixture_secrets, acknowledge_secret_files)
    commit = commit.lower()
    name = f"review-{task_id}-{commit[:12]}"
    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = PRIVATE_ROOT / name
    if destination.exists():
        raise ValueError("staged task already exists; will not overwrite evidence")
    temp = Path(tempfile.mkdtemp(prefix=".staging-", dir=PRIVATE_ROOT))
    try:
        shutil.copytree(HERE / "template", temp, dirs_exist_ok=True)
        shutil.copytree(HERE / "reviewer-skill", temp / "environment/reviewer-skill")
        if base_image:
            dockerfile = (HERE / 'template/environment/Dockerfile').read_text()
            snapshot_part = dockerfile[dockerfile.index('# Only a fixed, scanned'):]
            # BuildKit treats FROM sha256:<id> as a registry repository. Use a
            # content-named local tag; the batch preflight verifies its exact ID.
            local_tag = 'harbor/prewarm:sha-' + base_image.split(':', 1)[1]
            # Measured waste: a submission whose pins are all cached still spent
            # the full 240 s budget on network pip. With --offline-install pip
            # resolves only from the image wheel set and fails in about a second,
            # so the reviewer adapts instead of waiting.
            offline_env = ('ENV PIP_NO_INDEX=1 PIP_TIMEOUT=5 PIP_RETRIES=0\n'
                           if offline_install else '')
            if pip_index:
                offline_env += f'ENV PIP_INDEX_URL={pip_index} PIP_TIMEOUT=60 PIP_RETRIES=2\n'
            (temp / 'environment/Dockerfile').write_text(
                f'FROM {local_tag}\nUSER root\n' + offline_env + snapshot_part)
            (temp / 'environment/input').mkdir(exist_ok=True)
            (temp / 'environment/input/runtime-profile.json').write_text(json.dumps({
                'base_image': base_image, 'services': services,
                'offline_install': offline_install,
                'dependency_policy': 'explicit compatibility experiment, not original pin success'}))
        for rel, (data, mode) in files.items():
            target = temp / "environment/submission" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
        identity = {"task_id": task_id, "commit_id": commit, "scope": "minimax"}
        manifest = {**identity, "synthetic": synthetic, "auto_callback": False,
                    "runtime_profile": "backend" if services else "light", "base_image": base_image,
                    "offline_install": offline_install,
                    "credential_scan": (
                        "acknowledged_fixture_and_files" if (acknowledge_fixture_secrets and acknowledge_secret_files)
                        else "acknowledged_secret_files" if acknowledge_secret_files
                        else "acknowledged_fixture" if acknowledge_fixture_secrets
                        else "strict"),
                    "files": {p: {"sha256": hashlib.sha256(data).hexdigest(), "mode": oct(mode)}
                              for p, (data, mode) in files.items()}}
        (temp / "environment/input").mkdir(exist_ok=True)
        (temp / "environment/input/manifest.json").write_text(json.dumps(manifest, indent=2))
        (temp / "tests/expected.json").write_text(json.dumps(identity, indent=2))
        config = temp / "task.toml"
        config.write_text(config.read_text().replace("__TASK_NAME__", name)
                          .replace("memory_mb = 2048", f"memory_mb = {memory_mb}"))
        # Oracle is a transport/report check only; it performs no contestant grading.
        (temp / "solution/expected.json").write_text(json.dumps(identity, indent=2))
        temp.rename(destination)
    except BaseException:
        shutil.rmtree(temp)
        raise
    return destination


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task-id", type=int, required=True)
    p.add_argument("--repo", type=Path)
    p.add_argument("--commit")
    p.add_argument("--synthetic", action="store_true", help="use no private source; no model required for oracle")
    p.add_argument("--base-image", help="immutable local prewarmed sha256 image ID")
    p.add_argument("--services", action="store_true",
                   help="bootstrap an evaluator-side PowerContext; only when the submission "
                        "documents an EXTERNAL service it does not start itself")
    p.add_argument("--memory-mb", type=int, default=None,
                   help="agent environment memory (2048..8192); default 2048, use 4096+ for heavy tasks")
    p.add_argument("--offline-install", action="store_true",
                   help="set PIP_NO_INDEX for a submission whose pins are all cached (fast failure)")
    p.add_argument("--acknowledge-secret-files", action="store_true",
                   help="allow credential-like FILENAMES (.env, *.pem) in the staged copy; recorded")
    p.add_argument("--acknowledge-fixture-secrets", action="store_true",
                   help="skip ONLY the content heuristic for known test fixtures (recorded in the manifest)")
    p.add_argument("--pip-index", default=None,
                   help="credential-free HTTPS index for uncached pins (recorded as an adjustment)")
    args = p.parse_args()
    if args.synthetic and (args.repo or args.commit):
        p.error("synthetic cannot be combined with repo/commit")
    try:
        print(prepare(args.task_id, args.repo, args.commit, synthetic=args.synthetic,
                      base_image=args.base_image, services=args.services, memory_mb=args.memory_mb,
                      offline_install=args.offline_install, pip_index=args.pip_index,
                      acknowledge_fixture_secrets=args.acknowledge_fixture_secrets,
                      acknowledge_secret_files=args.acknowledge_secret_files))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        p.exit(2, f"Staging refused: {type(exc).__name__}. Check fixed commit, scope, size and credential policy.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
