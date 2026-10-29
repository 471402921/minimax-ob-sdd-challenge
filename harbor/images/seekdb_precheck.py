#!/usr/bin/env python3
"""Offline seekdb compatibility precheck for the prewarm image.

The check installs the pinned wheels from the in-image cache into a throwaway directory
(`pip install --target`), opens a temporary database under /tmp with `aopen`, runs one
trivial statement and closes the handle again.

It never installs into the system interpreter, never changes the dependency versions of
the reviewed submission and never contacts a package index: pip runs with `--no-index`,
`PIP_CONFIG_FILE=/dev/null` and a private HOME so that no host pip configuration (which
could carry an index URL with credentials) is read.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_WHEELS = Path("/opt/wheels/seekdb")
INSTALL_TIMEOUT = 300


def wheel_requirements(wheels: Path) -> list[str]:
    """Return the pinned requirements for the wheels present in the cache."""
    requirements: list[str] = []
    for pattern in ("pylibseekdb-*.whl", "pymysql-*.whl"):
        matches = sorted(wheels.glob(pattern))
        if not matches:
            raise RuntimeError(f"wheel cache does not contain {pattern}")
        name, version = matches[0].name.split("-")[0], matches[0].name.split("-")[1]
        requirements.append(f"{name}=={version}")
    return requirements


def install_into(wheels: Path, target: Path, home: Path, timeout: int = INSTALL_TIMEOUT) -> None:
    """Install the cached wheels into a throwaway directory, offline."""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "PIP_RETRIES": "0",
        "PIP_TIMEOUT": "15",
    }
    command = [sys.executable, "-m", "pip", "install", "--no-index", "--no-cache-dir",
               "--disable-pip-version-check", "--find-links", str(wheels), "--target", str(target)]
    command.extend(wheel_requirements(wheels))
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError("offline wheel install failed: " + " | ".join(tail))


async def open_and_close(module, database_dir: Path) -> dict:
    """Open a temporary database with aopen, run one statement and close everything."""
    instance = await module.aopen(db_dir=str(database_dir))
    try:
        # `test` is the default database of the local seekdb runtime.
        connection = instance.connect(database="test", autocommit=True)
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT 1")
                cursor.fetchall()
            finally:
                cursor.close()
        finally:
            connection.close()
    finally:
        instance.close()
    return {"opened": True, "closed": True}


def precheck(wheels: Path = DEFAULT_WHEELS, root: Path = Path("/tmp")) -> dict:
    """Run the whole precheck and return a small, secret-free report."""
    workspace = Path(tempfile.mkdtemp(prefix="seekdb-precheck-", dir=str(root)))
    workspace.chmod(0o700)
    target = workspace / "site-packages"
    try:
        home = workspace / "home"
        home.mkdir(parents=True, exist_ok=True)
        install_into(wheels, target, home)
        sys.path.insert(0, str(target))
        import pylibseekdb  # noqa: PLC0415 - imported after the throwaway install
        result = asyncio.run(open_and_close(pylibseekdb, workspace / "precheck.db"))
        result["wheels"] = [path.name for path in sorted(wheels.glob("*.whl"))]
        return result
    finally:
        sys.path[:] = [entry for entry in sys.path if entry != str(target)]
        shutil.rmtree(workspace, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline seekdb compatibility precheck.")
    parser.add_argument("--wheels", type=Path, default=DEFAULT_WHEELS)
    parser.add_argument("--root", type=Path, default=Path("/tmp"))
    args = parser.parse_args(argv)
    try:
        report = precheck(wheels=args.wheels, root=args.root)
    except RuntimeError as error:
        print(f"seekdb precheck failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - the failure detail is summarised, never dumped
        print(f"seekdb precheck failed: {type(error).__name__}: {str(error)[:200]}", file=sys.stderr)
        return 1
    print(f"seekdb precheck ok: wheels={','.join(report['wheels'])} opened_and_closed=yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
