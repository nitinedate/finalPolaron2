#!/usr/bin/env python3
"""Evidence-safe CPU pressure report: read-only host / Docker diagnostics.

Run from the machine where Docker is running:
  python scripts/diagnose_cpu_pressure.py --samples 5 --interval 2 --output cpu-report.json

This script never reads evidence files, images, case data, environment
variables or container logs (which can contain secrets).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def _docker_stats() -> list[dict]:
    try:
        p = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                           capture_output=True, text=True, timeout=20, check=True)
        return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return [{"diagnostic_error": str(exc)[:160]}]


def _cpu_value(row: dict) -> float:
    try:
        return float(str(row.get("CPUPerc", "0")).rstrip("%"))
    except (TypeError, ValueError):
        return 0.0


def _host_busy() -> float | None:
    # psutil is optional; on Windows this is more precise than load average.
    try:
        import psutil
        return round(float(psutil.cpu_percent(interval=None)), 1)
    except (ImportError, AttributeError):
        return None


def collect(samples: int, interval: float) -> list[dict]:
    output = []
    for i in range(samples):
        rows = _docker_stats()
        rows.sort(key=_cpu_value, reverse=True)
        sample = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "host_cpu_busy_percent": _host_busy(),
            "logical_cpus": os.cpu_count(),
            "containers_by_cpu": [{
                "name": item.get("Name", ""),
                "cpu_percent_of_one_core": item.get("CPUPerc", ""),
                "memory_usage_limit": item.get("MemUsage", ""),
                "pids": item.get("PIDs", ""),
                **({"error": item["diagnostic_error"]} if "diagnostic_error" in item else {}),
            } for item in rows],
        }
        output.append(sample)
        print(f"Sample {i+1}/{samples}: host CPU={sample['host_cpu_busy_percent']}%", flush=True)
        for item in sample["containers_by_cpu"][:8]:
            print(f"  {item['name'][:32]:32} CPU={item['cpu_percent_of_one_core']:>7} "
                  f"mem={item['memory_usage_limit'][:24]} pids={item['pids']}")
        if i < samples - 1:
            time.sleep(interval)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--output", type=Path, default=Path("cpu-pressure-report.json"))
    args = parser.parse_args()
    if not 1 <= args.samples <= 60 or not 0.2 <= args.interval <= 120:
        parser.error("samples must be 1..60; interval must be 0.2..120 seconds")
    samples = collect(args.samples, args.interval)
    args.output.write_text(json.dumps({
        "notice": "CPU percent in docker stats may exceed 100% for multicore containers; not total-host CPU.",
        "samples": samples,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Saved read-only CPU diagnostics to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
