#!/usr/bin/env python3
"""Verify the fixed input hash manifest of the prewarm image.

The manifest lists the sha256 of every file that produced the image. This script is run
inside the image at build time so that a tampered build context is rejected instead of
being baked into an image.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

CHUNK = 1 << 20


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_check(value: str) -> tuple[str, str]:
    """Parse a `manifest-prefix=container-path` argument."""
    prefix, separator, target = value.partition("=")
    if not separator or not prefix or not target:
        raise argparse.ArgumentTypeError("expected <manifest-prefix>=<container-path>")
    return prefix, target


def verify(manifest: dict, prefix: str, target: Path) -> list[str]:
    """Return the list of mismatches between the manifest and the files under target."""
    problems: list[str] = []
    checked = 0
    for name in sorted(manifest.get("files", {})):
        if name == prefix:
            relative = ""
        elif name.startswith(prefix + "/"):
            relative = name[len(prefix) + 1:]
        else:
            continue
        path = target if not relative else target / relative
        expected = manifest["files"][name].get("sha256", "")
        if not path.is_file():
            problems.append(f"{name}: missing in the image")
            continue
        actual = sha256(path)
        if actual != expected:
            problems.append(f"{name}: sha256 mismatch")
        checked += 1
    if not checked:
        problems.append(f"{prefix}: no manifest entry matched")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the prewarm input hash manifest.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--check", action="append", default=[], type=parse_check,
                        metavar="PREFIX=PATH")
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text("utf-8"))
    except (OSError, ValueError):
        print("verify_inputs: the manifest is missing or unreadable", file=sys.stderr)
        return 1
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        print("verify_inputs: the manifest has no file table", file=sys.stderr)
        return 1
    failures: list[str] = []
    total = 0
    for prefix, target in args.check:
        problems = verify(manifest, prefix, Path(target))
        failures.extend(problems)
        total += sum(1 for name in manifest["files"] if name == prefix or name.startswith(prefix + "/"))
    if failures:
        for problem in failures:
            print(f"verify_inputs: {problem}", file=sys.stderr)
        return 1
    print(f"verify_inputs: {total} input files match the manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
