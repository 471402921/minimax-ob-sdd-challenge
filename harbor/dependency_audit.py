#!/usr/bin/env python3
"""Read-only dependency audit of a staged Harbor submission.

The staged ``environment/submission/minimax`` tree is read as data: explicit
``requirements*.txt`` files and ``pyproject.toml`` are parsed and their pins
are compared with the wheels of the given wheel directories.  ``setup.py`` is
never executed, contestant modules are never imported, nothing is fetched or
installed, and no credential or environment variable is read.

Only an explicit ``name==version`` pin counts as a hit (PEP 503 name
canonicalisation).  Transitive dependencies, extras and environment markers
are not resolved, so anything else stays ``unknown`` for a human decision and
``cached`` never means "ready".
"""
from __future__ import annotations

import argparse
import email.parser
import json
import os
import re
import sys
import zipfile
from pathlib import Path

MAX_FILES, MAX_TOTAL_BYTES, MAX_FILE_BYTES = 2000, 4 << 20, 256 << 10
MAX_WHEELS, MAX_WHEEL_BYTES, MAX_METADATA_BYTES = 2000, 64 << 20, 256 << 10
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[A-Za-z0-9,._ -]*\])?\s*==\s*([A-Za-z0-9][A-Za-z0-9.+!-]*)$")
SPEC = re.compile(r"^(===|==|!=|<=|>=|~=|<|>)\s*([0-9][0-9A-Za-z.*]*)$")
RANGE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[A-Za-z0-9,._ -]*\])?\s*((?:(?:===|==|!=|<=|>=|~=|<|>)\s*[0-9][0-9A-Za-z.*]*\s*,?\s*)+)$")
BARE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[A-Za-z0-9,._ -]*\])?$")
RANGE_PART = re.compile(r"(===|==|!=|<=|>=|~=|<|>)\s*([0-9][0-9A-Za-z.*]*)")
METADATA_MEMBER = re.compile(r"^[^/]+\.dist-info/METADATA$")
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
RUNNING_PYTHON = ".".join(str(part) for part in sys.version_info[:3])
CMP = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b, ">=": lambda a, b: a >= b,
       ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, "<": lambda a, b: a < b}
NOTES = ["Only explicit name==version pins are matched; transitive dependencies, extras and environment markers are not resolved.",
         "Statuses describe the given wheel directories only; a cached pin does not assert an installable or complete environment."]


class AuditError(Exception):
    """The audit cannot be performed safely (symlink, limit, missing input)."""


def _canonical(name: str) -> str:
    """PEP 503 name normalisation."""
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _version_tuple(text: str) -> tuple[int, int, int]:
    parts = []
    for chunk in text.split("."):
        digits = re.match(r"\d+", chunk)
        parts.append(int(digits.group()) if digits else 0)
    return tuple((parts + [0, 0, 0])[:3])


def _requires_python_ok(spec: str | None, running: str) -> bool:
    """False only when a Requires-Python clause is provably violated."""
    if not spec:
        return True
    current = _version_tuple(running)
    for clause in spec.split(","):
        match = SPEC.match(clause.strip()) if clause.strip() else None
        if not match:
            return True  # an unevaluable clause must never be reported as a violation
        operator, raw = match.groups()
        if operator == "===":
            if raw != running:
                return False
        elif raw.endswith(".*"):
            base = _version_tuple(raw[:-2])
            if operator == "==" and current[:len(base)] != base:
                return False
            if operator == "!=" and current[:len(base)] == base:
                return False
        elif operator == "~=":
            target = _version_tuple(raw)
            upper = (target[0], target[1] + 1, 0) if len(raw.split(".")) > 2 else (target[0] + 1, 0, 0)
            if not target <= current < upper:
                return False
        elif operator in CMP and not CMP[operator](current, _version_tuple(raw)):
            return False
    return True


def _resolve_root(submission: Path) -> Path:
    """Locate the staged minimax tree without following symlinks."""
    for candidate in (submission / "environment/submission/minimax", submission / "submission/minimax",
                      submission / "minimax", submission):
        if candidate.is_symlink():
            raise AuditError("symlinked submission directory refused")
        if not candidate.is_dir():
            continue
        if candidate == submission and (submission / "environment").is_dir():
            raise AuditError("staged task has no environment/submission/minimax tree")
        return candidate
    raise AuditError("submission tree not found")


def _is_dependency_file(name: str) -> bool:
    return name == "pyproject.toml" or (name.startswith("requirements") and name.endswith(".txt"))


