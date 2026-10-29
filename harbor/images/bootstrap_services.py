#!/usr/bin/env python3
"""Start the pre-provisioned PowerContext service inside the review container.

The script is intentionally dependency free (Python standard library only) so that it can
run before anything else is installed in the image:

  1. generate a single-use bearer token with `secrets`;
  2. start the PowerContext server on loopback with a throwaway SQLite workspace;
  3. wait for `/health/ready` to answer 200;
  4. provision one server-side Scope and read the Scope id the server assigned;
  5. write `/tmp/review-services.json` with mode 0600.

The service description file has exactly these keys (contract with the agent adapter):

  url        base URL of the loopback service, e.g. http://127.0.0.1:18000
  token      single-use bearer token for that service (never printed)
  scope_id   Scope id assigned by the server
  plus non-secret metadata: schema_version, service, access_mode, database,
  database_url, workspace, server_log, pid, created_at.

Secret handling rules enforced here:
  * the token is passed to the server through the child process environment only, never
    through argv and never on a command line;
  * the token is never printed, never written to a world readable file and never copied
    into a log line; anything that is printed is passed through `redact` first;
  * the server log is scanned for the token before the script reports success.

The resulting JSON file is meant to be read by the agent adapter that starts Pi later;
it must not be copied into a report or an evidence bundle.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_OUTPUT = "/tmp/review-services.json"
DEFAULT_PC_BIN = "/opt/pc-venv/bin/powercontext"
HOST = "127.0.0.1"
PORT = 18000
READY_TIMEOUT = 180.0
REQUEST_TIMEOUT = 15.0
SCOPE_TITLE = "Harbor prewarm review scope"
SCOPE_SUMMARY = "Server-side Scope provisioned by the Harbor prewarm image bootstrap."
SCOPE_IDEMPOTENCY_KEY = "harbor-prewarm-scope"
SERVICES_FILE_MODE = 0o600


class BootstrapError(RuntimeError):
    """Raised when the service cannot be started, verified or described."""


def make_token() -> str:
    """Return a single-use bearer token for this container run."""
    return secrets.token_urlsafe(32)


def redact(text: str, *secrets_to_hide: str) -> str:
    """Replace every secret occurrence so that text is safe to print."""
    for secret in secrets_to_hide:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def contains_secret(text: str, *secrets_to_hide: str) -> bool:
    """Return True when any secret appears in the text."""
    return any(secret and secret in text for secret in secrets_to_hide)


def server_environment(token: str, *, workspace: Path, database_url: str, home: Path, tmpdir: Path) -> dict[str, str]:
    """Build the explicit environment of the PowerContext server process."""
    return {
        "PATH": "/opt/pc-venv/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "POWERCONTEXT_SERVER_HTTP_HOST": HOST,
        "POWERCONTEXT_SERVER_HTTP_PORT": str(PORT),
        "POWERCONTEXT_SERVER_WORKSPACE": str(workspace),
        "POWERCONTEXT_SERVER_DATABASE_KIND": "sqlite",
        "POWERCONTEXT_SERVER_DATABASE_URL": database_url,
        "POWERCONTEXT_SERVER_ACCESS_MODE": "enforced",
        "POWERCONTEXT_SERVER_AUTH_TOKEN": token,
        "POWERCONTEXT_SERVER_RUNTIME_ARTIFACT_PROCESSING_ROLE": "all",
        "POWERCONTEXT_SERVER_LOGGING_LEVEL": "INFO",
    }


def _request(request: urllib.request.Request, timeout: float):
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", "replace")


def wait_ready(base_url: str, timeout: float = READY_TIMEOUT, poll: float = 1.0) -> None:
    """Block until the server answers 200 on /health/ready, or fail."""
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        try:
            status, _ = _request(urllib.request.Request(f"{base_url}/health/ready"), timeout=5.0)
            if status == 200:
                return
            last = f"HTTP {status}"
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}"
        except (urllib.error.URLError, socket.timeout, OSError) as error:
            last = type(error).__name__
        time.sleep(poll)
    raise BootstrapError(f"service did not become ready within {timeout:.0f}s (last: {last})")


def create_scope(base_url: str, token: str, payload: dict, timeout: float = REQUEST_TIMEOUT) -> str:
    """Create one server-side Scope and return the Scope id assigned by the server."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/v1/scopes",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        status, text = _request(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        raise BootstrapError(f"scope provisioning failed with HTTP {error.code}") from None
    except (urllib.error.URLError, socket.timeout, OSError) as error:
        raise BootstrapError(f"scope provisioning failed: {type(error).__name__}") from None
    if status not in (200, 201):
        raise BootstrapError(f"scope provisioning failed with HTTP {status}")
    try:
        scope_id = json.loads(text)["scope_id"]
    except (ValueError, KeyError, TypeError):
        raise BootstrapError("scope provisioning returned no scope_id") from None
    if not isinstance(scope_id, str) or not scope_id:
        raise BootstrapError("scope provisioning returned an empty scope_id")
    return scope_id


def list_scope_ids(base_url: str, token: str, timeout: float = REQUEST_TIMEOUT) -> list[str]:
    """Read back the Scope list; used to confirm the server really stored the Scope."""
    request = urllib.request.Request(f"{base_url}/v1/scopes", headers={"Authorization": f"Bearer {token}"})
    try:
        status, text = _request(request, timeout=timeout)
    except (urllib.error.HTTPError, urllib.error.URLError, socket.timeout, OSError):
        return []
    if status != 200:
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    return [item.get("scope_id", "") for item in items if isinstance(item, dict)]


def write_services_file(path: Path, payload: dict) -> None:
    """Write the service description with mode 0600 and verify the mode afterwards."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SERVICES_FILE_MODE)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)
    os.chmod(str(path), SERVICES_FILE_MODE)
    mode = stat.S_IMODE(os.stat(str(path)).st_mode)
    if mode != SERVICES_FILE_MODE:
        raise BootstrapError(f"services file mode is {oct(mode)}, expected {oct(SERVICES_FILE_MODE)}")


def start_server(pc_binary: str, env: dict[str, str], *, workspace: Path, log_path: Path) -> subprocess.Popen:
    """Start the PowerContext server in its own session; the token stays in the environment."""
    if not Path(pc_binary).is_file():
        raise BootstrapError(f"powercontext binary not found: {pc_binary}")
    handle = open(log_path, "wb")
    try:
        return subprocess.Popen(
            [pc_binary, "server", "run", "--host", HOST, "--port", str(PORT), "--no-env-file"],
            env=env,
            cwd=str(workspace),
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        handle.close()


def log_tail(log_path: Path, limit: int = 20) -> str:
    """Return the last lines of the server log, with secrets already redacted."""
    try:
        text = log_path.read_text("utf-8", "replace")
    except OSError:
        return ""
    lines = text.splitlines()[-limit:]
    return "\n".join(lines)


def summary_line(description: dict) -> str:
    """Return the one-line, secret-free summary of a started service."""
    return (f"powercontext ready: url={description['url']} scope_id={description['scope_id']} "
            f"access_mode={description['access_mode']} database={description['database']}")


def stop_server(process: subprocess.Popen | None, timeout: float = 10.0) -> None:
    """Terminate the server and everything it started; used when the bootstrap fails."""
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


def bootstrap(*, pc_binary: str = DEFAULT_PC_BIN, output: Path = Path(DEFAULT_OUTPUT),
              root: Path = Path("/tmp"), title: str = SCOPE_TITLE, summary: str = SCOPE_SUMMARY,
              idempotency_key: str = SCOPE_IDEMPOTENCY_KEY, ready_timeout: float = READY_TIMEOUT,
              keep_workspace: bool = False) -> dict:
    """Start the service, provision the Scope and describe it. Raises BootstrapError on failure."""
    token = make_token()
    workspace_root = Path(tempfile.mkdtemp(prefix="pc-prewarm-", dir=str(root)))
    workspace_root.chmod(0o700)
    workspace = workspace_root / "workspace"
    home = workspace_root / "home"
    database_dir = workspace_root / "db"
    for directory in (workspace, home, database_dir):
        directory.mkdir(parents=True, exist_ok=True)
    log_path = workspace_root / "powercontext-server.log"
    database_url = f"sqlite+aiosqlite:///{database_dir / 'pc.db'}"
    base_url = f"http://{HOST}:{PORT}"
    process = None
    started = False
    try:
        process = start_server(pc_binary, server_environment(token, workspace=workspace,
                                                             database_url=database_url, home=home,
                                                             tmpdir=workspace_root),
                               workspace=workspace, log_path=log_path)
        started = True
        wait_ready(base_url, timeout=ready_timeout)
        scope_id = create_scope(base_url, token, {"title": title, "summary": summary,
                                                 "idempotency_key": idempotency_key})
        known = list_scope_ids(base_url, token)
        if known and scope_id not in known:
            raise BootstrapError("server did not report the provisioned scope")
        log_text = log_path.read_text("utf-8", "replace") if log_path.exists() else ""
        if contains_secret(log_text, token):
            raise BootstrapError("server log contains the bearer token; refusing to publish the service")
        description = {
            "schema_version": 1,
            "service": "powercontext",
            "url": base_url,
            "token": token,
            "scope_id": scope_id,
            "access_mode": "enforced",
            "database": "sqlite",
            "database_url": database_url,
            "workspace": str(workspace),
            "server_log": str(log_path),
            "pid": process.pid,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        write_services_file(output, description)
        started = False  # the service stays up on purpose
        return description
    finally:
        if started:
            # A failed bootstrap must not leave a half started service behind.
            stop_server(process)
        if not keep_workspace:
            # The server keeps running in its own session; only the scratch workspace of a
            # failed start is removed here. A successful run keeps its files for the agent.
            if process is None or process.poll() is not None:
                shutil.rmtree(workspace_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start and describe the pre-provisioned PowerContext service.")
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT))
    parser.add_argument("--pc-binary", default=DEFAULT_PC_BIN)
    parser.add_argument("--root", type=Path, default=Path("/tmp"))
    parser.add_argument("--title", default=SCOPE_TITLE)
    parser.add_argument("--summary", default=SCOPE_SUMMARY)
    parser.add_argument("--idempotency-key", default=SCOPE_IDEMPOTENCY_KEY)
    parser.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT)
    parser.add_argument("--keep-workspace", action="store_true")
    args = parser.parse_args(argv)

    def _terminate(signum, frame):  # noqa: ANN001 - signal handler signature
        raise KeyboardInterrupt(f"signal {signum}")

    for received in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(received, _terminate)
        except (ValueError, OSError):
            pass
    try:
        description = bootstrap(pc_binary=args.pc_binary, output=args.output, root=args.root,
                                title=args.title, summary=args.summary,
                                idempotency_key=args.idempotency_key,
                                ready_timeout=args.ready_timeout, keep_workspace=args.keep_workspace)
    except BootstrapError as error:
        print(f"prewarm bootstrap failed: {redact(str(error))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("prewarm bootstrap aborted", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"prewarm bootstrap failed: {type(error).__name__}", file=sys.stderr)
        return 1
    # Only non-secret facts are printed; the bearer token stays in the 0600 file.
    print(summary_line(description))
    print(f"services written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
