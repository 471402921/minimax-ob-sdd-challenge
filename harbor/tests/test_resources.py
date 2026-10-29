"""Offline tests for the stdlib host resource admission module.

Nothing here reaches the network, sudo, Docker, a model, a platform or the
real ``/proc`` tree: every source is a temporary fixture file and every
injected callable is a local stub.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import sys
import unittest
from collections import namedtuple
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3
KIB = 1024

Usage = namedtuple("Usage", "total used free")

MEMINFO = (
    "MemTotal:       65793640 kB\n"
    "MemFree:         1234567 kB\n"
    "MemAvailable:   23456789 kB\n"
    "SwapTotal:       8388604 kB\n"
)
PRESSURE = (
    "some avg10=0.15 avg60=0.20 avg300=0.05 total=1234\n"
    "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
)
VMSTAT = (
    "nr_free_pages 123456\n"
    "pswpin 100\n"
    "pswpout 250\n"
)
STAT = (
    "cpu  1000 20 300 4000 100 0 50 5 0 0\n"
    "cpu0 500 10 150 2000 50 0 25 2 0 0\n"
    "intr 12345 0 0\n"
)
LOADAVG = "0.42 0.35 0.30 2/345 6789\n"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


resources = _load("harbor_resources", ROOT / "resources.py")


def write_tree(root: Path, *, meminfo=MEMINFO, pressure=PRESSURE, vmstat=VMSTAT,
               stat=STAT, loadavg=LOADAVG) -> dict[str, Path]:
    """Materialise a fake /proc tree. ``None`` means "this file is absent"."""
    layout = {
        "meminfo": "meminfo",
        "pressure": "pressure/memory",
        "vmstat": "vmstat",
        "stat": "stat",
        "loadavg": "loadavg",
    }
    contents = {"meminfo": meminfo, "pressure": pressure, "vmstat": vmstat,
                "stat": stat, "loadavg": loadavg}
    for key, relative in layout.items():
        content = contents[key]
        if content is None:
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return {
        "meminfo_path": root / "meminfo",
        "pressure_path": root / "pressure/memory",
        "vmstat_path": root / "vmstat",
        "stat_path": root / "stat",
        "loadavg_path": root / "loadavg",
    }


def disk_stub(free_gib: float = 500.0, total_gib: float = 1000.0):
    """A ``shutil.disk_usage`` lookalike with deterministic numbers."""
    def usage(_path: Path) -> Usage:
        return Usage(int(total_gib * GIB), int((total_gib - free_gib) * GIB),
                     int(free_gib * GIB))
    return usage


def meminfo_with_gib(gib: float) -> str:
    return f"MemTotal:       65793640 kB\nMemAvailable:   {int(gib * KIB * KIB)} kB\n"


def jsonable(value):
    """Normalise tuples to lists so a JSON round trip can be compared."""
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


class ResourceTestCase(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def tree(self, **contents) -> dict[str, Path]:
        return write_tree(self.root, **contents)

    def capture(self, paths=None, *, disk=None, cpu_count=lambda: 8,
                monotonic=lambda: 100.0, strict=True, **options):
        paths = paths if paths is not None else self.tree()
        call = dict(
            meminfo_path=paths["meminfo_path"],
            pressure_path=paths["pressure_path"],
            vmstat_path=paths["vmstat_path"],
            stat_path=paths["stat_path"],
            loadavg_path=paths["loadavg_path"],
            disk_usage=disk if disk is not None else disk_stub(),
            cpu_count=cpu_count,
            monotonic=monotonic,
            strict=strict,
        )
        call.update(options)
        return resources.capture(self.root, **call)


class CaptureTests(ResourceTestCase):
    def test_capture_reads_every_source(self):
        snapshot = self.capture()
        self.assertAlmostEqual(snapshot.memory_available_gib, 23456789 / KIB / KIB, places=6)
        self.assertAlmostEqual(snapshot.disk_free_gib, 500.0, places=6)
        self.assertAlmostEqual(snapshot.disk_total_gib, 1000.0, places=6)
        self.assertAlmostEqual(snapshot.memory_psi_avg10, 0.15, places=6)
        self.assertEqual(snapshot.swap_in_pages, 100)
        self.assertEqual(snapshot.swap_out_pages, 250)
        # busy = user+nice+system+irq+softirq+steal = 1000+20+300+0+50+5
        self.assertEqual(snapshot.cpu_busy_ticks, 1375)
        # total = busy + idle + iowait = 1375 + 4000 + 100
        self.assertEqual(snapshot.cpu_total_ticks, 5475)
        self.assertAlmostEqual(snapshot.load_avg_1m, 0.42, places=6)
        self.assertEqual(snapshot.cpu_count, 8)
        self.assertEqual(snapshot.monotonic, 100.0)
        self.assertTrue(snapshot.captured_at.endswith("+00:00"))
        self.assertEqual(snapshot.errors, ())
        self.assertEqual(snapshot.unknowns, ())

    def test_snapshot_is_a_frozen_dataclass_with_json_safe_fields(self):
        snapshot = self.capture()
        payload = dataclasses.asdict(snapshot)
        self.assertEqual(json.loads(json.dumps(payload)), jsonable(payload))
        self.assertIsNone(payload["memory_psi_avg10"] if payload["memory_psi_avg10"] is None else None)
        self.assertEqual(
            set(payload),
            {
                "memory_available_gib", "disk_free_gib", "disk_total_gib",
                "memory_psi_avg10", "swap_in_pages", "swap_out_pages",
                "cpu_busy_ticks", "cpu_total_ticks", "load_avg_1m", "cpu_count",
                "monotonic", "captured_at", "errors", "unknowns",
            },
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snapshot.memory_available_gib = 1.0  # type: ignore[misc]

    def test_missing_meminfo_is_fail_closed(self):
        paths = self.tree(meminfo=None)
        with self.assertRaises(resources.ResourceCaptureError) as ctx:
            self.capture(paths)
        self.assertIn(resources.E_MEMINFO_UNAVAILABLE, str(ctx.exception))
        lenient = self.capture(paths, strict=False)
        self.assertIsNone(lenient.memory_available_gib)
        self.assertIn(resources.E_MEMINFO_UNAVAILABLE, lenient.errors)

    def test_meminfo_without_memavailable_line_is_fail_closed(self):
        paths = self.tree(meminfo="MemTotal:       65793640 kB\nMemFree:  1 kB\n")
        with self.assertRaises(resources.ResourceCaptureError):
            self.capture(paths)
        lenient = self.capture(paths, strict=False)
        self.assertIsNone(lenient.memory_available_gib)
        self.assertIn(resources.E_MEMINFO_MISSING_FIELD, lenient.errors)

    def test_non_numeric_memavailable_is_fail_closed(self):
        paths = self.tree(meminfo="MemAvailable:   notanumber kB\n")
        with self.assertRaises(resources.ResourceCaptureError):
            self.capture(paths)
        lenient = self.capture(paths, strict=False)
        self.assertIsNone(lenient.memory_available_gib)
        self.assertIn(resources.E_MEMINFO_INVALID, lenient.errors)

    def test_negative_memavailable_is_fail_closed(self):
        paths = self.tree(meminfo="MemAvailable:   -5 kB\n")
        lenient = self.capture(paths, strict=False)
        self.assertIn(resources.E_MEMINFO_INVALID, lenient.errors)

    def test_memavailable_with_wrong_unit_is_fail_closed(self):
        paths = self.tree(meminfo="MemAvailable:   1234 bytes\n")
        lenient = self.capture(paths, strict=False)
        self.assertIn(resources.E_MEMINFO_INVALID, lenient.errors)

    def test_disk_usage_failure_is_fail_closed(self):
        def broken(_path):
            raise OSError("no such filesystem")
        with self.assertRaises(resources.ResourceCaptureError):
            self.capture(disk=broken)
        lenient = self.capture(disk=broken, strict=False)
        self.assertIsNone(lenient.disk_free_gib)
        self.assertIn(resources.E_DISK_UNAVAILABLE, lenient.errors)

    def test_non_numeric_disk_free_is_fail_closed(self):
        def broken(_path):
            return Usage(1000, 0, "not-a-number")
        lenient = self.capture(disk=broken, strict=False)
        self.assertIsNone(lenient.disk_free_gib)
        self.assertIn(resources.E_DISK_INVALID, lenient.errors)

    def test_negative_disk_free_is_fail_closed(self):
        def broken(_path):
            return Usage(1000, 0, -5)
        lenient = self.capture(disk=broken, strict=False)
        self.assertIn(resources.E_DISK_INVALID, lenient.errors)

    def test_psi_missing_is_unknown_not_zero(self):
        paths = self.tree(pressure=None)
        snapshot = self.capture(paths, strict=False)
        self.assertIsNone(snapshot.memory_psi_avg10)
        self.assertNotEqual(snapshot.memory_psi_avg10, 0)
        self.assertIn(resources.U_PSI_UNKNOWN, snapshot.unknowns)
        self.assertEqual(snapshot.errors, ())
        # An unknown PSI must not invent a pressure failure.
        self.assertEqual(resources.AdmissionPolicy().evaluate(snapshot), [])

    def test_psi_without_some_line_is_unknown(self):
        paths = self.tree(pressure="full avg10=0.10 avg60=0.10 avg300=0.10 total=5\n")
        snapshot = self.capture(paths, strict=False)
        self.assertIsNone(snapshot.memory_psi_avg10)
        self.assertIn(resources.U_PSI_UNKNOWN, snapshot.unknowns)
        self.assertEqual(snapshot.errors, ())

    def test_non_numeric_psi_is_fail_closed(self):
        paths = self.tree(pressure="some avg10=abc avg60=0.20 total=1\n")
        with self.assertRaises(resources.ResourceCaptureError):
            self.capture(paths)
        lenient = self.capture(paths, strict=False)
        self.assertIsNone(lenient.memory_psi_avg10)
        self.assertIn(resources.E_PSI_INVALID, lenient.errors)

    def test_vmstat_invalid_counter_is_fail_closed(self):
        paths = self.tree(vmstat="pswpin abc\npswpout 250\n")
        with self.assertRaises(resources.ResourceCaptureError):
            self.capture(paths)
        lenient = self.capture(paths, strict=False)
        self.assertIsNone(lenient.swap_in_pages)
        self.assertIn(resources.E_VMSTAT_INVALID, lenient.errors)

    def test_vmstat_missing_counters_are_unknown(self):
        paths = self.tree(vmstat="nr_free_pages 123456\n")
        snapshot = self.capture(paths, strict=False)
        self.assertIsNone(snapshot.swap_in_pages)
        self.assertIsNone(snapshot.swap_out_pages)
        self.assertIn(resources.U_SWAP_UNKNOWN, snapshot.unknowns)
        self.assertEqual(snapshot.errors, ())

    def test_cpu_counters_invalid_are_fail_closed(self):
        paths = self.tree(stat="cpu  1000 20 abc 4000 100 0 50 5 0 0\n")
        with self.assertRaises(resources.ResourceCaptureError):
            self.capture(paths)
        lenient = self.capture(paths, strict=False)
        self.assertIsNone(lenient.cpu_busy_ticks)
        self.assertIn(resources.E_STAT_INVALID, lenient.errors)

    def test_stat_without_cpu_line_is_unknown(self):
        paths = self.tree(stat="intr 12345 0 0\nctxt 999\n")
        snapshot = self.capture(paths, strict=False)
        self.assertIsNone(snapshot.cpu_busy_ticks)
        self.assertIsNone(snapshot.cpu_total_ticks)
        self.assertIn(resources.U_CPU_UNKNOWN, snapshot.unknowns)
        self.assertEqual(snapshot.errors, ())

    def test_loadavg_invalid_is_fail_closed(self):
        paths = self.tree(loadavg="not-a-load 0.2 0.1 1/2 3\n")
        with self.assertRaises(resources.ResourceCaptureError):
            self.capture(paths)
        lenient = self.capture(paths, strict=False)
        self.assertIsNone(lenient.load_avg_1m)
        self.assertIn(resources.E_LOADAVG_INVALID, lenient.errors)

    def test_loadavg_missing_is_unknown(self):
        paths = self.tree(loadavg=None)
        snapshot = self.capture(paths, strict=False)
        self.assertIsNone(snapshot.load_avg_1m)
        self.assertIn(resources.U_LOADAVG_UNKNOWN, snapshot.unknowns)

    def test_cpu_count_unknown_when_kernel_cannot_report_it(self):
        snapshot = self.capture(cpu_count=lambda: None)
        self.assertIsNone(snapshot.cpu_count)
        self.assertIn(resources.U_CPU_COUNT_UNKNOWN, snapshot.unknowns)

    def test_cpu_count_exception_is_unknown(self):
        def broken():
            raise OSError("no cpu info")
        snapshot = self.capture(cpu_count=broken)
        self.assertIsNone(snapshot.cpu_count)
        self.assertIn(resources.U_CPU_COUNT_UNKNOWN, snapshot.unknowns)


class PolicyTests(ResourceTestCase):
    def test_default_policy_thresholds(self):
        policy = resources.AdmissionPolicy()
        self.assertEqual(policy.min_available_gib, 12.0)
        self.assertEqual(policy.min_disk_gib, 150.0)
        self.assertEqual(policy.max_memory_psi_avg10, 1.0)

    def test_policy_rejects_invalid_thresholds(self):
        for kwargs in (
            {"min_available_gib": -1},
            {"min_disk_gib": "150"},
            {"max_memory_psi_avg10": float("nan")},
            {"min_available_gib": float("inf")},
        ):
            with self.assertRaises(ValueError):
                resources.AdmissionPolicy(**kwargs)

    def test_policy_coerces_integers_to_floats(self):
        policy = resources.AdmissionPolicy(min_available_gib=12)
        self.assertIsInstance(policy.min_available_gib, float)

    def test_memory_below_threshold_is_refused(self):
        paths = self.tree(meminfo=meminfo_with_gib(11.9))
        snapshot = self.capture(paths, strict=False)
        reasons = resources.AdmissionPolicy().evaluate(snapshot)
        self.assertIn("memory_low: 11.9 GiB available < 12.0 GiB required", reasons)

    def test_memory_exactly_at_threshold_is_admitted(self):
        paths = self.tree(meminfo=meminfo_with_gib(12.0))
        snapshot = self.capture(paths, strict=False)
        self.assertEqual(resources.AdmissionPolicy().evaluate(snapshot), [])

    def test_disk_below_threshold_is_refused(self):
        snapshot = self.capture(disk=disk_stub(free_gib=100.0), strict=False)
        reasons = resources.AdmissionPolicy().evaluate(snapshot)
        self.assertIn("disk_low: 100.0 GiB free < 150.0 GiB required", reasons)

    def test_disk_exactly_at_threshold_is_admitted(self):
        snapshot = self.capture(disk=disk_stub(free_gib=150.0), strict=False)
        self.assertEqual(resources.AdmissionPolicy().evaluate(snapshot), [])

    def test_missing_core_signals_are_refused_with_fail_closed_reason(self):
        reasons = resources.AdmissionPolicy().evaluate(resources.Snapshot())
        self.assertTrue(any(reason.startswith("memory_unavailable") for reason in reasons))
        self.assertTrue(any(reason.startswith("disk_unavailable") for reason in reasons))
        self.assertTrue(all("fail closed" in reason for reason in reasons))

    def test_recorded_errors_are_refused_even_when_thresholds_pass(self):
        paths = self.tree(meminfo=None)
        snapshot = self.capture(paths, strict=False)
        reasons = resources.AdmissionPolicy().evaluate(snapshot)
        self.assertTrue(any("fail closed" in reason for reason in reasons))

    def test_psi_above_limit_is_refused(self):
        paths = self.tree(pressure="some avg10=2.50 avg60=1.00 avg300=0.10 total=9\n")
        snapshot = self.capture(paths, strict=False)
        reasons = resources.AdmissionPolicy().evaluate(snapshot)
        self.assertIn("memory_psi_high: some avg10 2.50 > 1.00 allowed", reasons)

    def test_psi_exactly_at_limit_is_admitted(self):
        paths = self.tree(pressure="some avg10=1.00 avg60=0.00 avg300=0.00 total=9\n")
        snapshot = self.capture(paths, strict=False)
        self.assertEqual(resources.AdmissionPolicy().evaluate(snapshot), [])

    def test_reasons_are_short_and_carry_numbers_only(self):
        paths = self.tree(meminfo=meminfo_with_gib(4.0))
        snapshot = self.capture(paths, strict=False)
        reasons = resources.AdmissionPolicy().evaluate(snapshot)
        self.assertTrue(reasons)
        for reason in reasons:
            self.assertLess(len(reason), 120)
            self.assertNotIn("/", reason)
            self.assertNotIn(str(self.root), reason)

    def test_module_level_evaluate_matches_the_policy_method(self):
        snapshot = self.capture(disk=disk_stub(free_gib=10.0), strict=False)
        policy = resources.AdmissionPolicy()
        self.assertEqual(resources.evaluate(snapshot), policy.evaluate(snapshot))
        self.assertEqual(resources.admits(snapshot), policy.admits(snapshot))
        self.assertFalse(resources.admits(snapshot))


class DeltaTests(ResourceTestCase):
    def snapshot(self, **fields):
        base = dict(memory_available_gib=32.0, disk_free_gib=400.0)
        base.update(fields)
        return resources.Snapshot(**base)

    def test_cpu_busy_percent_between_two_snapshots(self):
        earlier = self.snapshot(cpu_busy_ticks=40, cpu_total_ticks=100, monotonic=10.0)
        later = self.snapshot(cpu_busy_ticks=100, cpu_total_ticks=200, monotonic=70.0)
        growth = resources.delta(earlier, later)
        self.assertEqual(growth.elapsed_seconds, 60.0)
        self.assertEqual(growth.cpu_busy_ticks, 60)
        self.assertEqual(growth.cpu_total_ticks, 100)
        self.assertAlmostEqual(growth.cpu_busy_percent, 60.0, places=6)
        self.assertNotIn("cpu_counter_reset", growth.notes)
        self.assertNotIn("cpu_unknown", growth.notes)

    def test_cpu_counter_reset_yields_unknown_busy_percent(self):
        earlier = self.snapshot(cpu_busy_ticks=400, cpu_total_ticks=1000, monotonic=10.0)
        later = self.snapshot(cpu_busy_ticks=10, cpu_total_ticks=50, monotonic=70.0)
        growth = resources.delta(earlier, later)
        self.assertIsNone(growth.cpu_busy_percent)
        self.assertIsNone(growth.cpu_busy_ticks)
        self.assertIn("cpu_counter_reset", growth.notes)

    def test_cpu_counters_that_do_not_advance_are_unknown(self):
        earlier = self.snapshot(cpu_busy_ticks=40, cpu_total_ticks=100, monotonic=10.0)
        later = self.snapshot(cpu_busy_ticks=40, cpu_total_ticks=100, monotonic=70.0)
        growth = resources.delta(earlier, later)
        self.assertIsNone(growth.cpu_busy_percent)
        self.assertIn("cpu_counter_reset", growth.notes)

    def test_unknown_cpu_counters_are_reported_as_unknown(self):
        earlier = self.snapshot(monotonic=10.0)
        later = self.snapshot(monotonic=70.0)
        growth = resources.delta(earlier, later)
        self.assertIsNone(growth.cpu_busy_percent)
        self.assertIn("cpu_unknown", growth.notes)

    def test_swap_deltas_between_two_snapshots(self):
        earlier = self.snapshot(swap_in_pages=100, swap_out_pages=250, monotonic=10.0)
        later = self.snapshot(swap_in_pages=150, swap_out_pages=260, monotonic=70.0)
        growth = resources.delta(earlier, later)
        self.assertEqual(growth.swap_in_pages, 50)
        self.assertEqual(growth.swap_out_pages, 10)
        self.assertEqual(growth.swap_total_pages, 60)

    def test_swap_counter_reset_yields_unknown_delta(self):
        earlier = self.snapshot(swap_in_pages=100, swap_out_pages=250, monotonic=10.0)
        later = self.snapshot(swap_in_pages=10, swap_out_pages=260, monotonic=70.0)
        growth = resources.delta(earlier, later)
        self.assertIsNone(growth.swap_in_pages)
        self.assertIn("swap_counter_reset", growth.notes)
        self.assertIsNone(growth.swap_total_pages)

    def test_unknown_swap_counters_are_reported_as_unknown(self):
        earlier = self.snapshot(monotonic=10.0)
        later = self.snapshot(monotonic=70.0)
        growth = resources.delta(earlier, later)
        self.assertIsNone(growth.swap_in_pages)
        self.assertIn("swap_in_unknown", growth.notes)

    def test_non_monotonic_clock_is_reported(self):
        earlier = self.snapshot(monotonic=70.0)
        later = self.snapshot(monotonic=10.0)
        growth = resources.delta(earlier, later)
        self.assertIsNone(growth.elapsed_seconds)
        self.assertIn("clock_not_monotonic", growth.notes)

    def test_missing_clock_is_reported(self):
        growth = resources.delta(self.snapshot(), self.snapshot())
        self.assertIsNone(growth.elapsed_seconds)
        self.assertIn("clock_unknown", growth.notes)

    def test_delta_is_json_serialisable(self):
        earlier = self.snapshot(cpu_busy_ticks=1, cpu_total_ticks=2, monotonic=1.0)
        later = self.snapshot(cpu_busy_ticks=3, cpu_total_ticks=6, monotonic=2.0)
        payload = dataclasses.asdict(resources.delta(earlier, later))
        self.assertEqual(json.loads(json.dumps(payload)), jsonable(payload))


class CliTests(ResourceTestCase):
    def snapshot(self, **fields):
        base = dict(memory_available_gib=32.0, disk_free_gib=400.0,
                    memory_psi_avg10=0.10, swap_in_pages=1, swap_out_pages=2,
                    cpu_busy_ticks=100, cpu_total_ticks=200, load_avg_1m=0.5,
                    cpu_count=8, monotonic=12.5, captured_at="2026-01-01T00:00:00+00:00")
        base.update(fields)
        return resources.Snapshot(**base)

    def run_cli(self, snapshot):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = resources.main(["--path", str(self.root)],
                                  capture_fn=lambda path: snapshot)
        return code, json.loads(buffer.getvalue())

    def test_cli_prints_one_json_sample_and_admits(self):
        code, payload = self.run_cli(self.snapshot())
        self.assertEqual(code, 0)
        self.assertTrue(payload["admission"]["ok"])
        self.assertEqual(payload["admission"]["reasons"], [])
        self.assertEqual(payload["snapshot"]["memory_available_gib"], 32.0)
        self.assertEqual(payload["policy"]["min_available_gib"], 12.0)
        self.assertEqual(payload["policy"]["min_disk_gib"], 150.0)
        self.assertEqual(payload["policy"]["max_memory_psi_avg10"], 1.0)

    def test_cli_reports_refusal_with_exit_code_one(self):
        code, payload = self.run_cli(self.snapshot(memory_available_gib=2.0))
        self.assertEqual(code, 1)
        self.assertFalse(payload["admission"]["ok"])
        self.assertTrue(any("memory_low" in reason for reason in payload["admission"]["reasons"]))

    def test_cli_fails_closed_when_capture_raises(self):
        def broken(path):
            raise resources.ResourceCaptureError("host resource capture failed: meminfo_unavailable")
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stdout(io.StringIO()):
                resources.main(["--path", str(self.root)], capture_fn=broken)
        self.assertEqual(ctx.exception.code, 2)

    def test_cli_payload_carries_no_paths_or_host_data(self):
        _, payload = self.run_cli(self.snapshot())
        blob = json.dumps(payload)
        self.assertNotIn(str(self.root), blob)
        self.assertNotIn("/home", blob)


class RunnerContractTests(ResourceTestCase):
    """The shape the main runner relies on."""

    def test_runner_usage_pattern_works(self):
        snapshot = resources.capture(self.root)
        reasons = resources.AdmissionPolicy().evaluate(snapshot)
        payload = dataclasses.asdict(snapshot)
        self.assertIsInstance(reasons, list)
        self.assertTrue(all(isinstance(reason, str) for reason in reasons))
        self.assertIn("memory_available_gib", payload)
        self.assertEqual(payload["memory_available_gib"], snapshot.memory_available_gib)
        self.assertEqual(json.loads(json.dumps(payload)), jsonable(payload))

    def test_field_name_for_the_reserved_prediction_budget(self):
        fields = {field.name for field in dataclasses.fields(resources.Snapshot)}
        self.assertIn("memory_available_gib", fields)
        self.assertNotIn("available_gib", fields)

    def test_policy_evaluate_is_callable_on_the_instance_and_the_module(self):
        policy = resources.AdmissionPolicy()
        snapshot = resources.Snapshot(memory_available_gib=1.0, disk_free_gib=1.0)
        self.assertEqual(policy.evaluate(snapshot), resources.evaluate(snapshot))

    def test_module_imports_without_sys_modules_registration(self):
        # A runner may load the file directly; the dataclasses must still build.
        name = "harbor_resources_unregistered"
        spec = importlib.util.spec_from_file_location(name, ROOT / "resources.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(module.AdmissionPolicy().evaluate(module.Snapshot()))
        self.assertNotIn(name, sys.modules)


class ContainerStatsTests(ResourceTestCase):
    """Docker sampling is exercised through an injected runner; Docker is never called."""

    ROW = json.dumps({
        "ID": "0123456789ab",
        "Container": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "Name": "pilot-runner",
        "CPUPerc": "37.50%",
        "MemUsage": "512MiB / 4GiB",
        "PIDs": "17",
    })

    def runner(self, output: str, recorder: list | None = None):
        def call(argv, timeout):
            if recorder is not None:
                recorder.append((list(argv), timeout))
            return output
        return call

    def test_only_the_requested_containers_are_queried(self):
        recorder: list = []
        rows = resources.capture_containers(
            ["0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"],
            runner=self.runner(self.ROW, recorder),
        )
        argv, timeout = recorder[0]
        self.assertEqual(argv[:5], ["docker", "stats", "--no-stream", "--format", "json"])
        self.assertEqual(len(argv), 6)
        self.assertEqual(timeout, resources.DOCKER_STATS_TIMEOUT)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "pilot-runner")
        self.assertAlmostEqual(rows[0].cpu_percent, 37.5, places=6)
        self.assertEqual(rows[0].mem_usage, "512MiB / 4GiB")
        self.assertAlmostEqual(rows[0].mem_used_gib, 0.5, places=6)
        self.assertAlmostEqual(rows[0].mem_limit_gib, 4.0, places=6)
        self.assertEqual(rows[0].pids, 17)
        self.assertIsNone(rows[0].error)

    def test_identifiers_that_look_like_flags_are_refused(self):
        for bad in ("--format", "-rf", "a b", "a;b", "", "x" * 200):
            with self.assertRaises(ValueError):
                resources.capture_containers([bad], runner=self.runner(self.ROW))

    def test_empty_selection_returns_nothing_and_never_calls_docker(self):
        recorder: list = []
        self.assertEqual(resources.capture_containers([], runner=self.runner(self.ROW, recorder)), [])
        self.assertEqual(recorder, [])

    def test_container_without_a_row_is_reported_as_unknown(self):
        rows = resources.capture_containers(["deadbeef"], runner=self.runner(""))
        self.assertEqual(rows[0].error, "no_stats")
        self.assertIsNone(rows[0].cpu_percent)

    def test_unparsable_output_fails_closed(self):
        with self.assertRaises(resources.ResourceCaptureError):
            resources.capture_containers(["deadbeef"], runner=self.runner("not json"))

    def test_malformed_values_degrade_to_unknown(self):
        row = json.dumps({"ID": "abc", "Name": "x", "CPUPerc": "n/a",
                          "MemUsage": "n/a", "PIDs": "n/a"})
        rows = resources.capture_containers(["abc"], runner=self.runner(row))
        self.assertIsNone(rows[0].cpu_percent)
        self.assertIsNone(rows[0].mem_used_gib)
        self.assertIsNone(rows[0].pids)

    def test_timeout_must_be_positive(self):
        with self.assertRaises(ValueError):
            resources.capture_containers(["abc"], timeout=0, runner=self.runner(self.ROW))

    def test_container_rows_are_json_serialisable(self):
        rows = resources.capture_containers(["abc"], runner=self.runner(self.ROW))
        payload = dataclasses.asdict(rows[0])
        self.assertEqual(json.loads(json.dumps(payload)), jsonable(payload))


class SafetyTests(unittest.TestCase):
    def test_module_never_reads_process_state_or_reaches_out(self):
        source = (ROOT / "resources.py").read_text(encoding="utf-8")
        for forbidden in ("cmdline", "os.environ", "psutil", "urllib", "requests",
                          "socket", "os.setuid", "os.setgid"):
            self.assertFalse(forbidden in source, f"{forbidden} must not appear")

    def test_host_capture_path_never_shells_out(self):
        source = (ROOT / "resources.py").read_text(encoding="utf-8")
        start = source.index("def _run_docker_stats(")
        end = source.index("def main(")
        outside = source[:start] + source[end:]
        self.assertNotIn("subprocess.", outside)
        self.assertIn("subprocess.run", source[start:end])

    def test_docker_is_never_inspected_for_environment_data(self):
        source = (ROOT / "resources.py").read_text(encoding="utf-8")
        for forbidden in ('"inspect"', "'inspect'", "--env", "shell=True", "os.popen"):
            self.assertFalse(forbidden in source, f"{forbidden} must not appear")

    def test_module_only_reads_the_documented_sources(self):
        source = (ROOT / "resources.py").read_text(encoding="utf-8")
        for allowed in ("/proc/meminfo", "/proc/pressure/memory", "/proc/vmstat",
                        "/proc/stat", "/proc/loadavg", "shutil.disk_usage",
                        "os.cpu_count"):
            self.assertIn(allowed, source)


if __name__ == "__main__":
    unittest.main()
