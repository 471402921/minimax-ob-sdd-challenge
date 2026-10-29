#!/usr/bin/env python3
"""Stdlib-only host resource admission for Harbor review pilots.

One question, answered from aggregate kernel counters: can this host run a
Harbor review pilot right now?

Sources (every one of them is injectable, so offline tests never touch the
real ``/proc`` tree):

===============================  ==========================================
``/proc/meminfo``                ``MemAvailable`` -> GiB (core signal)
``shutil.disk_usage(path)``      free GiB on the volume holding ``path``
                                 (core signal)
``/proc/pressure/memory``        ``some avg10`` (optional; *unknown* if
                                 absent, never ``0``)
``/proc/vmstat``                 ``pswpin`` / ``pswpout`` raw page counters
``/proc/stat``                   aggregate CPU jiffy counters
``/proc/loadavg``                1 minute load average
``os.cpu_count()``               logical CPU count
===============================  ==========================================

Contract the rest of the repository may rely on:

* **Fail closed.** A missing or unparsable *core* signal (memory, disk) and
  every present-but-non-numeric metric are hard failures. ``capture()``
  raises :class:`ResourceCaptureError` by default so the caller aborts;
  ``capture(..., strict=False)`` instead records the fault in
  ``Snapshot.errors`` and :meth:`AdmissionPolicy.evaluate` then rejects it.
* **Absent is unknown, not zero.** Optional signals that the kernel does not
  expose stay ``None`` (``Snapshot.unknowns``). Memory PSI is never reported
  as ``0.0`` just because ``/proc/pressure/memory`` is missing.
* **Never escalated.** No sudo, no setuid, no privileged syscall.
* **No process enumeration.** Per-process command lines and environments are
  never read, and no file outside the listed sources is opened.
* **No private files, no environment dumps, no network, no model/platform
  calls, no Docker.**
* **Safe output only.** Snapshots carry numbers, a UTC timestamp and short
  stable reason codes. No hostnames, no paths, no user data.

Typical use::

    from resources import AdmissionPolicy, capture, delta

    snapshot = capture(ROOT)                  # raises when the host is unusable
    reasons = AdmissionPolicy().evaluate(snapshot)
    if reasons:
        ...                                   # refuse to launch, reasons are safe
    later = capture(ROOT)
    growth = delta(snapshot, later)           # CPU busy % and swap deltas

``Snapshot.memory_available_gib`` is the field a runner should compare
against its reserved per-launch prediction budget.

Optional container sampling, never part of the admission decision::

    from resources import capture_containers

    rows = capture_containers(["my-pilot-runner"])   # docker stats --no-stream

``capture_containers`` accepts only the container identifiers the caller
spelled out, runs one bounded ``docker stats --no-stream --format json`` and
keeps ``Name``/``CPUPerc``/``MemUsage``/``PIDs``. It never calls
``docker inspect``, so environment variables, mounts and labels stay unread.
"""
import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

__all__ = [
    "AdmissionPolicy",
    "ContainerStats",
    "DEFAULT_POLICY",
    "Delta",
    "ResourceCaptureError",
    "Snapshot",
    "admits",
    "capture",
    "capture_containers",
    "delta",
    "evaluate",
]

KIB = 1024
GIB = 1024 ** 3

DOCKER_STATS_TIMEOUT = 10.0
CONTAINER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SIZE_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000 ** 2,
    "gb": 1000 ** 3,
    "tb": 1000 ** 4,
    "kib": 1024,
    "mib": 1024 ** 2,
    "gib": 1024 ** 3,
    "tib": 1024 ** 4,
}

MEMINFO_PATH = Path("/proc/meminfo")
PRESSURE_PATH = Path("/proc/pressure/memory")
VMSTAT_PATH = Path("/proc/vmstat")
STAT_PATH = Path("/proc/stat")
LOADAVG_PATH = Path("/proc/loadavg")

