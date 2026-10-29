#!/usr/bin/env python3
"""Build the wheel-matching review image from a verified offline wheel set.

The older `prewarm.py` builds the trial-local PowerContext service from a
separate repository snapshot. That produced a 1.2.0 service for submissions
pinning a 1.4.0 client, and left the submission's own `pip install` downloading
the whole dependency closure at review time (~12 minutes of the 30-minute
budget). This module instead stages ONE resolved wheel set that serves both the
service venv and the submission's own venv.

Nothing here uses sudo, claims tasks, calls a model, reads credentials or copies
contestant source into the image. Build and verify are explicit switches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
IMAGE_DIR = HERE / "images" / "wheelhouse"
SCRIPT_DIR = HERE / "images"
DEFAULT_WHEELS = ROOT / "workdir" / "wheelhouse-pc140"
DEFAULT_CONTEXT = ROOT / "workdir" / "harbor-images-wheelhouse"
DEFAULT_TAG = "harbor/review-wheels:local"
SERVICE_REQUIREMENT = "powercontext[cli,server,seekdb]==1.4.0"
REQUIREMENTS_NAME = "requirements.service.txt"
SOURCES = ("Dockerfile", "bootstrap_services.py", "pi-with-services.py",
           "verify_inputs.py", "inspect_services.py")
SECRET = re.compile(rb"(?:github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9]{30,}"
                    rb"|sk-[A-Za-z0-9_-]{20,}|https?://[^\s/@:]+:[^\s/@]+@)")
MAX_WHEELS = 2000
MAX_WHEEL_BYTES = 256 << 20
MAX_TOTAL_BYTES = 1 << 30


class PrewarmError(RuntimeError):
    """Refused or failed; the message never contains file contents."""


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/tmp"), "DOCKER_CONFIG": os.environ.get("DOCKER_CONFIG", "")}
    env = {k: v for k, v in env.items() if v}
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env)


def wheel_metadata(path: Path) -> tuple[str, str]:
    """Return (name, version) from a wheel's dist-info METADATA without extracting it."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist()
                     if n.endswith(".dist-info/METADATA") and n.count("/") == 1]
            if len(names) != 1:
                raise PrewarmError("wheel without exactly one dist-info METADATA")
            text = archive.read(names[0]).decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError, KeyError):
        raise PrewarmError("unreadable wheel") from None
    name = re.search(r"^Name:\s*(\S+)\s*$", text, re.M)
    version = re.search(r"^Version:\s*(\S+)\s*$", text, re.M)
    if not name or not version:
        raise PrewarmError("wheel metadata lacks Name/Version")
    return name.group(1), version.group(1)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def collect_wheels(source: Path) -> dict[str, tuple[str, int]]:
    """Hash every wheel, refusing symlinks/oversize sets. Returns name -> (sha256, size)."""
    if not source.is_dir():
        raise PrewarmError("wheel directory missing")
    files: dict[str, tuple[str, int]] = {}
    total = 0
    for entry in sorted(source.iterdir()):
        if entry.name == REQUIREMENTS_NAME:
            continue
        if entry.is_symlink() or not entry.is_file() or not entry.name.endswith(".whl"):
            raise PrewarmError("wheel directory must contain only regular .whl files")
        size = entry.stat().st_size
        if size > MAX_WHEEL_BYTES or len(files) >= MAX_WHEELS or total + size > MAX_TOTAL_BYTES:
            raise PrewarmError("wheel set exceeds the accepted size")
        files[entry.name] = (hashlib.sha256(entry.read_bytes()).hexdigest(), size)
        total += size
    if not files:
        raise PrewarmError("wheel directory is empty")
    names = {}
    for name in files:
        meta = wheel_metadata(source / name)
        names[canonical(meta[0])] = meta[1]
    if names.get("powercontext") != "1.4.0":
        raise PrewarmError("wheel set does not contain powercontext 1.4.0")
    return files


def _copy_sources(destination: Path) -> None:
    scripts = destination / "scripts"
    scripts.mkdir()
    for name in SOURCES:
        source = IMAGE_DIR / name if (IMAGE_DIR / name).is_file() else SCRIPT_DIR / name
        if not source.is_file():
            raise PrewarmError("missing image source")
        data = source.read_bytes()
        if SECRET.search(data):
            raise PrewarmError("possible credential in an image source file")
        (scripts / name).write_bytes(data)