def _requirement_lines(text: str) -> list[str]:
    """Keep only non-empty, non-comment requirement lines."""
    lines = []
    for raw in text.splitlines():
        line = re.sub(r"\s+#.*$", "", raw).strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _unknown_reason(line: str) -> str:
    """Why a non-pin requirement cannot be decided automatically."""
    for pattern, reason in (("^-", "option or include directive; not evaluated"),
                             (";", "environment marker not evaluated"),
                             (r"\[", "extras not resolved"),
                             ("@|://", "direct reference not evaluated")):
        if re.search(pattern, line):
            return reason
    return "not an explicit name==version pin"


def _parse_pyproject(text: str) -> tuple[list[str] | None, str]:
    """Runtime requirements of a pyproject.toml, or None when it stays unparsed.

    ``tomllib`` only exists from Python 3.11. On 3.10 a bounded fallback reads
    only the ``[project].dependencies`` string array, so an audit on an older
    host does not silently turn every pyproject into an unparsed blocker.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        return _parse_project_dependencies_fallback(text)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None, "pyproject.toml not parsed: invalid TOML; manual determination required"
    declared = (data.get("project") or {}).get("dependencies") or []
    return [str(item) for item in declared if isinstance(item, str)], ""


def _parse_project_dependencies_fallback(text: str) -> tuple[list[str] | None, str]:
    """Bounded read of [project].dependencies when tomllib is unavailable.

    A single quote-aware scanner finds the array bounds, so a nested ``[...]``
    inside an element (``powercontext[builtin]>=1.4.0``) is not mistaken for the
    end of the array and environment markers keep their quotes.
    """
    section = text.split("[project]", 1)
    if len(section) != 2:
        return None, "pyproject.toml not parsed: no [project] table; manual determination required"
    body = section[1].split("\n[", 1)[0]
    match = re.search(r"(?m)^\s*dependencies\s*=\s*\[", body)
    if not match:
        return [], "no [project].dependencies declared"
    index = match.end()
    depth = 1
    quote = None
    escaped = False
    chunks: list[str] = []
    while index < len(body) and depth:
        char = body[index]
        if quote:
            chunks.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "'\"":
            quote = char
            chunks.append(char)
        elif char == "[":
            depth += 1
            chunks.append(char)
        elif char == "]":
            depth -= 1
            if depth:
                chunks.append(char)
        else:
            chunks.append(char)
        index += 1
    if depth:
        return None, "pyproject.toml not parsed: unterminated dependencies array; manual determination required"
    elements: list[str] = []
    current = ""
    quote = None
    escaped = False
    for char in "".join(chunks):
        if quote:
            current += char
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            current += char
            continue
        if char == ",":
            elements.append(current)
            current = ""
            continue
        current += char
    if current.strip():
        elements.append(current)
    cleaned = []
    for element in elements:
        value = element.strip()
        if not value:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        cleaned.append(value)
    if not cleaned:
        return [], "no [project].dependencies declared"
    return cleaned, ("pyproject.toml parsed by the bounded fallback (tomllib absent); "
                     "only [project].dependencies is read")


def _collect_declarations(root: Path) -> list[tuple[str, str | None, str]]:
    """Collect (relative file, requirement, note) triples from the staged tree."""
    declarations: list[tuple[str, str | None, str]] = []
    visited = total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for name in list(dirnames):
            if (Path(dirpath) / name).is_symlink():
                raise AuditError(f"symlinked directory refused: {(Path(dirpath) / name).relative_to(root)}")
        for name in sorted(filenames):
            path = Path(dirpath) / name
            visited += 1
            if visited > MAX_FILES:
                raise AuditError("submission tree exceeds the file-count limit")
            if path.is_symlink():
                raise AuditError(f"symlinked file refused: {path.relative_to(root)}")
            if not _is_dependency_file(name):
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise AuditError(f"dependency file exceeds the size limit: {path.relative_to(root)}")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise AuditError("dependency files exceed the total size limit")
            relative = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace")
            if name == "pyproject.toml":
                requirements, note = _parse_pyproject(text)
                declarations += [(relative, None, note)] if requirements is None else \
                    [(relative, item, "") for item in requirements]
            else:
                declarations += [(relative, item, "") for item in _requirement_lines(text)]
    return declarations


def _read_wheel_metadata(path: Path) -> tuple[str | None, str | None, str | None]:
    """Read Name/Version/Requires-Python from a wheel's dist-info METADATA.

    The archive is opened for reading only; no member is ever extracted.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            members = sorted(n for n in archive.namelist() if METADATA_MEMBER.match(n))
            if not members:
                return None, None, None
            info = archive.getinfo(members[0])
            if info.file_size > MAX_METADATA_BYTES:
                return None, None, None
            with archive.open(info) as handle:
                raw = handle.read(MAX_METADATA_BYTES + 1)
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return None, None, None
    headers = email.parser.Parser().parsestr(raw.decode("utf-8", "replace"))
    return headers.get("Name"), headers.get("Version"), headers.get("Requires-Python")


