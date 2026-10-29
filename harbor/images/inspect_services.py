#!/usr/bin/env python3
"""Print a redacted summary of the service description written by the bootstrap.

The bearer token is never printed; only its presence and length are reported so that a
health check can prove the file is complete without disclosing the secret.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

DEFAULT_PATH = Path("/tmp/review-services.json")


def summarize(path: Path) -> dict:
    """Return the non-secret facts about the service description file."""
    mode = stat.S_IMODE(os.stat(str(path)).st_mode)
    payload = json.loads(path.read_text("utf-8"))
    token = payload.get("token", "")
    return {
        "path": str(path),
        "mode": oct(mode),
        "url": payload.get("url", ""),
        "scope_id": payload.get("scope_id", ""),
        "access_mode": payload.get("access_mode", ""),
        "database": payload.get("database", ""),
        "token_present": bool(token),
        "token_length": len(token) if isinstance(token, str) else 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise the prewarm service description.")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)
    try:
        report = summarize(args.path)
    except (OSError, ValueError) as error:
        print(f"inspect_services: cannot read the service description: {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
