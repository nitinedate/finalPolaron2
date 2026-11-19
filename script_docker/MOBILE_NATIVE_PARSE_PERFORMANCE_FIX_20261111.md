# Polaron mobile native parsing queue performance fix (2026-11-11)

## Symptom
Stage 5 Native forensic parsing: 64,214 / 179,028 bundles in 3h 11m, while stage 7 GPU OCR runs separately. This is not evidence that the OCR worker is causing the parse slowdown.

## Root causes found in source
- Each mobile bundle was claimed by a separate `SELECT ... FOR UPDATE SKIP LOCKED`, `UPDATE`, and transaction, even for metadata-only artifacts.
- Each bundle repeated `pipeline_should_stop()`, which can open an additional readonly database session when checking job stop flags.
- Per-item inventory upserts and parser-specific evidence reads may remain costly; their true contribution requires profiling.
- The disk native parser already has a queue-priority index and parallel parse shards, but the mobile pipeline did not batch its queue claims.

## Change
- New `_claim_work_batch` fetches up to 24 pending bundles in priority order with `SKIP LOCKED` in one SQL statement; supports 1-64 via MOBILE_PARSE_CLAIM_BATCH.
- Processing stays independent per bundle, maintaining the existing per-bundle commit and forensic provenance.
- Pause detection is throttled to once per second per worker between bundle completions; can be set 0.2-5 seconds with MOBILE_STOP_POLL_SECONDS. Long-running parsers still use the existing internal stop checks.
- Unstarted, preclaimed bundles are returned to pending during normal interruption. A worker crash remains recoverable through existing stage restart claim-recovery.
- SQLite -wal/-shm/-journal companions, forensic fallback, deleted-data recovery and per-item failures remain intact.

## Deployment
1. Do NOT replace code during an active parse or extraction; wait for the stage to finish or use Polaron's explicit Pause workflow at a safe checkpoint.
2. Back up existing file backend/app/services/forensic_serial_mobile.py.
3. Overlay the small ZIP at D:\project1\polaran2, preserving folder paths.
4. Rebuild/recreate the application service and the worker service that runs mobile forensic serial parsing. For this installation, use the existing script_docker/start_docker_local.cmd launcher because it handles shared Docker services.
5. Use Polaron's Resume workflow to continue the same job. Do not reset or delete stage work tables or evidence.
6. Check progress and worker logs; verify parsed artifact counts and outstanding work items before accepting 100%.

## Settings (optional)
MOBILE_PARSE_CLAIM_BATCH=24
MOBILE_STOP_POLL_SECONDS=1.0

## Validation
- 9 focused local tests passed, including batch-claim SQL shape, unstarted-claim release and preservation of parser/sidecar paths.
- Python source compiled successfully.
- Full integration tests were not available locally because psycopg2 was absent.
- Live runtime performance and throughput improvement on the user's E01/iOS dataset have NOT been measured; actual speedup is not guaranteed.

## Further performance work (separate)
Use actual SQL statement timings, time per plugin, MinIO read latency, number of unclassified 4KiB header probes, and PostgreSQL transaction counts to identify the next bottleneck. Batch inventory upserts only after a transactional test that proves no lost artifact states on worker failure.
