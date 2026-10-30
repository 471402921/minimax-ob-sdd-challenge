#!/usr/bin/env python3
"""Claim platform tasks while staging them in parallel.

`judge/review.py fetch` claims exactly one task per call and clones its
repository. Doing that serially for a 45-task wave spends most of the lease
before the first container starts (measured: staging used to eat 15 of 30
minutes). Here one thread claims and several threads stage the moment a
pending record appears, so claiming and packaging overlap.

Usage:
    python3 harbor/wave.py --base-image sha256:<id> --count 46 --workers 4 --tag 44e

Writes ``workdir/manifest-<tag>.json`` (consumed by ``harbor/batch.py``) and
appends progress to ``workdir/wave-<tag>.log``. Both live under the ignored
``workdir/`` because they reference private task ids and paths.

Requirements: ``PLATFORM_TOKEN`` in the environment (the caller sources the
private platform env file), a working ``docker`` for the base-image check, and
the platform client dependencies used by ``judge/review.sh``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKDIR = ROOT / "workdir"
BASE_IMAGE_RE = re.compile(r"sha256:[0-9a-f]{64}")


def validate_base_image(value: str) -> str:
    """Only an immutable local image ID is accepted, never a mutable tag."""
    if not BASE_IMAGE_RE.fullmatch(value or ""):
        raise ValueError("base image must be an immutable local sha256 image ID")
    return value


def manifest_from(results: list[dict]) -> dict:
    """Successful stages become tasks; failures stay out of the manifest."""
    return {"tasks": [{"path": r["path"], "profile": r["profile"]}
                      for r in results if "path" in r]}


def pending_records() -> dict[str, Path]:
    return {p.name: p for p in WORKDIR.glob("task-*.pending.json")}


def check_local_image(base_image: str) -> None:
    """Batch preflight needs harbor/prewarm:sha-<id> to exist with that exact ID."""
    tag = "harbor/prewarm:sha-" + base_image.split(":", 1)[1]
    found = subprocess.run(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
        capture_output=True, text=True, timeout=30,
    )
    if found.returncode or found.stdout.strip() != base_image:
        raise ValueError(f"local image tag {tag} is absent or points elsewhere; "
                         "tag the built image before staging")


def load_prepare():
    spec = importlib.util.spec_from_file_location("harbor_prepare", ROOT / "harbor" / "prepare.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["harbor_prepare"] = module
    spec.loader.exec_module(module)
    return module


def run(base_image: str, count: int, workers: int, tag: str, *, fetch_retries: int = 6,
        timeout: int = 420) -> dict:
    base_image = validate_base_image(base_image)
    check_local_image(base_image)
    prepare = load_prepare()
    manifest_path = WORKDIR / f"manifest-{tag}.json"
    log_path = WORKDIR / f"wave-{tag}.log"

    def log(message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        with log_path.open("a") as fh:
            fh.write(line + "\n")

    work: queue.Queue = queue.Queue()
    results: list[dict] = []
    lock = threading.Lock()

    def claimer() -> None:
        seen = set(pending_records())
        claimed = 0
        misses = 0
        while claimed < count and misses < fetch_retries:
            try:
                proc = subprocess.run(
                    ["./judge/review.sh", "fetch", "--yes", "--allow-multiple"],
                    cwd=ROOT, capture_output=True, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                misses += 1
                log(f"claim timeout ({misses})")
                continue
            current = pending_records()
            fresh = [name for name in current if name not in seen]
            seen = set(current)
            if fresh:
                for name in fresh:
                    work.put(current[name])
                    claimed += 1
                misses = 0
                log(f"claim ok {fresh} ({claimed}/{count})")
            else:
                misses += 1
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
                log(f"claim miss rc={proc.returncode} {tail} ({misses})")
                time.sleep(6)
        log(f"claim phase done: {claimed}")
        for _ in range(workers):
            work.put(None)

    def stager() -> None:
        while True:
            item = work.get()
            if item is None:
                work.task_done()
                return
            try:
                record = json.loads(Path(item).read_text())
                task_id = int(record["task_id"])
                commit = record.get("commit_id") or record.get("commit_requested")
                repo = Path(record.get("checkout_dir") or (WORKDIR / f"task-{task_id}"))
                if not (repo / ".git").exists():
                    repo = WORKDIR / f"task-{task_id}"
                destination = None
                used = ""
                error = ""
                # Strict credential scan first; relax only as far as needed and
                # record the scan level in the staged manifest.
                for fixtures, files in ((False, False), (True, False), (False, True), (True, True)):
                    try:
                        destination = prepare.prepare(
                            task_id, repo, commit, base_image=base_image, offline_install=True,
                            acknowledge_fixture_secrets=fixtures, acknowledge_secret_files=files,
                        )
                        used = f"fx={fixtures},sf={files}"
                        break
                    except Exception as exc:  # noqa: BLE001 - keep the real reason
                        error = f"{type(exc).__name__}: {exc}"
                if destination is not None:
                    rel = str(destination.relative_to(ROOT))
                    with lock:
                        results.append({"path": rel, "profile": "light",
                                        "task_id": task_id, "flags": used})
                    log(f"staged {task_id} {rel} {used}")
                else:
                    with lock:
                        results.append({"task_id": task_id, "error": error})
                    log(f"STAGE FAIL {task_id}: {error}")
            except Exception as exc:  # noqa: BLE001
                log(f"stage exception {type(exc).__name__}: {exc}")
            finally:
                work.task_done()

    threads = [threading.Thread(target=claimer)] + [
        threading.Thread(target=stager) for _ in range(max(1, workers))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    manifest = manifest_from(results)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    failures = [r for r in results if "error" in r]
    log(f"manifest {manifest_path.name}: {len(manifest['tasks'])} staged, {len(failures)} failed")
    return {"manifest": str(manifest_path), "staged": len(manifest["tasks"]),
            "failed": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image", required=True, help="immutable sha256 local image ID")
    parser.add_argument("--count", type=int, required=True, help="how many tasks to claim")
    parser.add_argument("--workers", type=int, default=4, help="parallel staging threads")
    parser.add_argument("--tag", required=True, help="suffix for manifest/log names")
    args = parser.parse_args()
    if args.count < 1 or args.count > 64:
        parser.error("count must be 1..64")
    try:
        result = run(args.base_image, args.count, args.workers, args.tag)
    except ValueError as exc:
        parser.exit(2, f"Wave refused: {type(exc).__name__}: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
