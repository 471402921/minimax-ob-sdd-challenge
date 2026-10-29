"""Validate report structure and evidence hashes, NOT the truth of a verdict."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from pathlib import Path, PurePosixPath

CHECKS = {"runtime", "minimax", "context", "database", "state", "code_change"}
STATUSES = {"verified", "failed", "blocked", "not_run", "uncertain"}
BASES = {"rerun", "static", "supplied_record", "not_run"}
MAX_REPORT = 1024 * 1024
MAX_EVIDENCE = 20 * 1024 * 1024


class InvalidReport(ValueError):
    pass


def regular_file(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative)
    if parts.is_absolute() or ".." in parts.parts or "\\" in relative:
        raise InvalidReport("unsafe artifact path")
    current = root
    for part in parts.parts:
        current /= part
        if current.is_symlink():
            raise InvalidReport("symlink artifact is not accepted")
    if not stat.S_ISREG(current.stat().st_mode):
        raise InvalidReport("artifact is not a regular file")
    if not current.resolve().is_relative_to(root.resolve()):
        raise InvalidReport("artifact escapes output directory")
    return current


def load_json(path: Path, max_bytes: int = MAX_REPORT):
    if path.stat().st_size > max_bytes:
        raise InvalidReport("JSON artifact too large")
    return json.loads(path.read_text(encoding="utf-8"))


def validate(output: Path, expected: dict) -> dict:
    report = load_json(regular_file(output, "report.json"))
    if not isinstance(report, dict) or type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise InvalidReport("expected report schema_version=1")
    for key in ("task_id", "commit_id", "scope"):
        if type(report.get(key)) is not type(expected[key]) or report.get(key) != expected[key]:
            raise InvalidReport(f"{key} does not match fixed submission")
    if report.get("recommendation") not in {"pass", "fail", "review", "error"}:
        raise InvalidReport("unknown recommendation")
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        raise InvalidReport("summary is required")
    changes = report.get("environment_adjustments")
    if not isinstance(changes, list) or any(not isinstance(x, str) for x in changes):
        raise InvalidReport("environment_adjustments must be an array of strings")
    checks = report.get("checks")
    if not isinstance(checks, dict) or set(checks) != CHECKS:
        raise InvalidReport("exactly six named evidence dimensions required")
    checked_files = set()
    for name, check in checks.items():
        if not isinstance(check, dict):
            raise InvalidReport(f"{name}: invalid check")
        if check.get("status") not in STATUSES or check.get("basis") not in BASES:
            raise InvalidReport(f"{name}: invalid status or basis")
        if not isinstance(check.get("detail"), str) or not check["detail"].strip():
            raise InvalidReport(f"{name}: detail required")
        evidence = check.get("evidence")
        if not isinstance(evidence, list):
            raise InvalidReport(f"{name}: evidence array required")
        if check["status"] in {"verified", "failed"}:
            if not evidence or check["basis"] == "not_run":
                raise InvalidReport(f"{name}: observed result requires evidence and execution basis")
        for item in evidence:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise InvalidReport("evidence reference shape is invalid")
            rel, digest = item["path"], item["sha256"]
            if not isinstance(rel, str) or not rel.startswith("evidence/"):
                raise InvalidReport("evidence must be under evidence/")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise InvalidReport("invalid evidence SHA-256")
            file = regular_file(output, rel)
            if file.stat().st_size > MAX_EVIDENCE:
                raise InvalidReport("evidence file too large")
            if hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                raise InvalidReport("evidence hash mismatch")
            checked_files.add(rel)
    if report["recommendation"] == "pass" and any(c["status"] != "verified" for c in checks.values()):
        raise InvalidReport("pass recommendation has unverified checks")
    return {"report_valid": True, "evidence_files": len(checked_files),
            "recommendation": report["recommendation"], "auto_callback": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    args = parser.parse_args()
    args.logs.mkdir(parents=True, exist_ok=True)
    try:
        result = validate(args.output, load_json(args.expected))
    except (InvalidReport, OSError, ValueError, TypeError, KeyError):
        # Do not echo candidate data or exception strings (may contain secrets).
        result = {"report_valid": False, "error": "invalid report, metadata or evidence", "auto_callback": False}
    reward = int(result["report_valid"])
    (args.logs / "validation.json").write_text(json.dumps(result, indent=2))
    (args.logs / "reward.json").write_text(json.dumps({"reward": reward, "report_valid": reward}))
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
