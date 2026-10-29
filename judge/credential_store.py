"""Task-scoped repository credentials, separate from redacted receipts and workspaces.

Default: ~/.local/state/minimax-ob-judge/repo-credentials/<workdir-hash>/task-N.json.
Only the control-plane fetcher may read this store. Never upload it to a sandbox.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class CredentialStoreError(RuntimeError):
    """Messages intentionally contain neither paths from submissions nor secrets."""


def _directory(workdir: Path) -> Path:
    root = Path(os.environ.get("OB_JUDGE_CREDENTIAL_DIR", str(
        Path.home() / ".local/state/minimax-ob-judge/repo-credentials"))).absolute()
    if root.resolve().is_relative_to(REPO_ROOT) or root.resolve().is_relative_to(workdir.resolve()):
        raise CredentialStoreError("credential store must be outside repository and workdir")
    # Reject symlink ancestors before creation/use; do not chmod unrelated parents.
    for p in (root, *root.parents):
        if p.is_symlink():
            raise CredentialStoreError("credential directory contains a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid() or not root.is_dir():
        raise CredentialStoreError("credential directory ownership is invalid")
    root.chmod(0o700)
    scope = hashlib.sha256(str(workdir.resolve()).encode()).hexdigest()[:24]
    directory = root / scope
    if directory.is_symlink():
        raise CredentialStoreError("credential namespace is a symlink")
    directory.mkdir(exist_ok=True, mode=0o700)
    if directory.stat().st_uid != os.getuid() or not directory.is_dir():
        raise CredentialStoreError("credential namespace ownership is invalid")
    directory.chmod(0o700)
    return directory


def _filename(task_id: int) -> str:
    if type(task_id) is not int or task_id <= 0:
        raise CredentialStoreError("invalid task id")
    return f"task-{task_id}.json"


def _binding(pending: dict) -> dict:
    return {k: pending[k] for k in ("task_id", "question_id", "branch_requested",
                                   "commit_requested", "claimed_epoch", "platform_deadline_epoch")}


def save(workdir: Path, pending: dict, git_repo: str) -> None:
    """Atomically save only recovery URL + fixed task binding; never platform token."""
    temporary = None
    try:
        filename = _filename(pending["task_id"])
        directory = _directory(workdir)
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump({"binding": _binding(pending), "git_repo": git_repo}, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, directory / filename)
    except (OSError, ValueError, KeyError, TypeError):
        raise CredentialStoreError("could not persist task recovery credential") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def load(workdir: Path, pending: dict) -> str:
    try:
        path = _directory(workdir) / _filename(pending["task_id"])
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as src:
            info = os.fstat(src.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
                raise CredentialStoreError("unsafe recovery credential file")
            data = json.load(src)
        if data.get("binding") != _binding(pending):
            raise CredentialStoreError("recovery credential does not match the task lease/version")
        url = data.get("git_repo")
        if not isinstance(url, str) or not url:
            raise CredentialStoreError("missing recovery repository URL")
        return url
    except (OSError, ValueError, KeyError, TypeError):
        raise CredentialStoreError("task recovery credential unavailable; no task was reclaimed") from None


def delete(workdir: Path, task_id: int) -> None:
    try:
        (_directory(workdir) / _filename(task_id)).unlink(missing_ok=True)
    except OSError:
        raise CredentialStoreError("task completed but credential cleanup failed") from None
