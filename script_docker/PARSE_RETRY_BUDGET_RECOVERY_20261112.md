# Polaron: parse-stage retry budget recovery

## Cause
The serial `parse` coordinator raises `StageWaiting('Parsing made no progress; remaining evidence claims must finish')` after ten minutes without a reduced pending count. In the previous progress-agent decision tree, that legitimate dependency wait was classified as `recover`, incrementing `recovery_dispatches`; after three such cycles it was marked `failed`, even though queued work still existed. The error text did not establish a corrupt evidence image or a failed parser.

## Code changes
- `backend/app/services/progress_agent.py`: classify parse-specific unfinished-claim `waiting` as `retry_dependency` rather than a new fault. Existing `retry_dependency` dispatch does not consume `recovery_dispatches`.
- `backend/app/services/forensic_serial_stages.py`: before yielding a quiet parse stage, run bounded stale-claim recovery and report pending/claimed counts. Preserve all committed parse states and never force completion.
- `backend/tests/test_progress_agent.py`: guard both parse wait and unrelated-stage classification.

The fix is not a guarantee of worker availability, file readability, or a particular processing rate. It does not silently skip remaining evidence.

## Deploy safely
1. Use Polaron's pause/checkpoint workflow and confirm there are no active native parsing workers or shard tasks for the affected job. The parent Celery logs should be quiet and task inspector should not show an active parse task for it.
2. Back up the three replaced source files in `D:\project1\polaran2`.
3. Overlay the patch's directory tree. Rebuild the parsing worker and progress-agent images using the same Compose file set used at startup, then recreate only the affected application workers.
4. Preserve PostgreSQL/MinIO/Redis and existing evidence objects. Do not run `docker compose down -v`, volume prune, or restart acquisition.

## Before clearing the recorded failure (read-only SQL, correct tenant schema)

```sql
SELECT id,stage,status,error,attempt,recovery_dispatches,total_items,
       completed_items,failed_items,skipped_items,task_id,heartbeat_at
FROM pipeline_stage_runs
WHERE job_id='d76f188e-a5fc-4227-b67a-1bb1a769bfeb' AND stage='parse';

SELECT parse_status,count(*)
FROM job_artifacts
WHERE job_id='d76f188e-a5fc-4227-b67a-1bb1a769bfeb'
GROUP BY parse_status ORDER BY parse_status;

SELECT sidecar,status,heartbeat_at,updated_at
FROM pipeline_sidecar_runs
WHERE job_id='d76f188e-a5fc-4227-b67a-1bb1a769bfeb'
  AND sidecar LIKE 'parse_shard_%';
```

Only when the patch is deployed, workers are confirmed stopped/idle, and the stage remains `failed` with the **exact** retry-budget error, use this guarded reset in the correct tenant schema. Review `RETURNING` output and commit only if the expected single row changes:

```sql
BEGIN;
UPDATE pipeline_stage_runs
SET status='waiting',error='Parse dependency recovery after watchdog patch',
    recovery_dispatches=0, task_id=NULL, cancel_requested_at=NULL,
    operation=NULL, operation_deadline=NULL,updated_at=NOW()
WHERE job_id='d76f188e-a5fc-4227-b67a-1bb1a769bfeb'
  AND stage='parse' AND status='failed'
  AND error='progressAgent retry budget exhausted'
RETURNING job_id,stage,status,recovery_dispatches;
-- COMMIT only after inspecting exactly one intended row; otherwise ROLLBACK.
COMMIT;
```

The progress agent can then dispatch the same stage. If the job has an explicit failed top-level status, use Polaron's job Resume/Retry operation to clear it. Do not bulk-update all `jobs` or all stages.

## Validate
- `docker logs --since 20m <progress-worker> | Select-String 'retry budget|Parsing made no progress'`
- In PostgreSQL, check that `recovery_dispatches` does not increase for the parse dependency wait.
- Check actual committed `parsed`, `skipped`, `no_parser`, `failed`, `pending`, `parsing` counts twice, ten minutes apart.
- If count remains static, examine Celery active workers and `parse_shard_*` heartbeats; do not increase retry limits or mark stage complete.

## Test status
Changed modules compile. Two new classification regressions pass. A broader existing suite has four assertion mismatches around unrelated live-stage cancellation behavior; those are not claimed as passing. No live PostgreSQL/MinIO or Docker integration test was performed.
