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


def snapshot(repo: Path, commit: str) -> dict[str, tuple[bytes, int]]:
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
        if name in {".env", "auth.json", "id_rsa", "id_ed25519"} or name.endswith((".pem", ".key", ".token")) or (name.startswith(".env.") and not name.endswith(".example")):
            raise ValueError("credential-like file requires staging review")
        # Bound memory before reading any candidate blob.
        blob_size = int(git(repo, "cat-file", "-s", sha))
        if len(result) >= MAX_FILES or size + blob_size > MAX_BYTES:
            raise ValueError("snapshot exceeds pilot size/file limit")
        data = git(repo, "cat-file", "blob", sha)
        if SECRET.search(data):
            raise ValueError("possible credential in submission; quarantine for human review")
        result[path] = (data, 0o755 if mode == "100755" else 0o644)
        size += len(data)
    if not result:
        raise ValueError("no minimax files at the specified commit")
    return result


def prepare(task_id: int, repo: Path | None = None, commit: str | None = None, *, synthetic: bool = False) -> Path:
    if task_id <= 0:
        raise ValueError("task_id must be positive")
    if synthetic:
        files = {"minimax/README.md": (b"Synthetic transport fixture, not a contestant. No database, model transcript or runnable application supplied. Report missing evidence honestly.\n", 0o644)}
        commit = "0" * 40  # explicitly synthetic, never a valid platform receipt
    else:
        if repo is None or commit is None:
            raise ValueError("repo and full commit required")
        files = snapshot(repo, commit)
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
        for rel, (data, mode) in files.items():
            target = temp / "environment/submission" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(mode)
        identity = {"task_id": task_id, "commit_id": commit, "scope": "minimax"}
        manifest = {**identity, "synthetic": synthetic, "auto_callback": False,
                    "files": {p: {"sha256": hashlib.sha256(data).hexdigest(), "mode": oct(mode)}
                              for p, (data, mode) in files.items()}}
        (temp / "environment/input").mkdir()
        (temp / "environment/input/manifest.json").write_text(json.dumps(manifest, indent=2))
        (temp / "tests/expected.json").write_text(json.dumps(identity, indent=2))
        config = temp / "task.toml"
        config.write_text(config.read_text().replace("__TASK_NAME__", name))
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
    args = p.parse_args()
    if args.synthetic and (args.repo or args.commit):
        p.error("synthetic cannot be combined with repo/commit")
    try:
        print(prepare(args.task_id, args.repo, args.commit, synthetic=args.synthetic))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        p.exit(2, f"Staging refused: {type(exc).__name__}. Check fixed commit, scope, size and credential policy.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
