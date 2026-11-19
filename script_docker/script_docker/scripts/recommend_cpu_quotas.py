#!/usr/bin/env python3
"""Print per-container Compose .env quotas for an enforceable host budget.

No files are changed. Copy output into the *existing* project .env only after
checking the same physical host also runs the mobile and vulnerability stacks.
Example: python scripts/recommend_cpu_quotas.py --host-cpus 16 --target-percent 50
"""
from __future__ import annotations

import argparse
import os

# Relative shares: no service is starved, disk/parse receive the majority.
FORENSIC = {
    "NEO4J": .5, "API": 1.0, "DISK": 2.5, "PARSE": 1.5,
    "QUALITY": .5, "RAG_GPU": .5, "OCR_DOCUMENTS": .75,
    "OCR_GPU": .5, "REPORT": .25, "AGENT": .25,
    "PROGRESS": .35, "BEAT": .15, "RAG_PREP": .5,
}
VULN = {"API": .5, "WORKER": .75}


def recommendations(host_cpus: int, target_pct: int, share: float = 0.8) -> dict[str, float]:
    # Hold back 20% of the target for PostgreSQL, MinIO, Redis, host Ollama,
    # scanner containers, other stacks and the desktop. Not a full-host hard cap.
    total_budget = max(1.0, host_cpus * target_pct / 100 * share)
    weights = {**{f"FORENSIC_CPU_{name}_CPUS": value for name, value in FORENSIC.items()},
               **{f"VULN_CPU_{name}_CPUS": value for name, value in VULN.items()}}
    weight_sum = sum(weights.values())
    return {key: round(max(0.05, total_budget * weight / weight_sum), 2)
            for key, weight in weights.items()}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host-cpus", type=int, default=os.cpu_count() or 8)
    p.add_argument("--target-percent", type=int, default=50)
    a = p.parse_args()
    if not 1 <= a.host_cpus <= 512 or not 20 <= a.target_percent <= 90:
        p.error("--host-cpus must be 1..512, --target-percent 20..90")
    plan = recommendations(a.host_cpus, a.target_percent)
    print(f"# Host {a.host_cpus} logical CPU(s); target {a.target_percent}% and 20% service reserve")
    print("# Copy to your existing .env. All workloads on the same host must share a budget.")
    print("CPU_SAFE_MODE_ENABLED=true")
    print(f"CPU_SAFE_TARGET_PCT={a.target_percent}")
    print("CPU_NATIVE_THREADS=1")
    print("RESOURCE_GOVERNOR_FAIL_OPEN_ON_UNAVAILABLE=false")
    for key, value in plan.items():
        print(f"{key}={value:.2f}")
    print(f"# Scheduled forensic+central-vuln CPU ceiling: {sum(plan.values()):.2f} CPUs")


if __name__ == '__main__':
    main()