def prepare(context: Path = DEFAULT_CONTEXT, wheels: Path = DEFAULT_WHEELS,
            service_requirement: str = SERVICE_REQUIREMENT) -> Path:
    """Stage an immutable build context. Never overwrites an existing context."""
    context, wheels = Path(context), Path(wheels)
    if context.exists():
        raise PrewarmError("build context already exists; remove it deliberately to rebuild")
    if not re.fullmatch(r"powercontext\[[a-z,]+\]==\d+\.\d+\.\d+", service_requirement):
        raise PrewarmError("service requirement must be an exact powercontext pin with extras")
    wheel_files = collect_wheels(wheels)
    temporary = context.with_name(f".{context.name}-{os.getpid()}")
    temporary.mkdir(parents=True)
    try:
        (temporary / "wheels" / "player").mkdir(parents=True)
        for name, (digest, _size) in wheel_files.items():
            copied = temporary / "wheels" / "player" / name
            shutil.copyfile(wheels / name, copied)
            if hashlib.sha256(copied.read_bytes()).hexdigest() != digest:
                raise PrewarmError("wheel changed while staging")
        (temporary / "wheels" / "player" / REQUIREMENTS_NAME).write_text(
            service_requirement + "\n", encoding="utf-8")
        _copy_sources(temporary)
        shutil.copyfile(IMAGE_DIR / "Dockerfile", temporary / "Dockerfile")
        manifest = {"files": {}, "sources": {"wheels": {"count": len(wheel_files)}}}
        for path in sorted(temporary.rglob("*")):
            if path.is_file():
                manifest["files"][str(path.relative_to(temporary))] = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        (temporary / "inputs.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        temporary.rename(context)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return context


def build_command(context: Path, tag: str) -> list[str]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,120}:[A-Za-z0-9._-]{1,60}", tag):
        raise PrewarmError("unsafe image tag")
    if not (context / "inputs.json").is_file():
        raise PrewarmError("prepared context required")
    return ["docker", "build", "--tag", tag, "--file", str(context / "Dockerfile"), str(context)]


def build(context: Path = DEFAULT_CONTEXT, tag: str = DEFAULT_TAG, timeout: int = 3600,
          dry_run: bool = False) -> dict:
    command = build_command(Path(context), tag)
    if dry_run:
        return {"command": command, "built": False}
    result = _run(command, timeout)
    if result.returncode:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-4:]
        raise PrewarmError("docker build failed: " + " | ".join(tail))
    inspect = _run(["docker", "image", "inspect", tag, "--format", "{{.Id}}"], 60)
    image_id = inspect.stdout.strip()
    if inspect.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise PrewarmError("cannot resolve the built image ID")
    return {"built": True, "tag": tag, "image_id": image_id}


def verify_command(requirements: Path | None = None, container: str | None = None) -> list[str]:
    """Offline install check; pip must resolve each wheel from the image alone."""
    name = container or f"harbor-wheels-verify-{os.getpid()}"
    target = "/req.txt" if requirements is not None else "/opt/wheels/player/" + REQUIREMENTS_NAME
    script = (
        "set -e; cd /tmp; start=$(date +%s); python3 -m venv .venv; "
        ".venv/bin/python -m pip install --no-index --disable-pip-version-check "
        f"--find-links /opt/wheels/player -r {target} >/tmp/install.log 2>&1; "
        "end=$(date +%s); .venv/bin/python -m pip check; "
        ".venv/bin/python -c 'import powercontext, pylibseekdb, pymysql, yaml; print(\"imports_ok\")'; "
        ".venv/bin/python -c \"import importlib.metadata as m; print('powercontext', m.version('powercontext'))\"; "
        "echo \"offline_install_seconds=$((end-start))\""
    )
    command = ["docker", "run", "--rm", "--name", name, "--network", "none", "--user", "10001:10001",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512",
               "--memory", "2g"]
    if requirements is not None:
        command += ["-v", f"{Path(requirements).resolve()}:/req.txt:ro"]
    return command + [DEFAULT_TAG, "sh", "-c", script]


def verify(requirements: Path | None = None, timeout: int = 600, dry_run: bool = False) -> dict:
    """Prove the wheel set installs offline. Exit-0 only means this pinned set works."""
    command = verify_command(requirements)
    if dry_run:
        return {"command": command, "verified": False}
    result = _run(command, timeout)
    output = f"{result.stdout or ''}{result.stderr or ''}".strip()
    if result.returncode:
        raise PrewarmError("offline install failed: " + " | ".join(output.splitlines()[-4:]))
    if SECRET.search(output.encode()):
        raise PrewarmError("offline check printed credential-shaped material")
    return {"verified": True, "output": output.splitlines()[-5:]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    parser.add_argument("--wheels", type=Path, default=DEFAULT_WHEELS)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--requirements", type=Path,
                        help="submission requirements.txt to prove offline (default: service pin)")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not (args.prepare or args.build or args.verify):
        parser.error("choose at least one of --prepare/--build/--verify")
    try:
        if args.prepare:
            print(json.dumps({"context": str(prepare(args.context, args.wheels))}))
        if args.build:
            print(json.dumps(build(args.context, args.tag, args.timeout, args.dry_run)))
        if args.verify:
            print(json.dumps(verify(args.requirements, min(args.timeout, 900), args.dry_run)))
    except (PrewarmError, OSError, subprocess.SubprocessError) as exc:
        print(f"prewarm refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
