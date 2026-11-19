# Polaron CPU saturation and responsiveness repair — source review and deployment handoff

**Date:** 19 November 2026  
**Source:** `polaron_forensic_ocr_scanner_2026-11-19_FULL_SOURCE.zip` (the previously delivered codebase).  
**Scope:** disk E01/native parse, OCR document search, media review, RAG preparation, central vulnerability dispatcher and on-site laptop Greenbone.

## What is conclusively wrong in the source

1. The forensic `worker-disk` Compose command explicitly starts eight Celery processes and consumes `disk-build,disk-inventory`. The startup script only matched `disk-build` or `report-gen`, so **the combined disk queue bypassed its concurrency rewrite**. The worker could run eight jobs despite a smaller capacity plan.
2. On the same physical host the disk worker permitted 12 CPU cores, the parse worker eight, and the new OCR document worker four, **without one combined CPU quota**. Further RAG, OCR, reporting, SQL and background workers lacked explicit CPU limits. Multiple container quotas were additive and their ceilings were not host-aware.
3. `host_capacity.probe_host()` and other native/serial workers used `os.cpu_count()` and `/proc/meminfo`, which often expose host totals even inside containers with smaller CFS CPU and memory limits. The planner could therefore overestimate available resources.
4. Several native libraries could use their own BLAS/OpenMP/Tesseract thread pools **per Celery process**. OCR/media review and independent RAG preparation created additional bounded pools without an overall processor budget.
5. The adaptive semaphore relied on load average, temperature and RAM. Load average is not the same as instantaneous percentage busy; some containers do not expose temperature. On some paths, loss of the shared Redis coordinator was explicitly configured to **fail open** for CPU-heavy extraction.

These are **confirmed code-level oversubscription causes**. The specific process responsible for the user's 100% CPU incident cannot be identified from source alone. That would require a capture of `docker stats`, host CPU utilization, throttling, and process names while the slowdown occurs. No actual production host or forensic disk was accessed during this change.

## Repairs implemented

| Layer | What changed |
|---|---|
| Startup | Recognizes the combined `disk-build,disk-inventory` queue; reduces every Celery worker's actual `--concurrency` using its effective cgroup CPU count and configured CPU target. |
| Native threads | Sets/caps OpenMP, OpenBLAS, MKL, BLIS, NumExpr, TensorFlow, Rayon, Tesseract and tokenizers before dynamic GPU/torch initialization. Default one native CPU thread per Celery process. |
| Dynamic capacity | `cpu_budget.py` reads cgroup v2 `cpu.max`, effective CPU sets and memory limits with v1 fallback. `_host_snapshot()` adds non-blocking actual host CPU busy percentage and dynamically shrinks heavy resource admission when hot. |
| Disk/TSK | The TSK directory reader count respects effective cgroup CPU budget. Serial CPU process pools and other disk-local loops no longer derive unconstrained limits from host-wide CPU counts. Original evidence, extraction checkpoints and forensic selection remain unchanged. |
| OCR/media and RAG | Document search, Tesseract media review, and RAG CPU prep use the container CPU budget. OCR extraction remains separate from disk and scanner queues. |
| Forensic Compose | 13 services now have individual CFS CPU ceilings, with safe defaults totaling **9.25 logical CPU cores** across these services. Includes fixes to duplicate `cpus` declaration in OCR documents worker. |
| Central vulnerability Compose | Central API and Nessus dispatcher capped to **1.25 logical CPU cores** total by default; two independent laptop-scanner jobs remain assignable. |
| Laptop scanner | Greenbone `gvmd`, `openvasd`, `ospd-openvas`, `pg-gvm` and scanner-agent CPU ceilings total **4.15 logical CPU cores** per laptop by default. Reduced intra-laptop scan/IP/check fan-out; keep `MAX_CONCURRENT_SCAN_JOBS=2`. Two **separate laptops on separate networks** operate independently. |
| Redis/CPU admission | `RESOURCE_GOVERNOR_FAIL_OPEN_ON_UNAVAILABLE=false` by default. CPU-heavy forensic stages return a resource coordinator failure instead of starting an ungoverned heavy job if Redis is unreachable. Preserve retry/checkpoint state; do not declare processing complete. |

### IMPORTANT: 50% is a target, not a guaranteed total-host limit

`CPU_SAFE_TARGET_PCT=50` is used to size and pace tasks. Docker `cpus` limits are the hard **per-container** ceilings. A physical host's CPU utilization can still exceed 50% if PostgreSQL, MinIO, Ollama, the OS, the Mobile stack, another Docker project, or another user process consumes cores. The default forensic (9.25 cores) + central-vuln (1.25 cores) caps sum to **10.5 CPUs** only when all are fully utilized; on a 24-logical-core machine, this is at most ~44% of nominal logical CPU capacity *for those capped services only*. On an 8- or 16-core host, use the host-specific quota calculation below. Scanner laptop quotas apply to their own hardware and should not be added to the server's total unless scanner runs on that server.

