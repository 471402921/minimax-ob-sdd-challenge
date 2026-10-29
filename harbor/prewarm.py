#!/usr/bin/env python3
"""Prewarm the Harbor review image: prepare a private build context, then build it.

The tool is inert unless an explicit switch is given:

  --prepare   export the hash-pinned PowerContext source and the offline wheel caches
              into a private build context under workdir/ and write the input manifest
  --build     verify that context against the manifest and build the image with Docker
  --health    run the built image in an isolated container, start the PowerContext
              service, provision the server-side Scope and verify the result

`--prepare --build` runs the two steps in that order; `--build` alone refuses to run
against a context that does not match its manifest.

Safety rules enforced by this module:
  * the build context never holds contestant code: the PowerContext source is exported
    from Git objects of one explicit commit and limited to a file allowlist, and there is
    no `submission/` or `input/` directory anywhere in the context;
  * every dependency is installed offline from the local wheel cache;
  * the tool never claims a task, never calls a model, never escalates privileges and
    never reads host credentials: Git runs with the global/system configuration disabled
    and every parameter is checked for credential material before it reaches Docker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
IMAGES = HERE / "images"

DEFAULT_CONTEXT = ROOT / "workdir" / "harbor-images"
DEFAULT_PC_REPO = ROOT / "workdir" / "task-47213508-recovery-4js3dm1k" / "repo"
DEFAULT_PC_COMMIT = "fc7a26a2d6b0f47e85bb05b7ce59f3fa1ad16642"
DEFAULT_PC_WHEELS = ROOT / "workdir" / "pc-47213508" / "wheels"
DEFAULT_SEEKDB_WHEELS = ROOT / "workdir" / "review-47213508-wheels"
DEFAULT_IMAGE_TAG = "harbor/prewarm:local"
DEFAULT_PYTHON_IMAGE = "python:3.11.15-slim-bookworm"
DEFAULT_NODE_IMAGE = "node:22.23.2-bookworm-slim"
DEFAULT_DEBIAN_MIRROR = "https://mirrors.ustc.edu.cn"
PC_VERSION = "1.2.0"

# The PowerContext export is an allowlist: nothing else of the commit is copied.
PC_SOURCE_FILES = ("pyproject.toml", "README.md", "LICENSE")
PC_SOURCE_PREFIXES = ("src/",)
SEEKDB_WHEEL_PATTERNS = ("pylibseekdb-*.whl", "pymysql-*.whl")
IMAGE_ASSETS = {
    "Dockerfile": "Dockerfile",
    "requirements.pc.txt": "requirements.pc.txt",
    "bootstrap_services.py": "scripts/bootstrap_services.py",
    "pi-with-services.py": "scripts/pi-with-services.py",
    "seekdb_precheck.py": "scripts/seekdb_precheck.py",
    "verify_inputs.py": "scripts/verify_inputs.py",
    "inspect_services.py": "scripts/inspect_services.py",
}
FORBIDDEN_CONTEXT_NAMES = ("submission", "input")

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILES = 5000

SECRET_PATTERN = re.compile(
    rb"(?:github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{20,}"
    rb"|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    rb"|https?://[^\s/@:]+:[^\s/@]+@)"
)
CREDENTIAL_NAMES = {".env", "auth.json", "id_rsa", "id_ed25519", "credentials", "secrets.json", "token"}
IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?(@sha256:[0-9a-f]{64})?$")


class PrewarmError(RuntimeError):
    """Raised when a prewarm step refuses to continue."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def git(repo: Path, *args: str) -> bytes:
    """Read Git objects without any credential capable configuration."""
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "--literal-pathspecs",
                              "-C", str(repo), *args],
                             env=environment, capture_output=True, timeout=120)
    if result.returncode:
        raise PrewarmError("git object read failed (details suppressed to protect credentials)")
    return result.stdout


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_image_ref(value: str, name: str) -> str:
    """Accept a plain image reference or one pinned to a digest; refuse anything else."""
    if not value or not IMAGE_REF.fullmatch(value):
        raise PrewarmError(f"{name} is not a valid image reference")
    if "://" in value or "@" in value and not value.split("@", 1)[1].startswith("sha256:"):
        raise PrewarmError(f"{name} must not carry credentials")
    return value


