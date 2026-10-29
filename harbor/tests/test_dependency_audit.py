"""Offline tests for the read-only dependency audit.

No Docker, no platform calls, no network, no credentials: everything runs on
temporary fixtures.  The properties under test are that only an explicit
``name==version`` pin counts as a hit, that wheel archives are never
extracted, that symlinks and oversized inputs are refused, and that the
result never leaks absolute paths.
"""
from __future__ import annotations

import importlib.util
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


audit_module = module("pilot_dependency_audit", ROOT / "dependency_audit.py")


def make_wheel(directory: Path, name: str, version: str, requires_python: str = ">=3.8",
               extra_members: tuple = ()) -> Path:
    """Build a minimal wheel archive holding one dist-info METADATA entry."""
    directory.mkdir(parents=True, exist_ok=True)
    escaped = name.replace("-", "_")
    path = directory / f"{escaped}-{version}-py3-none-any.whl"
    metadata = (f"Metadata-Version: 2.1\n"
                f"Name: {name}\n"
                f"Version: {version}\n"
                f"Requires-Python: {requires_python}\n"
                "\nlong description\n")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{escaped}-{version}.dist-info/METADATA", metadata)
        for member, content in extra_members:
            archive.writestr(member, content)
    return path


class DependencyAuditTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="dep-audit-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.task = self.base / "task"
        self.submission = self.task / "environment" / "submission" / "minimax"
        self.submission.mkdir(parents=True)
        self.wheels = self.base / "wheels"
        self.wheels.mkdir()

    def requirements(self, text: str, relative: str = "requirements.txt") -> Path:
        path = self.submission / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def audit(self, *extra):
        target = None
        if extra:
            target = extra[1]
        return audit_module.audit(self.task, [self.wheels], target)

    def test_exact_pin_is_reported_as_cached(self):
        make_wheel(self.wheels, "numpy", "1.26.4")
        self.requirements("numpy==1.26.4\n")
        result = self.audit()
        self.assertEqual(result["declared"][0]["status"], "cached")
        self.assertEqual(result["declared"][0]["name"], "numpy")
        self.assertEqual(result["declared"][0]["version"], "1.26.4")
        self.assertTrue(result["all_pins_cached"])
        self.assertFalse(result["ready"])

    def test_missing_project_is_reported(self):
        make_wheel(self.wheels, "numpy", "1.26.4")
        self.requirements("pandas==2.0.0\n")
        result = self.audit()
        self.assertEqual(result["declared"][0]["status"], "missing")
        self.assertFalse(result["all_pins_cached"])

    def test_version_mismatch_is_not_a_hit(self):
        make_wheel(self.wheels, "numpy", "1.26.0")
        self.requirements("numpy==1.26.4\n")
        self.assertEqual(self.audit()["declared"][0]["status"], "version_mismatch")

    def test_requires_python_mismatch_is_unsupported(self):
        make_wheel(self.wheels, "numpy", "1.26.4", requires_python=">=3.99")
        self.requirements("numpy==1.26.4\n")
        self.assertEqual(self.audit()["declared"][0]["status"], "unsupported")

    def test_platform_markers_are_evaluated_not_guessed(self):
        self.requirements("numpy==1.26.4; sys_platform == 'linux'\n"
                          "numpy==1.26.4; sys_platform == 'win32'\n"
                          "numpy==1.26.4; python_version >= '3.8' and os_name == 'posix'\n")
        make_wheel(self.wheels, "numpy", "1.26.4")
        result = self.audit()
        self.assertEqual([item["status"] for item in result["declared"]],
                         ["cached", "skipped", "cached"])
        self.assertTrue(result["all_pins_cached"])

    def test_unevaluable_marker_stays_blocking(self):
        self.requirements("numpy==1.26.4; platform_release.startswith('5')\n")
        make_wheel(self.wheels, "numpy", "1.26.4")
        result = self.audit()
        self.assertEqual(result["declared"][0]["status"], "unknown")
        self.assertFalse(result["all_pins_cached"])

    def test_markers_ranges_and_extras_are_classified(self):
        self.requirements('torch==2.0.0; python_version < "3.12"\n'
                          "requests>=2.0\n"
                          "rich[jupyter]==13.0\n"
                          "-r other.txt\n")
        result = self.audit()
        # Markers and include directives stay undecided; extras select the same
        # distribution, and an unresolvable range is reported as missing.
        self.assertEqual([item["status"] for item in result["declared"]],
                         ["missing", "missing", "missing", "unknown"])
        reasons = " ".join(item["reason"] for item in result["declared"])
        self.assertIn("option or include", reasons)
        self.assertNotIn("extras not resolved", reasons)
        self.assertFalse(result["all_pins_cached"])

    def test_extras_and_ranges_hit_the_same_wheel(self):
        make_wheel(self.wheels, "powercontext", "1.4.0")
        make_wheel(self.wheels, "pytest", "8.4.2")
        self.requirements("powercontext[cli,server,seekdb]==1.4.0\npytest>=8,<9\n")
        result = self.audit()
        self.assertEqual([item["status"] for item in result["declared"]], ["cached", "cached"])
        self.assertEqual(result["declared"][1]["version"], "8.4.2")
        self.assertTrue(result["all_pins_cached"])

    def test_target_python_is_used_for_requires_python(self):
        make_wheel(self.wheels, "needs-new-python", "1.0", requires_python=">=3.11")
        self.requirements("needs-new-python==1.0\n")
        self.assertEqual(self.audit("--python", "3.11")["declared"][0]["status"], "cached")
        self.assertEqual(self.audit("--python", "3.10")["declared"][0]["status"], "unsupported")

    def test_names_are_canonicalised(self):
        make_wheel(self.wheels, "some-package-name", "1.0")
        self.requirements("Some_Package.Name==1.0\n")
        record = self.audit()["declared"][0]
        self.assertEqual(record["name"], "some-package-name")
        self.assertEqual(record["status"], "cached")

    def test_pyproject_is_parsed_or_flagged_for_manual_review(self):
        (self.submission / "pyproject.toml").write_text(
            '[project]\nname = "demo"\ndependencies = ["numpy==1.26.4"]\n', encoding="utf-8")
        make_wheel(self.wheels, "numpy", "1.26.4")
        result = self.audit()
        if audit_module.sys.version_info >= (3, 11):
            self.assertEqual(result["declared"][0]["status"], "cached")
            self.assertTrue(result["all_pins_cached"])
        else:
            # Python 3.10 has no tomllib; the bounded fallback still reads
            # [project].dependencies, so the pin is matched instead of skipped.
            self.assertEqual(result["declared"][0]["status"], "cached")
            self.assertTrue(result["all_pins_cached"])

    def test_setup_py_is_never_executed(self):
        (self.submission / "setup.py").write_text("raise SystemExit('must not run')\n", encoding="utf-8")
        self.requirements("numpy==1.26.4\n")
        make_wheel(self.wheels, "numpy", "1.26.4")
        result = self.audit()
        self.assertEqual([item["file"] for item in result["declared"]], ["requirements.txt"])

    def test_malicious_zip_member_is_never_extracted(self):
        make_wheel(self.wheels, "evil", "1.0",
                   extra_members=[("../../escape.py", "print('pwned')"), ("/abs/root.py", "print('pwned')")])
        self.requirements("evil==1.0\n")
        before = sorted(str(p) for p in self.base.rglob("*"))
        with patch.object(zipfile.ZipFile, "extractall", side_effect=AssertionError("no extraction")), \
                patch.object(zipfile.ZipFile, "extract", side_effect=AssertionError("no extraction")):
            result = self.audit()
        self.assertEqual(before, sorted(str(p) for p in self.base.rglob("*")))
        self.assertEqual(result["declared"][0]["status"], "cached")
        self.assertNotIn("pwned", json.dumps(result))

    def test_symlinked_dependency_file_is_refused(self):
        target = self.requirements("numpy==1.26.4\n")
        (self.submission / "requirements-link.txt").symlink_to(target)
        with self.assertRaises(audit_module.AuditError):
            self.audit()

    def test_symlinked_wheel_is_refused(self):
        make_wheel(self.wheels, "numpy", "1.26.4")
        (self.wheels / "link-1.0-py3-none-any.whl").symlink_to(self.wheels / "numpy-1.26.4-py3-none-any.whl")
        with self.assertRaises(audit_module.AuditError):
            self.audit()

    def test_oversized_dependency_file_is_refused(self):
        self.requirements("numpy==1.26.4\n" + "# padding\n" * 40000)
        with self.assertRaises(audit_module.AuditError):
            self.audit()

    def test_result_leaks_no_absolute_paths(self):
        make_wheel(self.wheels, "numpy", "1.26.4")
        self.requirements("numpy==1.26.4\n")
        payload = json.dumps(self.audit())
        self.assertNotIn(str(self.base), payload)
        self.assertNotIn(str(self.task), payload)
        self.assertIn("requirements.txt", payload)

    def test_cli_reports_cached_and_missing_with_exit_codes(self):
        make_wheel(self.wheels, "numpy", "1.26.4")
        self.requirements("numpy==1.26.4\n")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = audit_module.main(["--submission", str(self.task), "--wheels", str(self.wheels)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buffer.getvalue())["all_pins_cached"])
        self.requirements("numpy==1.26.4\npandas==2.0.0\n")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = audit_module.main(["--submission", str(self.task), "--wheels", str(self.wheels)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buffer.getvalue())["summary"]["missing"], 1)

    def test_cli_exits_two_for_unknown_requirements(self):
        self.requirements("requests>=2.0; platform_release.startswith('5')\n")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = audit_module.main(["--submission", str(self.task), "--wheels", str(self.wheels)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buffer.getvalue())["summary"]["unknown"], 1)


if __name__ == "__main__":
    unittest.main()