To calculate a lower host-specific set of quotas without changing any project files:

```sh
python scripts/recommend_cpu_quotas.py --host-cpus 16 --target-percent 50
```

The script prints `FORENSIC_CPU_*_CPUS` and `VULN_CPU_*_CPUS` values to add to the **existing** `.env`, preserving all secrets and case configuration. It intentionally leaves some CPU headroom for databases/background/host work; include other projects in the operator's CPU plan. For a real production ceiling, enforce host-wide Docker Desktop/VM limits or cgroups for all stacks, not only these two Composes. Do not set impractically low per-service quotas if they prevent watchdog/heartbeat tasks from running.

## How to measure the exact CPU consumer on the affected host

On the **server where Docker runs**, during a slow job (do not stop the forensic source):

```sh
docker stats --no-stream
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs --tail=150 worker-disk worker-parse worker-ocr-documents worker-progress
```

With Python available, the included helper captures repeated CPU snapshots and writes an audit-friendly JSON report without inspecting evidence or container secrets:

```sh
python scripts/diagnose_cpu_pressure.py --samples 5 --interval 2 --output cpu-pressure-report.json
```

**On Windows PowerShell:**

```powershell
docker stats --no-stream
docker compose -f docker-compose.yml ps
py -3 scripts/diagnose_cpu_pressure.py --samples 5 --interval 2 --output cpu-pressure-report.json
```

(`py -3` requires Python installed; `docker stats` does not.) `docker stats` displays percentages relative to **one** CPU, so a multi-core container can show more than 100%. Compare its usage to the logical CPU quota, host CPU busy percentage and other active containers, not to a simplistic 100% per-container threshold. On Linux, inspect `cpu.stat` under the matching cgroup for throttling if performance is still low at a fixed CPU quota; collect slow I/O stats with `iostat -xz 2` when available. A process blocked on network/FUSE reads may look hung with **low** CPU; a CPU cap will not repair a broken client share, so the separate resumable remote E01 staging fix remains necessary.

## Safe rollout procedure

1. **Preserve forensic evidence and case state.** Back up the project `.env`, database, job manifests and staging indexes. Avoid restarting workers with active tasks unless they are checkpointed; do not force jobs to 100%, destroy the transfer `.partial` file or delete extracted artifacts.
2. Overlay the **CPU patch ZIP** on the previously updated Polaron full source. It includes all modified backend/source/Compose files, updated example env, tests and this handoff. Do not replace the actual `.env` with `.env.example`.
3. Keep `RESOURCE_GOVERNOR_REDIS_URL` reachable from forensic/mobile services sharing one host, and keep `RESOURCE_GOVERNOR_GROUP` identical when physical CPU/GPU is shared. Redis failure now fails heavy CPU admission closed; verify reachability before resuming ingestion.
4. Validate `docker compose -f docker-compose.yml config --quiet`. For the central vulnerability service, validate with its normal Compose profiles and `--project-directory .`; laptop scanner uses a separate Compose project on each laptop. YAML parsing and explicit duplicate-key checks verified these three edited Compose files; this offline container had **no Docker engine**, so live Compose resolution and container recreation were not run.
5. At a safe maintenance point, recreate affected worker containers to apply **new environment variables, startup scripts and CPU quotas**. Example from repository root:

```sh
docker compose -f docker-compose.yml up -d --no-deps --force-recreate api worker-disk worker-parse worker-quality worker-rag-gpu worker-ocr-gpu worker-ocr-documents worker-rag-prep worker-progress worker-report worker-agent worker-beat
```

6. Start a **small test image** first, and confirm extraction counts, SHA-256, checkpoints, no 100%-CPU host saturation, and that the UI continues to respond. Start OCR and two **different laptop** scan jobs next; verify both run independently with no cross-laptop claim or evidence mutation. Monitor transfer progress separately from TSK filesystem-read progress.
7. Measure throughput. If CPU is low but extraction is slower than expected, check I/O latency and DB lock contention before raising quotas. Increase one worker CPU quota/parallelism knob at a time only after observing headroom.

## Tests and limitations

- 63 selected regression tests pass: cgroup-aware CPU/memory quota detection, non-blocking CPU sample, pressure-aware semaphores, safe capacity plan, Celery disk queue rewrite, native BLAS thread clamp, media OCR worker cap, prior TSK inventory, prior WAN disk staging and OCR document search tests.
- Python syntax compile and YAML parse succeed. Docker Compose CLI and a full installation test are not available in this offline review.
- Some unrelated historical tests cannot be collected without the project's `zstandard` or `psycopg2` dependencies; a legacy media-review test refers to symbols removed from the current source. This is *not* a full green test-suite claim.
- No production E01 read, GPU benchmark, 2-laptop Greenbone scan, or 50%-host-CPU performance measurement was performed. Confirm results on the host and adjust quotas to its actual size.