def _build_cache(wheel_dirs: list[Path]) -> dict[str, list[tuple[str, str | None]]]:
    """Index the wheels of the given directories by canonical project name."""
    cache: dict[str, list[tuple[str, str | None]]] = {}
    count = 0
    for directory in wheel_dirs:
        if directory.is_symlink():
            raise AuditError("symlinked wheel directory refused")
        if not directory.is_dir():
            raise AuditError("wheel directory not found")
        for path in sorted(directory.glob("*.whl")):
            if path.is_symlink():
                raise AuditError("symlinked wheel file refused")
            count += 1
            if count > MAX_WHEELS:
                raise AuditError("too many wheel files")
            if path.stat().st_size > MAX_WHEEL_BYTES:
                raise AuditError("wheel file exceeds the size limit")
            name, version, requires_python = _read_wheel_metadata(path)
            if name and version:
                cache.setdefault(_canonical(name), []).append((version, requires_python))
    return cache


MARKER_ENV = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
              "python_version": "3.11", "python_full_version": "3.11.15",
              "platform_machine": "x86_64", "implementation_name": "cpython"}


def _marker_applies(marker: str, env: dict[str, str] | None = None) -> bool | None:
    """Evaluate a simple environment marker; None means "cannot decide".

    Only boolean logic, comparisons against constants and the whitelisted
    variables above are accepted. Anything else (version specifiers via
    ``in`` on an unknown list, callables, unknown names) is undecidable and the
    caller keeps the requirement blocking instead of guessing.
    """
    import ast

    values = dict(MARKER_ENV)
    if env:
        values.update(env)
    try:
        tree = ast.parse(marker.strip(), mode="eval")
    except SyntaxError:
        return None

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.BoolOp):
            results = [evaluate(value) for value in node.values]
            if isinstance(node.op, ast.And):
                if any(value is False for value in results):
                    return False
                return True if all(value is True for value in results) else None
            if any(value is True for value in results):
                return True
            return False if all(value is False for value in results) else None
        if isinstance(node, ast.Compare):
            left = evaluate(node.left)
            if left is None:
                return None
            result = True
            for op, comparator in zip(node.ops, node.comparators):
                right = evaluate(comparator)
                if right is None:
                    return None
                if isinstance(op, ast.Eq):
                    ok = left == right
                elif isinstance(op, ast.NotEq):
                    ok = left != right
                elif isinstance(op, ast.In):
                    ok = left in right
                elif isinstance(op, ast.NotIn):
                    ok = left not in right
                elif isinstance(op, (ast.Gt, ast.GtE, ast.Lt, ast.LtE)):
                    # Only plain dotted-numeric versions; anything else stays undecidable.
                    if not (re.fullmatch(r"\d+(\.\d+)*", str(left))
                            and re.fullmatch(r"\d+(\.\d+)*", str(right))):
                        return None
                    first, second = _version_tuple(str(left)), _version_tuple(str(right))
                    ok = {ast.Gt: first > second, ast.GtE: first >= second,
                          ast.Lt: first < second, ast.LtE: first <= second}[type(op)]
                else:
                    return None
                if not ok:
                    return False
                result = result and ok
                left = right
            return result
        if isinstance(node, ast.Name):
            return values.get(node.id)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    return evaluate(tree)


def _split_marker(requirement: str) -> tuple[str, str | None]:
    """Split ``name[extras]spec; marker`` on the first top-level semicolon."""
    quote = None
    for index, char in enumerate(requirement):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            continue
        if char == ";":
            return requirement[:index].strip(), requirement[index + 1:].strip()
    return requirement.strip(), None


