#!/usr/bin/env python3
"""Trusted launcher for the Pi agent inside the prewarmed review container.

The launcher reads the service description written by the bootstrap
(`/tmp/review-services.json`, mode 0600) and starts the real Pi binary with the
PowerContext connection exported into the process environment:

  POWERCONTEXT_URL        service base URL
  POWERCONTEXT_TOKEN      service bearer token
  POWERCONTEXT_SCOPE_ID   server-side Scope id

Rules:
  * the token is never placed in argv, never printed and never written to a log;
  * when the service description is missing or unusable (a light task that does not
    start the backend) Pi is started unchanged, so the same image serves both
    light and backend tasks;
  * Pi is replaced with `exec`, so signals, exit code and stdio behave exactly as
    if Pi had been started directly.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SERVICES_FILE = Path(os.environ.get("POWERCONTEXT_SERVICES_FILE", "/tmp/review-services.json"))
ORIGINAL_CANDIDATES = (
    "/opt/review/pi-original",
    "/opt/review/bin/pi-original",
)
ENV_URL = "POWERCONTEXT_URL"
ENV_TOKEN = "POWERCONTEXT_TOKEN"
ENV_SCOPE_ID = "POWERCONTEXT_SCOPE_ID"


def load_services(path: Path = SERVICES_FILE) -> dict:
    """Return the service description, or an empty mapping when it is unusable."""
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return {}  # light task: no backend was started
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    url = payload.get("url")
    token = payload.get("token")
    scope_id = payload.get("scope_id")
    if not all(isinstance(value, str) and value for value in (url, token, scope_id)):
        return {}
    return {"url": url, "token": token, "scope_id": scope_id}


def build_environment(base: dict, services: dict) -> dict:
    """Return the environment Pi is started with; the token stays in the environment only."""
    environment = dict(base)
    if services:
        environment[ENV_URL] = services["url"]
        environment[ENV_TOKEN] = services["token"]
        environment[ENV_SCOPE_ID] = services["scope_id"]
    return environment


def resolve_original() -> str | None:
    """Return the absolute path of the real Pi entry point."""
    override = os.environ.get("PI_ORIGINAL_PATH", "").strip()
    if override and Path(override).exists():
        return override
    for candidate in ORIGINAL_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    original = resolve_original()
    if original is None:
        print("pi-with-services: the original Pi entry point was not found", file=sys.stderr)
        return 1
    services = load_services()
    if not services and SERVICES_FILE.exists():
        print("pi-with-services: the service description is unusable; starting Pi without PowerContext",
              file=sys.stderr)
    environment = build_environment(dict(os.environ), services)
    try:
        os.execve(original, [original] + arguments, environment)
    except OSError as error:
        print(f"pi-with-services: cannot start Pi: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0  # unreachable: execve replaces the process


if __name__ == "__main__":
    raise SystemExit(main())
