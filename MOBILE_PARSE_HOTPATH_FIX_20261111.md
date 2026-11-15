# Polaron mobile native parsing: hot-path source lookup + bounded shard prefetch

## Diagnosis
The supplied PostgreSQL screenshot shows `idle` sessions with null transaction_age, not `idle in transaction`. This is an improvement. It does not establish whether the remaining bottleneck is source I/O, parsing or persistence. The previous source path repeatedly selected `job_artifacts` and `jobs` when reading a file from its own already-claimed work payload. Prefetch could also sequentially scan every eligible shard in one claim before any bundle was processed.

## Implementation
- Uses the **already-claimed immutable source metadata** (exact path, extracted-part URI, length and SHA-256) to read and independently verify an extracted source without PostgreSQL lookup. Missing metadata or derived evidence always falls back to the original reader.
- Direct reader rejects size and SHA-256 mismatches. Missing bytes remain missing, not fabricated. Read caps and special derived evidence retain prior behavior.
- Limits shard prefetch to two highest-reuse shards per claim (configurable). This prevents one claim from triggering numerous full-archive scans before any item is committed.
- Adds per-worker cumulative `mobile-parse-perf` logs every 25 completed bundles: prefetch time, parser time, database commit time, direct reads, and database fallbacks.
- Existing work ownership, bundle checkpoint commits, evidence data, and OCI volumes remain unchanged.

## Suggested configuration
```
MOBILE_SHARD_PREFETCH=true
MOBILE_PARSE_CLAIM_BATCH=24
MOBILE_PREFETCH_MAX_SHARDS=2
MOBILE_PREFETCH_MIN_GROUP=2
```
For diagnosis, `MOBILE_SHARD_PREFETCH=false` provides an A/B comparison without changing evidence artifacts.

## Safe deployment
Do not replace code while a mobile stage is actively committing. Pause the case using the application's supported checkpoint workflow; extract the patch into the project root and rebuild only the worker image(s) using the project's existing Docker Compose launcher. Resume the existing job. Do not remove PostgreSQL or MinIO volumes, evidence, or checkpoints.

## Validation and remaining uncertainty
Python syntax compilation and six isolated direct-reader safety tests passed. Live PostgreSQL, S3, zstd and mobile parser integration was **not** run in this environment. The direct-lookup fix removes SQL overhead but cannot eliminate repeated decompression of large shards; timings will show how much it helps. Check `mobile-parse-perf` logs and compare completed-bundle rates over equal time intervals. If prefetch_s dominates, test `MOBILE_SHARD_PREFETCH=false` or lower `MOBILE_PREFETCH_MAX_SHARDS=1` after a safe pause.

Do not use `pg_terminate_backend`, purge Celery tasks or force a stage to 100% to address slowness.
