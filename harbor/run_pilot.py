#!/usr/bin/env python3
"""Run an already staged task with an explicit evaluation key, never host Pi auth."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
KEY_NAME = "OB_EVAL_MODEL_KEY"
FORMATS = ("anthropic_messages", "openai_chat_completions", "openai_responses")


def environment(home: Path, parent: dict[str, str]) -> dict[str, str]:
    key = parent.get(KEY_NAME, "").strip()
    if not key:
        raise ValueError("OB_EVAL_MODEL_KEY is required; host Pi credentials are never a fallback")
    # Allowlist, not os.environ.copy(): no platform/PAT/cloud/host Pi credential inheritance.
    return {"PATH": parent.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(home), KEY_NAME: key, "PYTHONUNBUFFERED": "1", "UV_NO_PROGRESS": "1",
            "PYTHONPATH": str(ROOT / "harbor"), "LITELLM_LOCAL_MODEL_COST_MAP": "True"}


def command(task: Path, model: str, base_url: str, api_format: str, uvx: str, *, dry_run: bool = False) -> list[str]:
    private = (ROOT / "workdir/harbor").resolve()
    task = task.resolve()
    if not task.is_relative_to(private) or not (task / "task.toml").is_file():
        raise ValueError("task must be a staged task under workdir/harbor")
    url = urlsplit(base_url)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("use a credential-free HTTPS model endpoint")
    if not model.strip() or api_format not in FORMATS:
        raise ValueError("explicit model and supported API format required")
    # Harbor 0.25.0 only consumes --model when --agent is supplied. Supplying
    # --agent replaces configured agent options, so repeat the pinned Pi version.
    args = [uvx, "--from", "harbor==0.25.0", "harbor", "run",
            "--config", str(ROOT / "harbor/jobs/pi-local.yaml"), "--path", str(task),
            "--agent", "pi_preinstalled:PreinstalledPi", "--agent-kwarg", "version=1.4.0",
            "--model", model, "--base-url", base_url, "--api-format", api_format,
            "--api-key-name", KEY_NAME]
    if dry_run:
        args.append("--dry-run")
    return args


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task", type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--api-format", choices=FORMATS, required=True)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if not os.environ.get(KEY_NAME, "").strip():
        p.exit(2, "OB_EVAL_MODEL_KEY missing; stopped before launching Harbor.\n")
    uvx = shutil.which("uvx")
    if not uvx:
        p.exit(2, "uvx not installed.\n")
    # Temporary private runner HOME never points at the user's ~/.pi, Docker
    # config or cached cloud credentials; results survive separately in runs/.
    private = ROOT / "workdir/harbor"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="runner-home-", dir=private) as home:
        try:
            cmd = command(args.task, args.model, args.base_url, args.api_format, uvx, dry_run=args.dry_run)
            env = environment(Path(home), dict(os.environ))
        except ValueError as exc:
            p.exit(2, str(exc) + "\n")
        # No auto-sudo and no auto-upload. Run on a dedicated worker with daemon access.
        return subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
