# Polaron: mobile and disk native parsing locality update (2026-11-11)

## Why both platforms need optimization

The disk parser already filters low-value sources for CPU-intensive native parsing and uses `iter_files_from_part()` for its extraction shards. Mobile still queued every preserved bundle, causing many independent shard scans, even after 24-item batching. Existing forensic source counts and raw evidence must remain intact.

## Implemented in this patch

1. **Mobile shard-affinity priority** (`backend/app/services/forensic_serial_mobile.py`). The claim queue retains the existing six forensic priority tiers and additionally groups pending bundles by the immutable extracted part URI. This works on both newly seeded and already-pending work items. A 5,000-row bounded SQL update is committed after each batch. Priority is encoded into the existing PostgreSQL smallint field as `6 + tier * 4096 + (hashtext(part URI or path) & 4095)`; 6..24581 fits `smallint`, prevents double encoding on resume and is already sorted by the existing pending-priority index. A hash collision is harmless: it only reduces locality. SQLite companion files stay within their original evidence bundle. Completed/running/failed claims and source bytes are not changed. No forensic items are skipped.
2. **Disk per-shard DB lookup removal** (`backend/app/services/artifact_parse.py`). The previous code ran `sum(size_bytes)` over all `job_artifacts`, filtered by JSONB `extracted_part_uri`, for every shard. The parser already holds the claimed rows and can sum their requested sizes without SQL. The new log calls this **requested bytes**, not total shard bytes. Percent-of-full-shard is intentionally omitted rather than reporting an inaccurate number. The existing single-pass shard streaming, forensic-only queue filtering, checkpoint semantics and parser routing are preserved.
3. **Regression tests** (`backend/tests/test_unified_shard_affinity_20261111.py`), covering batch/idempotency, rollback switch, smallint safety, existing claims, and avoiding the expensive disk aggregate. Five focused tests pass in this environment. Full database, MinIO, iOS/Android source parsing, GPU OCR, and end-to-end throughput were **not** tested on a live deployment.

## Rollback and operation

- Set `MOBILE_SHARD_AFFINITY=false` in the effective mobile parsing worker environment to stop packing **new** pending claims. Previously packed pending priorities remain valid and will continue to order work; reverting every packed priority would require a controlled schema-aware migration, not a blind update.
- Deploy only when the current native parse stage has been safely paused. Do not stop an actively committing source write or an in-flight evidence transfer.
- Back up existing source files before overlaying the patch at `D:\project1\polaran2`.
- Rebuild/recreate the relevant mobile and disk parsing worker service images via the project launcher, ensuring workers actually use rebuilt images.
- Resume the SAME job and verify actual progress: persisted bundles/minute, source-read seconds, prefetch seconds, disk shard scans, database transaction age. Do not rely on UI percentage alone.
- **Do not** run `docker compose down -v`, `docker system prune --volumes`, delete MinIO storage, evidence, extracted-part manifests, or PostgreSQL case data.

## Expected results / limitations

The locality update is intended to reduce repeated opening of different compressed MinIO parts within a claim batch. It is not a proof of speedup. If most pending items are spread over a large number of parts, the maximum gain will be limited. Full single-pass parsing of *all* large mobile database members, bulk metadata-only artifact persistence and platform-specific parser eligibility scheduling require integration tests and a deliberate migration plan: neither is falsely claimed as complete by this patch.

The disk parser already has forensic eligibility filtering and shard-aware extraction. Replacing those with a generic mobile parser would regress coverage. Evidence completion (all inputs accounted for) remains different from content recovery (some deleted/overwritten sources may be unrecoverable).

## Verification commands

```powershell
cd D:\project1\polaran2
docker compose -p aetheris-forensic -f docker-compose.yml config --services
docker ps --format 'table {{.Names}}\t{{.Status}}'
```

PostgreSQL (use your case schema and correct stage id):

```sql
SELECT status, count(*) FROM pipeline_work_items
WHERE stage_run_id = '<stage-run-uuid>' GROUP BY status;

SELECT priority / 4096 AS approximate_priority_band, count(*)
FROM pipeline_work_items
WHERE stage_run_id = '<stage-run-uuid>' AND status='pending'
GROUP BY 1 ORDER BY 1;
```

The band query is approximate because encoded priorities include an offset of 6; it is for inspection, not forensic reporting.
