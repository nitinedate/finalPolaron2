"""Container-aware CPU budget and non-blocking host pressure samples.

The original planner used os.cpu_count(), which reports all host CPUs inside
some Docker containers even when cgroup cpu.max permits only a few.  This
module is dependency-free so it can be called before torch/numpy are imported.
It is an admission/diagnostic input; Docker CPU quota is the hard ceiling.
"""
from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path

_CPU_SAMPLE_LOCK = threading.Lock()
_CPU_SAMPLE: tuple[float, int, int] | None = None


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def _cpuset_count(text: str) -> int | None:
    if not text:
        return None
    count = 0
    try:
        for element in text.split(","):
            span = element.strip().split("-", 1)
            low = int(span[0])
            high = int(span[1]) if len(span) == 2 else low
            if low < 0 or high < low:
                return None
            count += high - low + 1
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def cgroup_quota_cores(*, root: str = "/sys/fs/cgroup") -> float | None:
    """Read cgroup v2 cpu.max or legacy v1 CFS CPU quota."""
    raw = _read_text(f"{root}/cpu.max").split()
    if len(raw) >= 2 and raw[0] != "max":
        try:
            quota, period = int(raw[0]), int(raw[1])
            if quota > 0 and period > 0:
                return quota / period
        except ValueError:
            pass
    for base in (f"{root}/cpu", root):
        try:
            quota = int(_read_text(f"{base}/cpu.cfs_quota_us"))
            period = int(_read_text(f"{base}/cpu.cfs_period_us"))
            if quota > 0 and period > 0:
                return quota / period
        except ValueError:
            continue
    return None


def effective_cpu_count(*, root: str = "/sys/fs/cgroup") -> int:
    """Conservative CPU count accounting for affinity, cpuset and CFS quota.

    Floor fractional quotas for stable independent CPU-bound processes.
    A quota under one full core still permits one process.
    """
    limits = [max(1, int(os.cpu_count() or 1))]
    try:
        limits.append(max(1, len(os.sched_getaffinity(0))))
    except (AttributeError, OSError):
        pass
    cpuset = _cpuset_count(_read_text(f"{root}/cpuset.cpus.effective"))
    if cpuset is None:
        cpuset = _cpuset_count(_read_text(f"{root}/cpuset/cpuset.cpus"))
    if cpuset is not None:
        limits.append(cpuset)
    quota = cgroup_quota_cores(root=root)
    if quota is not None:
        limits.append(max(1, math.floor(quota)))
    return max(1, min(limits))


def effective_memory_mb(total: int, available: int, *, root: str = "/sys/fs/cgroup") -> tuple[int, int]:
    """Constrain /proc/meminfo to the container limit when present."""
    max_raw = _read_text(f"{root}/memory.max")
    used_raw = _read_text(f"{root}/memory.current")
    if not max_raw or max_raw == "max":
        max_raw = _read_text(f"{root}/memory/memory.limit_in_bytes")
        used_raw = _read_text(f"{root}/memory/memory.usage_in_bytes")
    try:
        cap_mb = int(max_raw) // (1024 * 1024)
        used_mb = int(used_raw) // (1024 * 1024)
    except (ValueError, TypeError):
        return total, available
    # Ignore near-infinite cgroup memory.max values.
    if cap_mb < 1 or cap_mb >= total:
        return total, available
    return max(1, cap_mb), min(available, max(0, cap_mb - used_mb))


def host_cpu_utilization_percent(*, min_interval_sec: float = 0.4) -> float | None:
    """Non-blocking overall CPU usage from /proc/stat (not load average).

    Returns None until a real sample interval has elapsed; the caller must
    not treat that as zero utilization.  Works inside normal Linux/WSL Docker.
    """
    global _CPU_SAMPLE
    try:
        line = _read_text("/proc/stat").splitlines()[0].split()
        if line[0] != "cpu" or len(line) < 5:
            return None
        ticks = [int(v) for v in line[1:]]
        total = sum(ticks)
        idle = ticks[3] + (ticks[4] if len(ticks) > 4 else 0)
    except (ValueError, IndexError):
        return None
    now = time.monotonic()
    with _CPU_SAMPLE_LOCK:
        previous = _CPU_SAMPLE
        if previous is None or now - previous[0] >= min_interval_sec:
            _CPU_SAMPLE = (now, total, idle)
        if previous is None or now - previous[0] < min_interval_sec:
            return None
        dt, di = total - previous[1], idle - previous[2]
        if dt <= 0:
            return None
        return max(0.0, min(100.0, (1.0 - di / dt) * 100.0))


def target_percent() -> int:
    try:
        return min(90, max(20, int(os.getenv("CPU_SAFE_TARGET_PCT", "50"))))
    except ValueError:
        return 50


def target_parallel_cores(visible_cpus: int) -> int:
    """Advisory CPU processes per service (in addition to Docker hard quota)."""
    return max(1, math.floor(max(1, visible_cpus) * target_percent() / 100.0))