# Blocking faults (present but unusable). Any of these rejects the host.
E_MEMINFO_UNAVAILABLE = "meminfo_unavailable"
E_MEMINFO_MISSING_FIELD = "meminfo_no_memavailable"
E_MEMINFO_INVALID = "meminfo_invalid"
E_DISK_UNAVAILABLE = "disk_unavailable"
E_DISK_INVALID = "disk_invalid"
E_PSI_INVALID = "memory_psi_invalid"
E_VMSTAT_INVALID = "vmstat_invalid"
E_STAT_INVALID = "stat_invalid"
E_LOADAVG_INVALID = "loadavg_invalid"

# Non blocking gaps (source simply not exposed by this kernel).
U_PSI_UNKNOWN = "memory_psi_unknown"
U_SWAP_UNKNOWN = "swap_unknown"
U_CPU_UNKNOWN = "cpu_unknown"
U_LOADAVG_UNKNOWN = "loadavg_unknown"
U_CPU_COUNT_UNKNOWN = "cpu_count_unknown"
U_CLOCK_UNKNOWN = "clock_unknown"

_ERROR_REASONS = {
    E_MEMINFO_UNAVAILABLE: "memory_invalid: meminfo unreadable (fail closed)",
    E_MEMINFO_MISSING_FIELD: "memory_invalid: meminfo has no MemAvailable (fail closed)",
    E_MEMINFO_INVALID: "memory_invalid: meminfo MemAvailable not numeric (fail closed)",
    E_DISK_UNAVAILABLE: "disk_invalid: disk usage unavailable (fail closed)",
    E_DISK_INVALID: "disk_invalid: disk free space not numeric (fail closed)",
    E_PSI_INVALID: "metric_invalid: memory psi avg10 not numeric (fail closed)",
    E_VMSTAT_INVALID: "metric_invalid: vmstat swap counters not numeric (fail closed)",
    E_STAT_INVALID: "metric_invalid: cpu counters not numeric (fail closed)",
    E_LOADAVG_INVALID: "metric_invalid: load average not numeric (fail closed)",
}

_CORE_ERRORS = (
    E_MEMINFO_UNAVAILABLE,
    E_MEMINFO_MISSING_FIELD,
    E_MEMINFO_INVALID,
    E_DISK_UNAVAILABLE,
    E_DISK_INVALID,
)


class ResourceCaptureError(RuntimeError):
    """Raised when a host snapshot cannot be trusted (fail closed)."""


class _FieldMissing(Exception):
    """The metric is not exposed by this source; the value stays unknown."""


class _FieldInvalid(Exception):
    """The metric is exposed but not usable; the host is rejected."""


@dataclass(frozen=True)
class Snapshot:
    """One point-in-time view of the host. All values are safe numbers."""

    memory_available_gib: Optional[float] = None
    """``MemAvailable`` of ``/proc/meminfo`` in GiB (core signal)."""

    disk_free_gib: Optional[float] = None
    """Free space of the filesystem holding the checked path, in GiB."""

    disk_total_gib: Optional[float] = None
    """Total size of that filesystem, in GiB."""

    memory_psi_avg10: Optional[float] = None
    """``some avg10`` of ``/proc/pressure/memory``; ``None`` means unknown."""

    swap_in_pages: Optional[int] = None
    """``pswpin`` raw counter of ``/proc/vmstat`` (pages)."""

    swap_out_pages: Optional[int] = None
    """``pswpout`` raw counter of ``/proc/vmstat`` (pages)."""

    cpu_busy_ticks: Optional[int] = None
    """Busy CPU jiffies of the aggregate ``cpu`` line of ``/proc/stat``."""

    cpu_total_ticks: Optional[int] = None
    """All CPU jiffies (busy + idle + iowait) of the same line."""

    load_avg_1m: Optional[float] = None
    """1 minute load average of ``/proc/loadavg``."""

    cpu_count: Optional[int] = None
    """Logical CPU count reported by ``os.cpu_count()``."""

    monotonic: Optional[float] = None
    """``time.monotonic()`` reading, used to measure the interval of two snapshots."""

    captured_at: Optional[str] = None
    """UTC ISO-8601 timestamp of the capture."""

    errors: tuple[str, ...] = ()
    """Blocking fault codes; any entry rejects the host."""

    unknowns: tuple[str, ...] = ()
    """Codes for signals this kernel did not expose; never blocking."""

    def to_dict(self) -> dict[str, Any]:
        """JSON friendly view of the snapshot (same shape as ``asdict``)."""
        return {
            "memory_available_gib": self.memory_available_gib,
            "disk_free_gib": self.disk_free_gib,
            "disk_total_gib": self.disk_total_gib,
            "memory_psi_avg10": self.memory_psi_avg10,
            "swap_in_pages": self.swap_in_pages,
            "swap_out_pages": self.swap_out_pages,
            "cpu_busy_ticks": self.cpu_busy_ticks,
            "cpu_total_ticks": self.cpu_total_ticks,
            "load_avg_1m": self.load_avg_1m,
            "cpu_count": self.cpu_count,
            "monotonic": self.monotonic,
            "captured_at": self.captured_at,
            "errors": list(self.errors),
            "unknowns": list(self.unknowns),
        }