def _classify(source: str, requirement: str | None, note: str, cache: dict) -> dict:
    """Match one declared requirement against the wheel cache."""
    record = {"file": source, "requirement": requirement, "name": None, "version": None,
              "status": "unknown", "reason": ""}
    if requirement is None:
        # An unparsed pyproject is reported, not silently treated as satisfied.
        record["status"] = "skipped" if note else "unknown"
        record["reason"] = note or "not parsed; manual determination required"
        return record
    requirement, marker = _split_marker(requirement)
    if marker:
        applies = _marker_applies(marker)
        if applies is False:
            record["status"] = "skipped"
            record["reason"] = "environment marker does not apply to this trial"
            return record
        if applies is None:
            record["reason"] = "environment marker could not be evaluated"
            return record
    single = PIN.match(requirement)
    if single:
        record["name"] = _canonical(single.group(1))
        record["version"] = single.group(2)
        available = cache.get(record["name"], [])
        if not available:
            record["status"] = "missing"
            record["reason"] = "no wheel for this project in the given wheel directories"
            return record
        for wheel_version, requires_python in available:
            if wheel_version != record["version"]:
                continue
            if not _requires_python_ok(requires_python, RUNNING_PYTHON):
                record["status"] = "unsupported"
                record["reason"] = "cached wheel Requires-Python is not satisfied by the running interpreter"
            else:
                record["status"] = "cached"
            return record
        record["status"] = "version_mismatch"
        record["reason"] = "cached wheel version differs from the declared pin"
        return record
    bare = BARE.match(requirement)
    if bare and not PIN.match(requirement):
        # No version specifier at all: pip would install whatever is newest.
        # A single cached distribution is enough to avoid a download, but the
        # absence of a pin is recorded rather than presented as reproducible.
        record["name"] = _canonical(bare.group(1))
        available = cache.get(record["name"], [])
        if not available:
            record["status"] = "missing"
            record["reason"] = "unpinned dependency with no cached wheel"
            return record
        record["version"] = available[0][0]
        record["status"] = "cached"
        record["reason"] = "unpinned dependency; a cached version exists"
        return record
    ranged = RANGE.match(requirement)
    if ranged:
        # Extras select the SAME distribution, so they are stripped rather than
        # treated as unresolved; a bounded range is decided by a cached version.
        record["name"] = _canonical(ranged.group(1))
        comparators = RANGE_PART.findall(ranged.group(2))
        available = cache.get(record["name"], [])
        if not available:
            record["status"] = "missing"
            record["reason"] = "no wheel for this project in the given wheel directories"
            return record
        for wheel_version, requires_python in available:
            if not _requires_python_ok(requires_python, RUNNING_PYTHON):
                continue
            if all(CMP[op](_version_tuple(wheel_version), _version_tuple(want))
                   for op, want in comparators):
                record["version"] = wheel_version
                record["status"] = "cached"
                record["reason"] = "cached version satisfies the declared range"
                return record
        record["status"] = "version_mismatch"
        record["reason"] = "no cached version satisfies the declared range"
        return record
    record["reason"] = _unknown_reason(requirement)
    return record


def audit(submission: Path, wheels: list[Path], target_python: str | None = None) -> dict:
    """Audit declared pins against local wheels for the TARGET interpreter.

    ``target_python`` matters because the host running the audit is not the
    image that installs the wheels (a cp311 wheel set must not be judged
    against a 3.10 host).
    """
    global RUNNING_PYTHON
    previous = RUNNING_PYTHON
    if target_python:
        if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", target_python):
            raise AuditError("target python must look like 3.11")
        RUNNING_PYTHON = target_python
    try:
        root = _resolve_root(Path(submission))
        cache = _build_cache([Path(item) for item in wheels])
        declared = [_classify(source, requirement, note, cache)
                    for source, requirement, note in _collect_declarations(root)]
    finally:
        RUNNING_PYTHON = previous
    summary = {"declared": len(declared)}
    for status in ("cached", "missing", "version_mismatch", "unsupported", "unknown", "skipped"):
        summary[status] = sum(1 for item in declared if item["status"] == status)
    blocking = [item for item in declared if item["status"] != "skipped"]
    return {"scope": "minimax", "declared": declared, "summary": summary,
            "all_pins_cached": bool(blocking) and all(item["status"] == "cached" for item in blocking),
            "ready": False, "notes": NOTES}


def main(argv: list[str] | None = None) -> int:
    """CLI: exit 0 when every declared pin is cached, 2 when anything needs a decision."""
    parser = argparse.ArgumentParser(description="Read-only dependency pin audit.")
    parser.add_argument("--submission", type=Path, required=True, help="staged task or submission tree")
    parser.add_argument("--wheels", type=Path, action="append", default=[], help="wheel directory (repeatable)")
    parser.add_argument("--python", dest="target_python", default=None,
                        help="interpreter version the wheels must satisfy (e.g. 3.11)")
    args = parser.parse_args(argv)
    try:
        result = audit(args.submission, args.wheels, args.target_python)
    except AuditError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 2
    except OSError:
        print(json.dumps({"error": "read refused: filesystem error"}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["all_pins_cached"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
