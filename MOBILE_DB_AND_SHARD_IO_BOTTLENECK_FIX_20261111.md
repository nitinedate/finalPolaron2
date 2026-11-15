# Polaron — Mobile native parsing: database transaction / shard streaming optimization

## What was observed
- 179,028 mobile bundles; 64,214 -> 64,481 completed in ~35 minutes while OCR also ran.
- PostgreSQL showed eight workers in `idle in transaction` after SELECT `extracted_disk_uri,disk_source FROM jobs`.
- The prior 24-bundle claim change reduced queue queries but left each small source read opening a zstd tar shard individually.

## Changes in this patch
1. **Transaction isolation:** source reads use an independent schema-scoped AUTOCOMMIT read session. The reader releases its checked-out PostgreSQL connection before long MinIO zstd/tar streaming. The write session retains its existing per-bundle checkpoint and artifact transaction rules.
2. **Close idle transactions:** end the parent stage read transaction before starting worker threads; end per-worker context/stop-check transactions; commit the progress update before processing the next batch.
3. **Index-friendly lookup:** perform `(job_id,file_path)` exact-match lookup first; normalized and filename fallbacks remain for legacy paths. The existing `ix_job_artifacts_job_path` index supports this path.
4. **Small-source shard coalescing:** after the existing claim of up to 24 bundles, group eligible items by recorded `extracted_part_uri` and stream each group from its extraction shard once. The prefetch verifies *full original byte count and source SHA-256* before accepting bytes. Small evidence headers can be retained as short prefixes; files requiring their full bytes retain full content, subject to the memory caps. SQLite WAL/SHM/journal companions stay linked to the original database.
5. **Safe fallback:** large sources, derivations, missing part references, singletons and failed prefetches use the existing verified single-file path. No evidence is skipped, relabeled or silently promoted to recovered status. File/artifact completion remains checkpointed per bundle.
6. **Progress heartbeat:** long shard prefetch scans issue a periodic stage operation update, rather than appearing idle solely because no bundle has committed yet.

## Limits / risk controls
- This is a **bounded optimization**, not a full rewrite of every mobile parser into disk-style shard-stream pipelines. Some large/isolated source files still use the original read path. Performance improvement must be benchmarked on the actual mobile case.
- Prefix prefetch is only used when the parser's requested read fits within that verified prefix; any later larger read falls back to the original source reader.
- Each worker caches at most a bounded subset of the claimed files and clears the cache when the next claim is fetched.
- Prefetched data is ephemeral memory only; provenance, original MinIO evidence, and database checkpoint semantics are unchanged.
- No PostgreSQL schema migration or data deletion is required.
- Do not force-complete incomplete parse/coverage work. Missing keys and inaccessible/private sources must remain reported.

## Config (optional)
```dotenv
MOBILE_SHARD_PREFETCH=true
MOBILE_PARSE_CLAIM_BATCH=24
MOBILE_PREFETCH_MAX_MEMBER_BYTES=8388608
MOBILE_PREFETCH_MAX_BATCH_BYTES=67108864
```

Set these on the service that executes mobile native parsing, not only on the API. `MOBILE_SHARD_PREFETCH=false` disables the new shard prefetch without reverting the read-only database transaction fixes. The default max member is 8 MiB and the default claim-cache budget is 64 MiB per worker. For eight workers this is up to 512 MiB of additional cached evidence bytes, plus parser allocations. Memory is released between claim batches.

## Installation (Windows PowerShell)
1. Let currently active forensic writes complete, or pause through Polaron's **supported** job pause workflow. Do not kill a stage subprocess in the middle of artifact commits.
2. Back up the three updated Python files. Unzip the *patch* into `D:\project1\polaran2` preserving directory paths.
3. Rebuild the image used by native parsing and restart only the relevant pipeline worker(s) with your normal launcher. On installations using the shared `rag_new2-api` image, workers may need recreation to take the new image. Check the real service names first with:
```powershell
cd D:\project1\polaran2
docker compose -p aetheris-forensic -f docker-compose.yml config --services
```
4. Resume the **same** case. Do not delete PostgreSQL volumes, MinIO objects, acquisition backups, or `pipeline_work_items`.
5. Validate the throughput using the same stage card across two time intervals; inspect the worker's live logs and `pg_stat_activity` for transactions that remain idle for minutes. Verify all artifacts and source-coverage counts remain stable when the stage completes.

## PostgreSQL diagnostics
```sql
SELECT pid,application_name,state,now()-xact_start AS transaction_age,
       now()-state_change AS idle_duration,wait_event_type,wait_event,
       LEFT(query,140) AS last_query
FROM pg_stat_activity
WHERE datname=current_database() AND state='idle in transaction'
ORDER BY xact_start;
```

## Validation performed
- Ten isolated regression tests: queue-claim integrity, unstarted claim release, bounded same-part grouping, duplicate/large/derived exclusions, verified fallback and explicit opt-out.
- Python syntax compilation of all changed modules.
- Archive integrity checks after packaging.

**Not validated here:** live PostgreSQL transaction timing, MinIO throughput, end-to-end iOS backup parsing speed, or exact throughput gains. Those require the user's running Docker/MinIO environment and evidence.