@dataclass(frozen=True)
class Delta:
    """Difference between two snapshots taken on the same host."""

    elapsed_seconds: Optional[float] = None
    """Wall time between the snapshots, from their ``monotonic`` readings."""

    cpu_busy_percent: Optional[float] = None
    """CPU busy share of the interval; ``None`` when unknown or after a reset."""

    cpu_busy_ticks: Optional[int] = None
    """Busy jiffies consumed during the interval."""

    cpu_total_ticks: Optional[int] = None
    """All jiffies consumed during the interval."""

    swap_in_pages: Optional[int] = None
    """Pages swapped in during the interval."""

    swap_out_pages: Optional[int] = None
    """Pages swapped out during the interval."""

    swap_total_pages: Optional[int] = None
    """Sum of pages swapped in and out during the interval."""

    notes: tuple[str, ...] = ()
    """Short codes such as ``cpu_counter_reset`` or ``swap_unknown``."""

    def to_dict(self) -> dict[str, Any]:
        """JSON friendly view of the delta."""
        return {
            "elapsed_seconds": self.elapsed_seconds,
            "cpu_busy_percent": self.cpu_busy_percent,
            "cpu_busy_ticks": self.cpu_busy_ticks,
            "cpu_total_ticks": self.cpu_total_ticks,
            "swap_in_pages": self.swap_in_pages,
            "swap_out_pages": self.swap_out_pages,
            "swap_total_pages": self.swap_total_pages,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ContainerStats:
    """One ``docker stats --no-stream`` row for an explicitly named container.

    Only the fields an operator asked for are kept: name, CPU share, memory
    usage and process count. Environment variables, mounts, labels and any
    other inspection payload are never requested or stored.
    """

    container_id: str
    """The identifier the caller asked for."""

    name: str = ""
    """Container name as reported by Docker."""

    cpu_percent: Optional[float] = None
    """CPU share in percent."""

    mem_usage: Optional[str] = None
    """Raw memory usage string, e.g. ``"120MiB / 2GiB"``."""

    mem_used_gib: Optional[float] = None
    """Used memory in GiB."""

    mem_limit_gib: Optional[float] = None
    """Memory limit in GiB."""

    pids: Optional[int] = None
    """Number of processes inside the container."""

    error: Optional[str] = None
    """Short code when no row could be read for this container."""

    def to_dict(self) -> dict[str, Any]:
        """JSON friendly view of the container sample."""
        return {
            "container_id": self.container_id,
            "name": self.name,
            "cpu_percent": self.cpu_percent,
            "mem_usage": self.mem_usage,
            "mem_used_gib": self.mem_used_gib,
            "mem_limit_gib": self.mem_limit_gib,
            "pids": self.pids,
            "error": self.error,
        }


@dataclass(frozen=True)
class AdmissionPolicy:
    """Thresholds a host must meet before a pilot may be launched."""

    min_available_gib: float = 12.0
    """Minimum ``MemAvailable`` in GiB."""

    min_disk_gib: float = 150.0
    """Minimum free disk space in GiB."""

    max_memory_psi_avg10: float = 1.0
    """Maximum tolerated ``some avg10`` memory pressure; unknown PSI never fails."""

    def __post_init__(self) -> None:
        for name in ("min_available_gib", "min_disk_gib", "max_memory_psi_avg10"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
            object.__setattr__(self, name, number)

    def evaluate(self, snapshot: Snapshot) -> list[str]:
        """Return the reasons the host is refused; an empty list means admitted."""
        return _evaluate(snapshot, self)

    def admits(self, snapshot: Snapshot) -> bool:
        """True when the host satisfies this policy."""
        return not self.evaluate(snapshot)


DEFAULT_POLICY = AdmissionPolicy()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def capture(
    path: Path,
    *,
    strict: bool = True,
    meminfo_path: Path = MEMINFO_PATH,
    pressure_path: Path = PRESSURE_PATH,
    vmstat_path: Path = VMSTAT_PATH,
    stat_path: Path = STAT_PATH,
    loadavg_path: Path = LOADAVG_PATH,
    disk_usage: Callable[[Path], Any] = shutil.disk_usage,
    cpu_count: Callable[[], Optional[int]] = os.cpu_count,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _utcnow,
) -> Snapshot:
    """Sample the host that stores ``path``.

    ``strict=True`` (default) raises :class:`ResourceCaptureError` when a core
    signal is missing or a metric is present but not numeric, so the caller
    fails closed. ``strict=False`` returns the snapshot anyway and records the
    fault in ``Snapshot.errors``; :meth:`AdmissionPolicy.evaluate` then rejects
    the host.

    Every source is injectable, which is what keeps the tests offline.
    """
    errors: list[str] = []
    unknowns: list[str] = []

    memory_available_gib = _capture_memory(meminfo_path, errors)
    disk_free_gib, disk_total_gib = _capture_disk(path, disk_usage, errors)
    memory_psi_avg10 = _capture_psi(pressure_path, errors, unknowns)
    swap_in_pages, swap_out_pages = _capture_swap(vmstat_path, errors, unknowns)
    cpu_busy_ticks, cpu_total_ticks = _capture_cpu(stat_path, errors, unknowns)
    load_avg_1m = _capture_loadavg(loadavg_path, errors, unknowns)
    cores = _capture_cpu_count(cpu_count, unknowns)
    clock = _capture_monotonic(monotonic, unknowns)
    stamp = _capture_timestamp(now)

    snapshot = Snapshot(
        memory_available_gib=memory_available_gib,
        disk_free_gib=disk_free_gib,
        disk_total_gib=disk_total_gib,
        memory_psi_avg10=memory_psi_avg10,
        swap_in_pages=swap_in_pages,
        swap_out_pages=swap_out_pages,
        cpu_busy_ticks=cpu_busy_ticks,
        cpu_total_ticks=cpu_total_ticks,
        load_avg_1m=load_avg_1m,
        cpu_count=cores,
        monotonic=clock,
        captured_at=stamp,
        errors=tuple(dict.fromkeys(errors)),
        unknowns=tuple(dict.fromkeys(unknowns)),
    )

    if strict and snapshot.errors:
        raise ResourceCaptureError(
            "host resource capture failed: " + ", ".join(snapshot.errors)
        )
    return snapshot


def evaluate(snapshot: Snapshot, policy: Optional[AdmissionPolicy] = None) -> list[str]:
    """Reasons the host is refused; an empty list means admitted."""
    return _evaluate(snapshot, policy or DEFAULT_POLICY)


def admits(snapshot: Snapshot, policy: Optional[AdmissionPolicy] = None) -> bool:
    """True when the host satisfies ``policy``."""
    return not evaluate(snapshot, policy)


def delta(earlier: Snapshot, later: Snapshot) -> Delta:
    """CPU busy share and swap growth between two snapshots.

    The interval comes from the ``monotonic`` readings of the snapshots. A
    counter that went backwards (host reboot, container restart) is reported as
    unknown with a ``cpu_counter_reset`` / ``swap_counter_reset`` note instead
    of a bogus number.
    """
    notes: list[str] = []

    elapsed: Optional[float] = None
    if earlier.monotonic is not None and later.monotonic is not None:
        elapsed = later.monotonic - earlier.monotonic
        if elapsed <= 0:
            notes.append("clock_not_monotonic")
            elapsed = None
    else:
        notes.append("clock_unknown")

    busy_percent: Optional[float] = None
    busy_delta: Optional[int] = None
    total_delta: Optional[int] = None
    if None in (earlier.cpu_busy_ticks, earlier.cpu_total_ticks,
                later.cpu_busy_ticks, later.cpu_total_ticks):
        notes.append("cpu_unknown")
    else:
        busy_delta = int(later.cpu_busy_ticks) - int(earlier.cpu_busy_ticks)
        total_delta = int(later.cpu_total_ticks) - int(earlier.cpu_total_ticks)
        if total_delta <= 0 or busy_delta < 0:
            notes.append("cpu_counter_reset")
            busy_delta = None
            total_delta = None
        else:
            busy_percent = min(max(100.0 * busy_delta / total_delta, 0.0), 100.0)

    swap_in = _counter_delta(earlier.swap_in_pages, later.swap_in_pages,
                             "swap_in_unknown", "swap_counter_reset", notes)
    swap_out = _counter_delta(earlier.swap_out_pages, later.swap_out_pages,
                              "swap_out_unknown", "swap_counter_reset", notes)
    swap_total = swap_in + swap_out if swap_in is not None and swap_out is not None else None

    return Delta(
        elapsed_seconds=elapsed,
        cpu_busy_percent=busy_percent,
        cpu_busy_ticks=busy_delta,
        cpu_total_ticks=total_delta,
        swap_in_pages=swap_in,
        swap_out_pages=swap_out,
        swap_total_pages=swap_total,
        notes=tuple(dict.fromkeys(notes)),
    )


def _evaluate(snapshot: Snapshot, policy: AdmissionPolicy) -> list[str]:
    reasons: list[str] = []
    for code in snapshot.errors:
        reasons.append(_ERROR_REASONS.get(code, f"metric_invalid: {code}"))

    memory = snapshot.memory_available_gib
    if memory is None:
        if not any(code in _CORE_ERRORS for code in snapshot.errors):
            reasons.append("memory_unavailable: MemAvailable unknown (fail closed)")
    elif memory < policy.min_available_gib:
        reasons.append(
            f"memory_low: {memory:.1f} GiB available < {policy.min_available_gib:.1f} GiB required"
        )

    free = snapshot.disk_free_gib
    if free is None:
        if not any(code in _CORE_ERRORS for code in snapshot.errors):
            reasons.append("disk_unavailable: free space unknown (fail closed)")
    elif free < policy.min_disk_gib:
        reasons.append(
            f"disk_low: {free:.1f} GiB free < {policy.min_disk_gib:.1f} GiB required"
        )

    psi = snapshot.memory_psi_avg10
    if psi is not None and psi > policy.max_memory_psi_avg10:
        reasons.append(
            f"memory_psi_high: some avg10 {psi:.2f} > {policy.max_memory_psi_avg10:.2f} allowed"
        )
    return reasons


def _counter_delta(
    earlier: Optional[int],
    later: Optional[int],
    unknown_note: str,
    reset_note: str,
    notes: list[str],
) -> Optional[int]:
    if earlier is None or later is None:
        notes.append(unknown_note)
        return None
    value = int(later) - int(earlier)
    if value < 0:
        notes.append(reset_note)
        return None
    return value


def _read_text(path: Path) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _finite(raw: Any) -> Optional[float]:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _counter(raw: Any) -> Optional[int]:
    value = _finite(raw)
    if value is None or value < 0 or value != int(value):
        return None
    return int(value)


def _capture_memory(meminfo_path: Path, errors: list[str]) -> Optional[float]:
    text = _read_text(meminfo_path)
    if text is None:
        errors.append(E_MEMINFO_UNAVAILABLE)
        return None
    try:
        return _mem_available_gib(text)
    except _FieldMissing:
        errors.append(E_MEMINFO_MISSING_FIELD)
    except _FieldInvalid:
        errors.append(E_MEMINFO_INVALID)
    return None


def _mem_available_gib(text: str) -> float:
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "MemAvailable:":
            continue
        if len(parts) < 2:
            raise _FieldInvalid
        value = _finite(parts[1])
        if value is None or value < 0:
            raise _FieldInvalid
        if len(parts) > 2 and parts[2].lower() != "kb":
            raise _FieldInvalid
        return value * KIB / GIB
    raise _FieldMissing


def _capture_disk(
    path: Path,
    disk_usage: Callable[[Path], Any],
    errors: list[str],
) -> tuple[Optional[float], Optional[float]]:
    try:
        usage = disk_usage(Path(path))
    except (OSError, ValueError, TypeError, AttributeError):
        errors.append(E_DISK_UNAVAILABLE)
        return None, None
    try:
        total = float(usage.total)
        free = float(usage.free)
    except (AttributeError, TypeError, ValueError):
        errors.append(E_DISK_INVALID)
        return None, None
    if not math.isfinite(total) or not math.isfinite(free) or total < 0 or free < 0:
        errors.append(E_DISK_INVALID)
        return None, None
    return free / GIB, total / GIB


def _capture_psi(
    pressure_path: Path,
    errors: list[str],
    unknowns: list[str],
) -> Optional[float]:
    text = _read_text(pressure_path)
    if text is None:
        unknowns.append(U_PSI_UNKNOWN)
        return None
    try:
        return _psi_some_avg10(text)
    except _FieldMissing:
        unknowns.append(U_PSI_UNKNOWN)
    except _FieldInvalid:
        errors.append(E_PSI_INVALID)
    return None


def _psi_some_avg10(text: str) -> float:
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "some":
            continue
        for token in parts[1:]:
            key, sep, raw = token.partition("=")
            if key != "avg10":
                continue
            if not sep:
                raise _FieldInvalid
            value = _finite(raw)
            if value is None or value < 0:
                raise _FieldInvalid
            return value
    raise _FieldMissing


def _capture_swap(
    vmstat_path: Path,
    errors: list[str],
    unknowns: list[str],
) -> tuple[Optional[int], Optional[int]]:
    text = _read_text(vmstat_path)
    if text is None:
        unknowns.append(U_SWAP_UNKNOWN)
        return None, None
    values: dict[str, int] = {}
    invalid = False
    for line in text.splitlines():
        key, sep, raw = line.partition(" ")
        if not sep or key not in ("pswpin", "pswpout"):
            continue
        value = _counter(raw.strip())
        if value is None:
            invalid = True
            continue
        values[key] = value
    if invalid:
        errors.append(E_VMSTAT_INVALID)
    pswpin = values.get("pswpin")
    pswpout = values.get("pswpout")
    if pswpin is None:
        unknowns.append(U_SWAP_UNKNOWN)
    if pswpout is None:
        unknowns.append(U_SWAP_UNKNOWN)
    return pswpin, pswpout


def _capture_cpu(
    stat_path: Path,
    errors: list[str],
    unknowns: list[str],
) -> tuple[Optional[int], Optional[int]]:
    text = _read_text(stat_path)
    if text is None:
        unknowns.append(U_CPU_UNKNOWN)
        return None, None
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "cpu":
            continue
        numbers: list[int] = []
        invalid = False
        for raw in parts[1:]:
            value = _counter(raw)
            if value is None:
                invalid = True
                break
            numbers.append(value)
        if invalid or len(numbers) < 4:
            errors.append(E_STAT_INVALID)
            return None, None
        user, nice, system, idle, iowait = numbers[:5]
        irq = numbers[5] if len(numbers) > 5 else 0
        softirq = numbers[6] if len(numbers) > 6 else 0
        steal = numbers[7] if len(numbers) > 7 else 0
        busy = user + nice + system + irq + softirq + steal
        total = busy + idle + iowait
        return busy, total
    unknowns.append(U_CPU_UNKNOWN)
    return None, None


def _capture_loadavg(
    loadavg_path: Path,
    errors: list[str],
    unknowns: list[str],
) -> Optional[float]:
    text = _read_text(loadavg_path)
    if text is None:
        unknowns.append(U_LOADAVG_UNKNOWN)
        return None
    parts = text.split()
    if not parts:
        unknowns.append(U_LOADAVG_UNKNOWN)
        return None
    value = _finite(parts[0])
    if value is None or value < 0:
        errors.append(E_LOADAVG_INVALID)
        return None
    return value


def _capture_cpu_count(
    cpu_count: Callable[[], Optional[int]],
    unknowns: list[str],
) -> Optional[int]:
    try:
        raw = cpu_count()
    except Exception:  # pragma: no cover - os.cpu_count does not raise in practice
        raw = None
    value = _counter(raw) if raw is not None else None
    if value is None or value <= 0:
        unknowns.append(U_CPU_COUNT_UNKNOWN)
        return None
    return value


def _capture_monotonic(
    monotonic: Callable[[], float],
    unknowns: list[str],
) -> Optional[float]:
    try:
        value = float(monotonic())
    except Exception:
        unknowns.append(U_CLOCK_UNKNOWN)
        return None
    if not math.isfinite(value):
        unknowns.append(U_CLOCK_UNKNOWN)
        return None
    return value


def _capture_timestamp(now: Callable[[], datetime]) -> Optional[str]:
    try:
        stamp = now()
    except Exception:
        return None
    try:
        return stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (AttributeError, ValueError, OverflowError):
        return None


def _run_docker_stats(argv: Sequence[str], timeout: float) -> str:
    """Run one bounded ``docker stats`` call and return its stdout."""
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ResourceCaptureError("docker executable is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise ResourceCaptureError("docker stats timed out") from exc
    except OSError as exc:
        raise ResourceCaptureError("docker stats could not be executed") from exc
    if completed.returncode != 0:
        raise ResourceCaptureError(
            f"docker stats exited with status {completed.returncode}"
        )
    return completed.stdout or ""


def capture_containers(
    container_ids: Sequence[str],
    *,
    timeout: float = DOCKER_STATS_TIMEOUT,
    runner: Callable[[Sequence[str], float], str] = _run_docker_stats,
) -> list[ContainerStats]:
    """Sample the given containers with ``docker stats --no-stream``.

    The caller must name the containers explicitly; nothing is discovered and
    no other container is ever touched. Only ``Name``, ``CPUPerc``,
    ``MemUsage`` and ``PIDs`` are extracted - ``docker inspect`` is never
    called, so environment variables, mounts and labels stay unread. The call
    is bounded by ``timeout`` seconds and uses an argument vector (no shell).

    ``runner`` is injectable so offline tests never reach Docker.
    """
    ids: list[str] = []
    for raw in container_ids:
        candidate = str(raw).strip()
        if not CONTAINER_ID_RE.match(candidate):
            raise ValueError("container ids must be explicit identifiers without flags")
        ids.append(candidate)
    if not ids:
        return []
    seconds = float(timeout)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("timeout must be a positive number of seconds")
    argv = ["docker", "stats", "--no-stream", "--format", "json", *ids]
    return _parse_container_stats(runner(argv, seconds), ids)


def _parse_container_stats(output: str, ids: Sequence[str]) -> list[ContainerStats]:
    reported: dict[str, dict[str, Any]] = {}
    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise ResourceCaptureError("docker stats returned unparsable output") from exc
        if not isinstance(payload, dict):
            raise ResourceCaptureError("docker stats returned unparsable output")
        for key in ("Container", "ID"):
            value = payload.get(key)
            if value:
                reported.setdefault(str(value), payload)

    results: list[ContainerStats] = []
    for container_id in ids:
        row = None
        for key, candidate in reported.items():
            if key == container_id or key.startswith(container_id) or container_id.startswith(key):
                row = candidate
                break
        if row is None:
            results.append(ContainerStats(container_id=container_id, error="no_stats"))
            continue
        results.append(_container_stats(container_id, row))
    return results


def _container_stats(container_id: str, row: dict[str, Any]) -> ContainerStats:
    name = row.get("Name")
    usage = row.get("MemUsage")
    used = limit = None
    if isinstance(usage, str) and "/" in usage:
        left, _, right = usage.partition("/")
        used = _parse_size_gib(left)
        limit = _parse_size_gib(right)
    return ContainerStats(
        container_id=container_id,
        name=str(name) if name else "",
        cpu_percent=_parse_percent(row.get("CPUPerc")),
        mem_usage=usage if isinstance(usage, str) else None,
        mem_used_gib=used,
        mem_limit_gib=limit,
        pids=_counter(row.get("PIDs")),
        error=None,
    )


def _parse_percent(raw: Any) -> Optional[float]:
    text = str(raw or "").strip().rstrip("%")
    value = _finite(text)
    if value is None or value < 0:
        return None
    return value


def _parse_size_gib(raw: Any) -> Optional[float]:
    text = str(raw or "").strip()
    if not text:
        return None
    match = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([A-Za-z]*)", text)
    if not match:
        return None
    number = _finite(match.group(1))
    if number is None or number < 0:
        return None
    factor = SIZE_UNITS.get((match.group(2) or "b").lower())
    if factor is None:
        return None
    return number * factor / GIB


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    capture_fn: Callable[[Path], Snapshot] = capture,
) -> int:
    """Print one JSON sample of the host and the admission decision.

    Exit code 0 means admitted, 1 means refused, 2 means the sample could not
    be taken (fail closed).
    """
    parser = argparse.ArgumentParser(
        description="Sample host resources and evaluate the Harbor admission policy."
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=Path("."),
        help="path whose filesystem is checked for free space (default: .)",
    )
    parser.add_argument(
        "--min-available-gib",
        type=float,
        default=DEFAULT_POLICY.min_available_gib,
        help="minimum available memory in GiB (default: %(default)s)",
    )
    parser.add_argument(
        "--min-disk-gib",
        type=float,
        default=DEFAULT_POLICY.min_disk_gib,
        help="minimum free disk space in GiB (default: %(default)s)",
    )
    parser.add_argument(
        "--max-memory-psi-avg10",
        type=float,
        default=DEFAULT_POLICY.max_memory_psi_avg10,
        help="maximum tolerated memory PSI some avg10 (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    try:
        policy = AdmissionPolicy(
            args.min_available_gib, args.min_disk_gib, args.max_memory_psi_avg10
        )
    except ValueError as exc:
        parser.exit(2, f"{exc}\n")

    try:
        snapshot = capture_fn(Path(args.path))
    except ResourceCaptureError as exc:
        parser.exit(2, f"{exc}\n")

    reasons = policy.evaluate(snapshot)
    payload = {
        "snapshot": snapshot.to_dict(),
        "policy": {
            "min_available_gib": policy.min_available_gib,
            "min_disk_gib": policy.min_disk_gib,
            "max_memory_psi_avg10": policy.max_memory_psi_avg10,
        },
        "admission": {"ok": not reasons, "reasons": reasons},
    }
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if not reasons else 1


if __name__ == "__main__":
    raise SystemExit(main())