def validate_mirror(value: str) -> str:
    """Accept a credential free HTTPS mirror URL."""
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment:
        raise PrewarmError("the Debian mirror must be a credential free HTTPS URL")
    return value


def validate_parameter(value: str, name: str) -> str:
    """Refuse a parameter that carries credential material."""
    if SECRET_PATTERN.search(value.encode("utf-8", "replace")):
        raise PrewarmError(f"{name} must not carry credentials")
    if "://" in value and "@" in value.split("://", 1)[1].split("/", 1)[0]:
        raise PrewarmError(f"{name} must not carry URL user information")
    return value


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------
def export_pc_source(repo: Path, commit: str) -> dict[str, bytes]:
    """Export the allowlisted PowerContext files from the Git objects of one commit."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise PrewarmError("provide the full 40 hex commit SHA of the PowerContext source")
    resolved = git(repo, "rev-parse", "--verify", commit + "^{commit}").decode().strip().lower()
    if resolved != commit.lower():
        raise PrewarmError("the commit does not match the requested SHA")
    entries = git(repo, "ls-tree", "-rz", commit).split(b"\0")
    files: dict[str, bytes] = {}
    total = 0
    for entry in filter(None, entries):
        fields, raw_path = entry.split(b"\t", 1)
        mode, kind, sha = fields.decode().split()
        path = raw_path.decode("utf-8")
        if kind != "blob":
            continue
        if mode not in {"100644", "100755"}:
            raise PrewarmError(f"{path}: symlinks and submodules are not exported")
        if not (path in PC_SOURCE_FILES or path.startswith(PC_SOURCE_PREFIXES)):
            continue
        if any(part in ("..", ".git") for part in Path(path).parts):
            raise PrewarmError(f"{path}: invalid path")
        name = Path(path).name.lower()
        if name in CREDENTIAL_NAMES or name.endswith((".pem", ".key", ".token")):
            raise PrewarmError(f"{path}: credential like file, refused")
        size = int(git(repo, "cat-file", "-s", sha))
        if len(files) >= MAX_SOURCE_FILES or total + size > MAX_SOURCE_BYTES:
            raise PrewarmError("the PowerContext export exceeds the size limit")
        data = git(repo, "cat-file", "blob", sha)
        if SECRET_PATTERN.search(data):
            raise PrewarmError(f"{path}: possible credential in the source, refused")
        files[path] = data
        total += size
    if not files:
        raise PrewarmError("no PowerContext source found at the requested commit")
    return files


def collect_wheels(directory: Path, patterns: tuple[str, ...]) -> dict[str, bytes]:
    """Read the wheel cache; only wheel files are accepted."""
    if not directory.is_dir():
        raise PrewarmError(f"wheel cache not found: {directory.name}")
    wheels: dict[str, bytes] = {}
    for pattern in patterns:
        for path in sorted(directory.glob(pattern)):
            if not path.is_file() or path.suffix != ".whl":
                continue
            wheels[path.name] = path.read_bytes()
    if not wheels:
        raise PrewarmError(f"no wheel matched {patterns} in {directory.name}")
    return wheels


def assert_private_context(context_dir: Path) -> None:
    """The build context must live in the ignored private area of the repository."""
    private_root = (ROOT / "workdir").resolve()
    if not context_dir.is_relative_to(private_root):
        raise PrewarmError("the build context must live under workdir/")
    ignored = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(context_dir)],
                              capture_output=True)
    if ignored.returncode != 0:
        raise PrewarmError("the build context is not covered by .gitignore")


def prepare(context_dir: Path = DEFAULT_CONTEXT, pc_repo: Path = DEFAULT_PC_REPO,
            pc_commit: str = DEFAULT_PC_COMMIT, pc_wheels: Path = DEFAULT_PC_WHEELS,
            seekdb_wheels: Path = DEFAULT_SEEKDB_WHEELS, images_dir: Path = IMAGES) -> dict:
    """Build the private build context and its fixed input hash manifest."""
    context_dir = Path(context_dir).resolve()
    assert_private_context(context_dir)
    files: dict[str, bytes] = {}
    source = export_pc_source(Path(pc_repo), pc_commit)
    for path, data in source.items():
        files[f"pc-source/{path}"] = data
    for name, data in collect_wheels(Path(pc_wheels), ("*.whl",)).items():
        files[f"wheels/pc/{name}"] = data
    for name, data in collect_wheels(Path(seekdb_wheels), SEEKDB_WHEEL_PATTERNS).items():
        files[f"wheels/seekdb/{name}"] = data
    for source_name, target in IMAGE_ASSETS.items():
        asset = Path(images_dir) / source_name
        if not asset.is_file():
            raise PrewarmError(f"missing image asset: {source_name}")
        files[target] = asset.read_bytes()
    for name in FORBIDDEN_CONTEXT_NAMES:
        if any(key == name or key.startswith(name + "/") for key in files):
            raise PrewarmError(f"the build context must not contain {name}/")

    manifest_files = {name: {"sha256": sha256_bytes(data), "size": len(data)}
                      for name, data in sorted(files.items())}
    manifest = {
        "schema_version": 1,
        "generated_by": "harbor/prewarm.py --prepare",
        "policy": {
            "offline_pip": True,
            "no_submission_or_input": True,
            "runtime_user": "reviewer(10001)",
            "powercontext_version": PC_VERSION,
        },
        "sources": {
            "pc_source": {"repo": Path(pc_repo).name, "commit": pc_commit.lower(),
                          "files": len(source)},
            "pc_wheels": Path(pc_wheels).name,
            "seekdb_wheels": Path(seekdb_wheels).name,
        },
        "files": manifest_files,
    }
    staging = context_dir.parent / (context_dir.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, mode=0o700)
    try:
        for name, data in files.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (staging / "inputs.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        if context_dir.exists():
            shutil.rmtree(context_dir)
        staging.rename(context_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def verify_context(context_dir: Path = DEFAULT_CONTEXT) -> dict:
    """Re-hash the build context and compare it with the manifest."""
    context_dir = Path(context_dir).resolve()
    manifest_path = context_dir / "inputs.json"
    if not manifest_path.is_file():
        raise PrewarmError("the build context is not prepared; run --prepare first")
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except ValueError:
        raise PrewarmError("the input manifest is unreadable") from None
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise PrewarmError("the input manifest has no file table")
    problems: list[str] = []
    for name, meta in sorted(expected.items()):
        path = context_dir / name
        if not path.is_file():
            problems.append(f"{name}: missing")
            continue
        if sha256_file(path) != meta.get("sha256"):
            problems.append(f"{name}: sha256 mismatch")
    for path in sorted(context_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.relative_to(context_dir).as_posix()
        if name == "inputs.json":
            continue
        if name not in expected:
            problems.append(f"{name}: unexpected file in the build context")
    for name in FORBIDDEN_CONTEXT_NAMES:
        if any(key == name or key.startswith(name + "/") for key in expected):
            problems.append(f"{name}/: forbidden in the build context")
    if problems:
        raise PrewarmError("the build context does not match its manifest: " + "; ".join(problems[:5]))
    return manifest


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def build_command(context_dir: Path, image_tag: str, python_image: str, node_image: str,
                  debian_mirror: str, pc_version: str = PC_VERSION, no_cache: bool = False,
                  pull: bool = False) -> list[str]:
    """Return the Docker build command; no privilege escalation, no secret arguments."""
    command = ["docker", "build", "--file", str(Path(context_dir) / "Dockerfile"), "--tag", image_tag,
               "--build-arg", f"PYTHON_IMAGE={python_image}",
               "--build-arg", f"NODE_IMAGE={node_image}",
               "--build-arg", f"DEBIAN_MIRROR={debian_mirror}",
               "--build-arg", f"PC_VERSION={pc_version}"]
    if no_cache:
        command.append("--no-cache")
    if pull:
        command.append("--pull")
    command.append(str(Path(context_dir)))
    return command


def run(command: list[str], timeout: int, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a command with a bounded timeout and without a shell."""
    try:
        return subprocess.run(command, capture_output=capture, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise PrewarmError(f"command timed out after {timeout}s: {command[0]} {command[1] if len(command) > 1 else ''}") from None
    except FileNotFoundError:
        raise PrewarmError(f"command not found: {command[0]}") from None


def build(context_dir: Path = DEFAULT_CONTEXT, image_tag: str = DEFAULT_IMAGE_TAG,
          python_image: str = DEFAULT_PYTHON_IMAGE, node_image: str = DEFAULT_NODE_IMAGE,
          debian_mirror: str = DEFAULT_DEBIAN_MIRROR, pc_version: str = PC_VERSION,
          timeout: int = 3600, no_cache: bool = False, pull: bool = False,
          dry_run: bool = False) -> dict:
    """Verify the prepared context and build the image."""
    validate_image_ref(python_image, "--python-image")
    validate_image_ref(node_image, "--node-image")
    validate_mirror(debian_mirror)
    validate_parameter(image_tag, "--image-tag")
    context_dir = Path(context_dir).resolve()
    manifest = verify_context(context_dir)
    command = build_command(context_dir, image_tag, python_image, node_image, debian_mirror,
                            pc_version=pc_version, no_cache=no_cache, pull=pull)
    if dry_run:
        return {"command": command, "files": len(manifest["files"]), "built": False}
    result = run(command, timeout=timeout)
    if result.returncode:
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
        raise PrewarmError("docker build failed: " + " | ".join(tail))
    return {"command": command, "files": len(manifest["files"]), "built": True, "image_tag": image_tag}


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------
def health_command(image_tag: str, network: str | None = None, container: str | None = None) -> list[str]:
    """Return the docker run command for the isolated health container."""
    name = container or f"harbor-prewarm-health-{os.getpid()}"
    command = ["docker", "run", "--detach", "--name", name]
    if network:
        command += ["--network", network]
    command += ["--user", "10001:10001", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--pids-limit", "512", "--memory", "4g", image_tag]
    return command


def health(image_tag: str = DEFAULT_IMAGE_TAG, timeout: int = 900, network: str | None = None,
           keep: bool = False, dry_run: bool = False) -> dict:
    """Run the image in an isolated container and verify the pre-provisioned service."""
    validate_parameter(image_tag, "--image-tag")
    container = f"harbor-prewarm-health-{os.getpid()}"
    created_network = None
    if network is None:
        created_network = f"harbor-prewarm-net-{os.getpid()}"
        command = ["docker", "network", "create", "--internal", "--driver", "bridge", created_network]
        if dry_run:
            return {"command": command, "built": False}
        result = run(command, timeout=120)
        if result.returncode:
            raise PrewarmError("cannot create the isolated health network")
        network = created_network
    run_command = health_command(image_tag, network=network, container=container)
    if dry_run:
        return {"command": run_command, "built": False}
    report: dict = {"container": container, "network": network, "checks": {}}
    try:
        result = run(run_command, timeout=120)
        if result.returncode:
            raise PrewarmError("the health container did not start")
        checks = (
            ("seekdb", ["docker", "exec", "--user", "10001:10001", container,
                        "python3", "/opt/prewarm/seekdb_precheck.py"]),
            ("bootstrap", ["docker", "exec", "--user", "10001:10001", container,
                           "python3", "/opt/review/bootstrap.py"]),
            ("services", ["docker", "exec", "--user", "10001:10001", container,
                          "python3", "/opt/prewarm/inspect_services.py"]),
        )
        for name, command in checks:
            result = run(command, timeout=timeout)
            output = f"{result.stdout or ''}{result.stderr or ''}"
            if result.returncode:
                raise PrewarmError(f"the {name} check failed: " + " | ".join(output.strip().splitlines()[-3:]))
            if contains_secret_markers(output):
                raise PrewarmError(f"the {name} check printed credential material")
            report["checks"][name] = output.strip().splitlines()[-1] if output.strip() else ""
    finally:
        run(["docker", "rm", "--force", container], timeout=120)
        if created_network:
            run(["docker", "network", "rm", created_network], timeout=120)
    return report


def contains_secret_markers(text: str) -> bool:
    """Detect credential material in printed output."""
    markers = ("AUTH_TOKEN", "Bearer ", "Authorization", "auth_token", "\"token\"")
    return any(marker in text for marker in markers)


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prewarm the Harbor review image.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prepare", action="store_true", help="export the pinned sources into the build context")
    parser.add_argument("--build", action="store_true", help="build the image from the prepared context")
    parser.add_argument("--health", action="store_true", help="verify the built image in an isolated container")
    parser.add_argument("--context-dir", type=Path, default=DEFAULT_CONTEXT)
    parser.add_argument("--pc-repo", type=Path, default=DEFAULT_PC_REPO)
    parser.add_argument("--pc-commit", default=DEFAULT_PC_COMMIT)
    parser.add_argument("--pc-wheels", type=Path, default=DEFAULT_PC_WHEELS)
    parser.add_argument("--seekdb-wheels", type=Path, default=DEFAULT_SEEKDB_WHEELS)
    parser.add_argument("--image-tag", default=DEFAULT_IMAGE_TAG)
    parser.add_argument("--python-image", default=DEFAULT_PYTHON_IMAGE)
    parser.add_argument("--node-image", default=DEFAULT_NODE_IMAGE)
    parser.add_argument("--debian-mirror", default=DEFAULT_DEBIAN_MIRROR)
    parser.add_argument("--pc-version", default=PC_VERSION)
    parser.add_argument("--build-timeout", type=int, default=3600)
    parser.add_argument("--health-timeout", type=int, default=900)
    parser.add_argument("--network", default=None, help="reuse an existing Docker network for the health check")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--pull", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not (args.prepare or args.build or args.health):
        parser.error("nothing to do: pass --prepare, --build or --health explicitly")
    try:
        if args.prepare:
            manifest = prepare(context_dir=args.context_dir, pc_repo=args.pc_repo, pc_commit=args.pc_commit,
                               pc_wheels=args.pc_wheels, seekdb_wheels=args.seekdb_wheels)
            print(f"prepared {len(manifest['files'])} input files in {args.context_dir}")
        if args.build:
            result = build(context_dir=args.context_dir, image_tag=args.image_tag,
                           python_image=args.python_image, node_image=args.node_image,
                           debian_mirror=args.debian_mirror, pc_version=args.pc_version,
                           timeout=args.build_timeout, no_cache=args.no_cache, pull=args.pull,
                           dry_run=args.dry_run)
            if args.dry_run:
                print("build command: " + " ".join(result["command"]))
            else:
                print(f"built {args.image_tag} from {result['files']} hashed inputs")
        if args.health:
            report = health(image_tag=args.image_tag, timeout=args.health_timeout,
                            network=args.network, dry_run=args.dry_run)
            if args.dry_run:
                print("health command: " + " ".join(report["command"]))
            else:
                for name, line in report["checks"].items():
                    print(f"{name}: {line}")
    except PrewarmError as error:
        print(f"prewarm refused: {error}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as error:
        print(f"prewarm failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
