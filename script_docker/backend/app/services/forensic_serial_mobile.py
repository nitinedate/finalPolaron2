"""Mobile native parse/recovery adapters with claimed evidence bundles."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque

from app.db.session import firm_session, firm_session_readonly
from app.db.sql_helpers import execute, fetchall, fetchone
from app.services.forensic_priority_evidence import bundle_priority
from app.services.fast_forensic_policy import fast_forensic_enabled, metadata_only_bundle
from app.services.forensic_serial_policy import bounded_map, db_parallel_slots, parallelism, stage_context
from app.services.mobile_forensic.discovery import inventory_from_job_artifacts
from app.services.mobile_forensic.models import InventoryItem
from app.services.mobile_forensic.plugins import ParseContext, get_plugin_registry
from app.services.mobile_forensic.storage import (
    _dumps,
    ensure_mobile_case_schema,
    persist_source_manifest,
    start_analysis_run,
    update_analysis_run,
)

SIDECARS = ("-wal", "-shm", "-journal")
# Small, bounded batch claims reduce repeated PostgreSQL queue round trips.
# Each bundle still commits its own results and completion marker atomically.
MOBILE_PARSE_CLAIM_BATCH = max(1, min(int(os.getenv("MOBILE_PARSE_CLAIM_BATCH", "256")), 512))
MOBILE_STOP_POLL_SECONDS = max(0.2, min(float(os.getenv("MOBILE_STOP_POLL_SECONDS", "1.0")), 5.0))
# Pack the existing 0..5 forensic priority into the high bits of smallint and
# a deterministic extracted-part affinity bucket into the low bits. This keeps
# critical sources ahead of metadata-only work, while putting files from the
# same tar.zst next to each other in an indexed queue. No evidence is skipped.
MOBILE_SHARD_AFFINITY = os.getenv("MOBILE_SHARD_AFFINITY", "true").strip().lower() not in {"0", "false", "no"}
MOBILE_SHARD_AFFINITY_BUCKETS = 4096



def _claim_work_batch(db, stage_run_id, worker_id, limit):
    """Atomically claim a bounded set of priority-ordered bundles.

    SKIP LOCKED makes claims disjoint between workers, and the pending-priority
    index avoids scanning the entire remaining mobile inventory for each item.
    """
    return fetchall(
        db,
        """WITH candidate AS (
            SELECT id FROM pipeline_work_items
            WHERE stage_run_id=:sid AND status='pending'
            ORDER BY priority, id LIMIT :batch FOR UPDATE SKIP LOCKED
        )
        UPDATE pipeline_work_items AS w
        SET status='running', attempt=attempt+1, worker_id=:worker,
            started_at=NOW(), error=NULL
        FROM candidate AS c
        WHERE w.id=c.id
        RETURNING w.id,w.payload""",
        {"sid": stage_run_id, "worker": worker_id, "batch": limit},
    ) or []


def _assign_shard_affinity(db, stage_run_id, *, batch_size=5000):
    """Reorder pending mobile work for locality without changing eligibility.

    Conversion is idempotent: unencoded priority values are 0..5; packed
    values are 6..24581 (smallint safe). A bounded UPDATE/commit keeps the
    job's queue responsive even with hundreds of thousands of source entries.
    Existing completed/failed/running claims and evidence objects are untouched.
    """
    if not MOBILE_SHARD_AFFINITY:
        return 0
    total = 0
    while True:
        row = fetchone(
            db,
            """WITH targets AS (
                   SELECT id FROM pipeline_work_items
                   WHERE stage_run_id=:sid AND status='pending'
                     AND priority BETWEEN 0 AND 5
                   ORDER BY id LIMIT :batch FOR UPDATE SKIP LOCKED
               ), changed AS (
                   UPDATE pipeline_work_items w
                   SET priority = 6 + w.priority * 4096 +
                       (hashtext(coalesce(
                           nullif(w.payload->0->'meta'->>'extracted_part_uri', ''),
                           w.payload->0->>'path', '')) & 4095)
                   FROM targets t WHERE w.id=t.id RETURNING w.id
               ) SELECT count(*) AS n FROM changed""",
            {"sid": stage_run_id, "batch": max(1, int(batch_size))},
        ) or {}
        changed = int(row.get("n") or 0)
        db.commit()
        total += changed
        if changed < batch_size:
            return total


def _mark_metadata_only_work(db, job_id, stage_run_id, bundles):
    """Account for metadata-only sources without treating them as native-parsed.

    Existing done/failed/running work is never changed. PostgreSQL updates are
    bounded and idempotent so a resumed case can switch to Fast Forensic.
    """
    if not bundles:
        return 0
    count = 0
    for start in range(0, len(bundles), 2000):
        batch = bundles[start:start + 2000]
        keys = [hashlib.sha256(bundle[0].path.encode()).hexdigest() for bundle in batch]
        paths = [item.path for bundle in batch for item in bundle]
        # Only pending work can be skipped; committed parser output remains intact.
        changed = fetchone(db, """WITH switched AS (
            UPDATE pipeline_work_items SET status='skipped', completed_at=NOW(),
                error='Fast Forensic: native parsing not required; source remains indexed'
            WHERE stage_run_id=:sid AND status='pending' AND partition_key=ANY(:keys)
            RETURNING id
        ) SELECT count(*)::int AS n FROM switched""", {"sid": stage_run_id, "keys": keys})
        count += int((changed or {}).get('n') or 0)
        execute(db, """UPDATE mobile_inventory_items SET
            status='unsupported', parser='fast_forensic_metadata_only',
            meta=meta || '{"native_parse_scope":"indexed_only","processing_mode":"fast_forensic"}'::jsonb,
            updated_at=NOW()
            WHERE job_id=:jid AND path=ANY(:paths) AND status='discovered'""",
            {"jid": job_id, "paths": paths})
        db.commit()
    return count


def _release_unstarted_claims(db, stage_run_id, worker_id, claimed):
    """Return only unstarted bundle claims, never completed work, to the queue."""
    if not claimed:
        return
    execute(
        db,
        """UPDATE pipeline_work_items
        SET status='pending',worker_id=NULL,started_at=NULL
        WHERE stage_run_id=:sid AND worker_id=:worker
          AND status='running' AND id=ANY(:ids)""",
        {"sid": stage_run_id, "worker": worker_id, "ids": [r["id"] for r in claimed]},
    )
    db.commit()

_PROGRESS_LOCK = threading.Lock()
_PROGRESS_AT: dict[tuple[str, str], float] = {}


def _publish_work_progress(db, job_id, stage, stage_run_id, *, force=False):
    """Record bundle progress on the stage row.

    Recovery used to finish thousands of files without moving progress_at.
    progressAgent then treated the stage as stalled, cancelled it, and the
    card fell back to waiting at 0 even though the finished bundles remained.
    """
    now = time.monotonic()
    key = (str(job_id), str(stage))
    with _PROGRESS_LOCK:
        if not force and now - _PROGRESS_AT.get(key, 0.0) < 5:
            return
        _PROGRESS_AT[key] = now
        # Bound memory usage in a long-lived worker across many cases.
        if len(_PROGRESS_AT) > 512:
            for expired in [k for k, t in _PROGRESS_AT.items() if now - t > 600]:
                _PROGRESS_AT.pop(expired, None)
    counts = fetchone(
        db,
        """SELECT count(*) AS total,count(*) FILTER (WHERE status='done') AS done,
        count(*) FILTER (WHERE status='failed') AS failed,
        count(*) FILTER (WHERE status='skipped') AS skipped
        FROM pipeline_work_items WHERE stage_run_id=:sid""",
        {"sid": stage_run_id},
    )
    if not counts or not int(counts["total"] or 0):
        return
    from app.services.forensic_serial_stages import report_progress
    from app.services.progress_agent import note_operation

    # The card follows the bundle queue while this pass is running. The file
    # counts are already complete by the time mobile parsing starts, and reporting
    # those left the card at 99% while tens of thousands of bundles were still queued.
    # The stage seal writes the extracted-file totals again when the pass returns.
    labels = {
        "parse": "Native forensic parsing",
        "recovery": "Deleted / recovery analysis",
    }
    report_progress(
        db,
        job_id,
        stage,
        total=int(counts["total"] or 0),
        completed=int(counts["done"] or 0),
        failed=int(counts["failed"] or 0),
        skipped=int(counts.get("skipped") or 0),
        label=labels.get(stage, stage),
    )
    # A 99% reconciliation written while file counts looked finished stays on the
    # card until it is removed. Bundle progress is the live denominator now.
    execute(
        db,
        """UPDATE pipeline_stage_runs
           SET details=details - 'progress_agent_reconciliation'
           WHERE job_id=:jid AND stage=:stage
             AND details ? 'progress_agent_reconciliation'""",
        {"jid": job_id, "stage": stage},
    )
    done = int(counts["done"] or 0)
    indexed_only = int(counts.get("skipped") or 0)
    total = int(counts["total"] or 0)
    note_operation(
        db,
        job_id,
        stage,
        f"{stage}: native {done:,}; indexed-only {indexed_only:,} / {total:,} bundles",
        timeout_seconds=300,
        advanced=True,
    )


def _persist_inventory_bulk(db, job_id, items):
    from psycopg2.extras import execute_values

    values = [
        (
            job_id,
            it.path,
            it.size,
            it.extension,
            it.mime_hint,
            it.sha256,
            it.status,
            it.parser,
            it.error,
            _dumps(it.meta or {}),
        )
        for it in items
    ]
    if not values:
        return 0
    with db.connection().connection.cursor() as cur:
        execute_values(
            cur,
            """INSERT INTO mobile_inventory_items
            (job_id,path,size_bytes,extension,mime_hint,sha256,status,parser,error,meta) VALUES %s
            ON CONFLICT(job_id,path) DO UPDATE SET size_bytes=EXCLUDED.size_bytes,
                extension=EXCLUDED.extension,mime_hint=EXCLUDED.mime_hint,
                sha256=COALESCE(EXCLUDED.sha256,mobile_inventory_items.sha256),
                status=CASE WHEN EXCLUDED.status='discovered' THEN mobile_inventory_items.status ELSE EXCLUDED.status END,
                parser=COALESCE(EXCLUDED.parser,mobile_inventory_items.parser),
                error=CASE WHEN EXCLUDED.status='discovered' THEN mobile_inventory_items.error ELSE EXCLUDED.error END,
                meta=mobile_inventory_items.meta || EXCLUDED.meta,updated_at=NOW()""",
            values,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            page_size=2000,
        )
    return len(values)


def _persist_artifacts_bulk(db, job_id, artifacts):
    from psycopg2.extras import execute_values

    # Multiple parsers can emit the same deterministic ID within a checkpoint.
    unique = {art.artifact_id: art for art in artifacts}
    values = []
    for art in unique.values():
        from app.services.forensic_priority_evidence import add_record_links
        add_record_links(art)
        forensic = art.forensic or {}
        values.append(
            (
                art.artifact_id,
                job_id,
                art.artifact_type,
                art.source_domain,
                art.timestamp_utc,
                forensic.get("state") or "allocated",
                forensic.get("ui_label"),
                _dumps(art.data or {}),
                _dumps(forensic),
                forensic.get("examiner_status") or "pending_review",
            )
        )
    if not values:
        return 0
    with db.connection().connection.cursor() as cur:
        execute_values(
            cur,
            """INSERT INTO mobile_normalized_artifacts
            (artifact_id,job_id,artifact_type,source_domain,timestamp_utc,state,ui_label,data,forensic,examiner_status)
            VALUES %s ON CONFLICT(artifact_id) DO UPDATE SET artifact_type=EXCLUDED.artifact_type,
                source_domain=EXCLUDED.source_domain,timestamp_utc=EXCLUDED.timestamp_utc,
                state=EXCLUDED.state,ui_label=EXCLUDED.ui_label,data=EXCLUDED.data,
                forensic=EXCLUDED.forensic,updated_at=NOW()""",
            values,
            template="(%s,%s,%s,%s,%s::timestamptz,%s,%s,%s::jsonb,%s::jsonb,%s)",
            page_size=2000,
        )
    return len(values)


def _analysis_run(db, job_id, stage, platform):
    row = fetchone(
        db,
        """SELECT run_id FROM mobile_analysis_runs WHERE job_id=:jid
        AND details->>'serial_pipeline'='true' ORDER BY started_at DESC LIMIT 1""",
        {"jid": job_id},
    )
    run_id = row["run_id"] if row else start_analysis_run(db, job_id, platform=platform)
    update_analysis_run(
        db, run_id, phase=stage, status="running", details={"serial_pipeline": True}
    )
    return run_id


def complete_mobile_analysis(db, job_id, validation):
    row = fetchone(
        db,
        """SELECT run_id FROM mobile_analysis_runs WHERE job_id=:jid
        AND details->>'serial_pipeline'='true' ORDER BY started_at DESC LIMIT 1""",
        {"jid": job_id},
    )
    if row:
        update_analysis_run(
            db,
            row["run_id"],
            status="completed",
            phase="complete",
            details={"serial_pipeline": True, "validation": validation},
        )


def evidence_bundles(items):
    """Keep a database and all sidecars in the same worker, including extensionless DBs."""
    groups = {}
    for item in items:
        key = item.path.replace("\\", "/")
        lower = key.lower()
        for suffix in SIDECARS:
            if lower.endswith(suffix):
                key = key[: -len(suffix)]
                break
        groups.setdefault(key, []).append(item)
    return [
        sorted(group, key=lambda item: (item.path.endswith(SIDECARS), item.path))
        for group in groups.values()
    ]


def _context(db, job_id, ds, *, references=None, evidence_paths=(), whatsapp_key_candidates=None, read_db=None):
    from app.services.artifact_selection_catalog import resolve_job_axiom_platform
    from app.services.mobile_forensic.integrity import load_intake_keys_from_disk_source
    from app.services.mobile_forensic.parsers._sqlite_util import SqliteEvidenceBytes
    from app.services.mobile_forensic.sqlite_counts import (
        _read_artifact_bytes,
        discover_whatsapp_keys,
    )

    keys = load_intake_keys_from_disk_source(ds)
    if whatsapp_key_candidates is None:
        whatsapp_key_candidates = discover_whatsapp_keys(db, job_id)
    acquired_paths = set(evidence_paths)
    read_errors = set()
    prefetched_bytes: dict[str, tuple[bytes, int]] = {}
    active_source_rows: dict[str, dict] = {}
    metrics = {"direct_reads": 0, "db_fallback_reads": 0, "prefetch_seconds": 0.0, "bundles": 0, "parse_seconds": 0.0, "commit_seconds": 0.0}

    def read_one(path, max_bytes):
        try:
            # The claim's small, hash-verified sources are already present in
            # memory from ONE streaming pass of their extraction shard.
            cached = prefetched_bytes.get(path.replace("\\", "/"))
            if cached is not None and (
                (max_bytes is not None and len(cached[0]) >= max_bytes)
                or len(cached[0]) == cached[1]
            ):
                data = cached[0] if max_bytes is None else cached[0][:max_bytes]
            else:
                # Keep MinIO reads outside the writer transaction.  The
                # independent read-only connection uses autocommit and releases
                # its DB connection before the potentially long archive scan.
                from app.services.mobile_forensic.direct_part_reader import read_claimed_member
                from app.services.tar_cache import read_file_from_part
                source_row = active_source_rows.get(path.replace("\\", "/"))
                handled, data = read_claimed_member(
                    source_row, path, max_bytes, read_file_from_part,
                )
                if handled:
                    metrics["direct_reads"] += 1
                else:
                    metrics["db_fallback_reads"] += 1
                    if read_db is None:
                        data = _read_artifact_bytes(db, job_id, path, max_bytes=max_bytes)
                    else:
                        data = _read_artifact_bytes(
                            read_db, job_id, path, max_bytes=max_bytes,
                            release_connection_before_io=True,
                        )
        except Exception:
            read_errors.add(path)
            raise
        if data is None and path in acquired_paths:
            read_errors.add(path)
        return data

    def read(path, max_bytes=120_000_000):
        data = read_one(path, max_bytes)
        if data and data.startswith(b"SQLite format 3\x00"):
            companions = {}
            for suffix in SIDECARS:
                if path + suffix not in acquired_paths:
                    continue
                companion = read_one(path + suffix, max_bytes)
                if companion:
                    companions[suffix] = companion
            return SqliteEvidenceBytes(data, companions)
        return data

    return ParseContext(
        job_id=job_id,
        platform=str(resolve_job_axiom_platform(db, job_id) or "Android"),
        source_id=(ds.get("evidence_manifest") or {}).get("source_id"),
        whatsapp_key_hex=keys.get("whatsapp_key_hex"),
        whatsapp_legacy_account=keys.get("whatsapp_legacy_account"),
        whatsapp_key_candidates=whatsapp_key_candidates,
        signal_db_key_hex=keys.get("signal_db_key_hex"),
        ios_backup_password=keys.get("ios_backup_password"),
        read_bytes=read,
        db=db,
        extra={
            "referenced_media_names": set(references or []),
            "read_errors": read_errors,
            "evidence_paths": acquired_paths,
            "prefetched_bytes": prefetched_bytes,
            "active_source_rows": active_source_rows,
            "performance_metrics": metrics,
        },
    )


def _seed_work(db, stage_run_id, bundles):
    from psycopg2.extras import execute_values

    values = []
    for bundle in bundles:
        payload = [
            {
                "path": it.path,
                "size": it.size,
                "extension": it.extension,
                "mime_hint": it.mime_hint,
                "sha256": it.sha256,
                "meta": it.meta,
            }
            for it in bundle
        ]
        key = hashlib.sha256(bundle[0].path.encode()).hexdigest()
        values.append((
            stage_run_id,
            key,
            json.dumps(payload),
            bundle_priority(bundle[0].path, bundle[0].extension),
        ))
        if len(values) >= 1000:
            with db.connection().connection.cursor() as cur:
                execute_values(
                    cur,
                    """INSERT INTO pipeline_work_items(stage_run_id,partition_key,payload,priority)
                    VALUES %s ON CONFLICT(stage_run_id,partition_key) DO NOTHING""",
                    values,
                    template="(%s,%s,%s::jsonb,%s)",
                    page_size=2000,
                )
            db.commit()
            values.clear()
    if values:
        with db.connection().connection.cursor() as cur:
            execute_values(
                cur,
                """INSERT INTO pipeline_work_items(stage_run_id,partition_key,payload,priority)
                VALUES %s ON CONFLICT(stage_run_id,partition_key) DO NOTHING""",
                values,
                template="(%s,%s,%s::jsonb,%s)",
                page_size=2000,
            )
    # A killed stage may have left claims. The caller holds the job execution lock.
    execute(
        db,
        "UPDATE pipeline_work_items SET status='pending' WHERE stage_run_id=:sid AND status='running'",
        {"sid": stage_run_id},
    )
    # Rows seeded before priority existed stay at the default. One pass fixes them.
    # Claims then read the integer column and the pending index.
    execute(
        db,
        """UPDATE pipeline_work_items SET priority = CASE
            WHEN lower(coalesce(payload->0->>'path','')) SIMILAR TO '%(msgstore|chatstorage|wa.db)%' THEN 0
            WHEN lower(coalesce(payload->0->>'path','')) SIMILAR TO '%(history|places.sqlite|bookmarks|browser)%' THEN 1
            WHEN lower(coalesce(payload->0->>'path','')) LIKE '%whatsapp%' THEN 2
            WHEN lower(coalesce(payload->0->>'path','')) SIMILAR TO '%(dcim|pictures|photos|trash|deleted)%' THEN 3
            WHEN lower(coalesce(payload->0->>'extension','')) IN ('.pdf','.doc','.docx','.xls','.xlsx','.ppt','.pptx','.odt','.ods','.epub','.rtf') THEN 4
            ELSE priority END
           WHERE stage_run_id=:sid AND status='pending' AND priority=5
             AND (
               lower(coalesce(payload->0->>'path','')) SIMILAR TO '%(msgstore|chatstorage|wa.db|history|places.sqlite|bookmarks|browser|dcim|pictures|photos|trash|deleted)%'
               OR lower(coalesce(payload->0->>'path','')) LIKE '%whatsapp%'
               OR lower(coalesce(payload->0->>'extension','')) IN ('.pdf','.doc','.docx','.xls','.xlsx','.ppt','.pptx','.odt','.ods','.epub','.rtf')
             )""",
        {"sid": stage_run_id},
    )
    _assign_shard_affinity(db, stage_run_id)
    db.commit()


def run_mobile_stage(db, job_id, stage, *, schema_name, stage_run_id):
    ensure_mobile_case_schema(db)
    db.commit()  # Finish DDL before opening independent worker Sessions.
    items = inventory_from_job_artifacts(db, job_id)
    from app.services.artifact_selection_catalog import resolve_job_axiom_platform

    run_id = _analysis_run(
        db, job_id, stage, str(resolve_job_axiom_platform(db, job_id) or "Android")
    )
    if stage == "normalize":
        from app.services.mobile_forensic.correlation import correlate_artifacts_db
        from app.services.mobile_forensic.coverage import persist_coverage_snapshot
        from app.services.mobile_forensic.storage import artifact_counts_by_domain

        relationships = correlate_artifacts_db(db, job_id)
        counts = artifact_counts_by_domain(db, job_id)
        states = {
            r["path"]: r
            for r in fetchall(
                db,
                "SELECT path,status,parser,error FROM mobile_inventory_items WHERE job_id=:jid",
                {"jid": job_id},
            )
        }
        for item in items:
            if item.path in states:
                item.status = states[item.path]["status"]
                item.parser = states[item.path]["parser"]
                item.error = states[item.path]["error"]
        persist_coverage_snapshot(db, job_id, items, counts)
        # Coverage is already persisted from the actual mobile inventory and
        # normalized artifacts above. Do not run a second priority-family
        # recount that can misclassify unacquired private app data as absent.
        update_analysis_run(
            db,
            run_id,
            inventory_total=len(items),
            files_processed=sum(
                it.status in {"parsed", "unsupported", "error"} for it in items
            ),
            unsupported_files=sum(it.status == "unsupported" for it in items),
            parse_errors=sum(it.status == "error" for it in items),
            artifacts_written=int(counts.get("total") or 0),
        )
        db.commit()
        return {
            "status": "ok",
            "relationships": relationships,
            "counts": counts,
            "run_id": run_id,
        }
    row = fetchone(db, "SELECT disk_source FROM jobs WHERE id=:jid", {"jid": job_id})
    ds = row.get("disk_source") or {}
    if isinstance(ds, str):
        ds = json.loads(ds)
    if stage == "parse" and isinstance(ds.get("evidence_manifest"), dict):
        persist_source_manifest(db, job_id, ds["evidence_manifest"])
    # Prepare/discover key once, not once for every database.
    seed_context = _context(db, job_id, ds)
    references = []
    if stage == "recovery":
        refs = fetchall(
            db,
            """SELECT DISTINCT lower(regexp_replace(value,'^.*[\\/]','')) AS name
            FROM mobile_normalized_artifacts m
            CROSS JOIN LATERAL jsonb_each_text(m.data) e(key,value)
            WHERE m.job_id=:jid AND e.key IN ('media_name','media_path','attachment','attachment_path')""",
            {"jid": job_id},
        )
        references = [row["name"] for row in refs if row["name"]]
    if stage == "parse":
        for start in range(0, len(items), 1000):
            _persist_inventory_bulk(db, job_id, items[start : start + 1000])
            db.commit()
    bundles = evidence_bundles(items)
    indexed_only_bundles = []
    if stage == "parse" and fast_forensic_enabled():
        indexed_only_bundles = [bundle for bundle in bundles if metadata_only_bundle(bundle)]
        # New work is seeded only for native parser candidates; old pending
        # work is reconciled below without touching committed work.
        bundles = [bundle for bundle in bundles if not metadata_only_bundle(bundle)]
    from app.services.forensic_serial_pipeline import _ensure_work_item_priority

    _ensure_work_item_priority(db)
    _seed_work(db, stage_run_id, bundles)
    indexed_only_count = _mark_metadata_only_work(
        db, job_id, stage_run_id, indexed_only_bundles
    ) if indexed_only_bundles else 0
    indexed_only_sources = sum(len(bundle) for bundle in indexed_only_bundles)
    if stage == "parse" and fast_forensic_enabled():
        import logging
        logging.getLogger(__name__).info(
            "fast-forensic job=%s metadata_only_sources=%d existing_queue_skipped=%d native_candidate_bundles=%d",
            job_id, indexed_only_sources, indexed_only_count, len(bundles)
        )
    db.commit()
    _publish_work_progress(db, job_id, stage, stage_run_id, force=True)
    # A parent stage session must not stay idle in a transaction while eight
    # evidence workers stream archives for hours.
    db.commit()
    # parallelism(8) is the CPU cap. The pool is smaller: each thread holds two
    # sessions until the stage returns, so eight threads overflow a 6+4 pool.
    slots = db_parallel_slots(parallelism(8))
    cancelled = threading.Event()

    def worker(slot):
        written = errors = processed = unsupported = 0
        worker_id = f"slot-{slot}"
        # The stop/paused flags are also checked by the progress agent. Polling
        # them with a per-bundle database round trip slowed 100k+ file cases.
        next_stop_check = 0.0
        claimed = deque()
        with firm_session(schema_name) as wdb, firm_session_readonly(schema_name) as rdb, stage_context(stage):
            context = _context(
                wdb,
                job_id,
                ds,
                references=references,
                evidence_paths=(item.path for item in items),
                whatsapp_key_candidates=seed_context.whatsapp_key_candidates,
                read_db=rdb,
            )
            # The context constructor performs source/key SELECTs. End that
            # snapshot before any external MinIO or CPU-heavy work.
            wdb.commit()
            registry = get_plugin_registry()
            try:
                while not cancelled.is_set():
                    now = time.monotonic()
                    if now >= next_stop_check:
                        from app.services.job_control import pipeline_should_stop

                        stop_requested = pipeline_should_stop(wdb, job_id)
                        wdb.commit()
                        if stop_requested:
                            cancelled.set()
                            break
                        next_stop_check = now + MOBILE_STOP_POLL_SECONDS
                    if not claimed:
                        claimed.extend(_claim_work_batch(
                            wdb, stage_run_id, worker_id, MOBILE_PARSE_CLAIM_BATCH
                        ))
                        wdb.commit()
                        if not claimed:
                            break
                        # One pass per selected part rather than one decompression
                        # per small source. Large/database sources retain their
                        # existing individual, integrity-checked read path.
                        context.extra["prefetched_bytes"].clear()
                        source_rows = context.extra["active_source_rows"]
                        source_rows.clear()
                        for claim in claimed:
                            source_payload = claim.get("payload") or []
                            if isinstance(source_payload, str):
                                source_payload = json.loads(source_payload)
                            for source_row in source_payload:
                                source_path = str(source_row.get("path") or "").replace("\\", "/")
                                if source_path:
                                    source_rows[source_path] = source_row
                        prefetch_started = time.monotonic()
                        if stage == "parse":
                            from app.services.mobile_forensic.shard_prefetch import prefetch_claim_bytes
                            last_prefetch_heartbeat = [0.0]

                            def _prefetch_heartbeat(detail):
                                now = time.monotonic()
                                if now - last_prefetch_heartbeat[0] < 20.0:
                                    return
                                last_prefetch_heartbeat[0] = now
                                from app.services.progress_agent import note_operation
                                note_operation(
                                    wdb, job_id, stage,
                                    f"Prefetch shard: scanned {detail.get('members_scanned', 0):,} files; "
                                    f"matched {detail.get('files_yielded', 0):,} / "
                                    f"{detail.get('files_wanted', 0):,} requested",
                                    timeout_seconds=300, advanced=True,
                                )
                                wdb.commit()

                            prefetch_claim_bytes(
                                claimed, registry, context,
                                on_detail=_prefetch_heartbeat,
                            )
                        context.extra["performance_metrics"]["prefetch_seconds"] += time.monotonic() - prefetch_started
                    work = claimed.popleft()
                    payload = work["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    bundle = [InventoryItem(**row) for row in payload]
                    buffer = []
                    failures = []
                    context.extra["read_errors"].clear()
                    parse_started = time.monotonic()
                    try:
                        for item in bundle:
                            if stage == "recovery" and item is not bundle[0]:
                                processed += 1
                                continue
                            parsers = (
                                registry.route(item, context)
                                if stage == "parse"
                                else registry.recovery_analyzers
                            )
                            item_written = 0
                            item_failures = []
                            for parser in parsers:
                                try:
                                    output = (
                                        parser.parse(item, context)
                                        if stage == "parse"
                                        else parser.analyze(
                                            [item] if len(bundle) == 1 else bundle,
                                            [],
                                            context,
                                        )
                                    )
                                    # A recovery analyzer receives the WHOLE bundle once.
                                    for artifact in output:
                                        companions = [
                                            {
                                                "path": it.path,
                                                "sha256": it.sha256,
                                                "size_bytes": it.size,
                                            }
                                            for it in bundle
                                            if it.path.lower().endswith(SIDECARS)
                                        ]
                                        if (
                                            companions
                                            and artifact.forensic.get("source_path")
                                            == bundle[0].path
                                        ):
                                            artifact.forensic["source_companions"] = (
                                                companions
                                            )
                                        buffer.append(artifact)
                                        item_written += 1
                                        if len(buffer) >= 500:
                                            written += _persist_artifacts_bulk(
                                                wdb, job_id, buffer
                                            )
                                            buffer.clear()
                                            wdb.commit()
                                            from app.services.progress_agent import note_operation
                                            note_operation(wdb,job_id,stage,'Parsed 500 source records',advanced=True)
                                            if pipeline_should_stop(wdb, job_id):
                                                cancelled.set()
                                                raise RuntimeError("Paused by user")
                                except Exception as exc:
                                    if cancelled.is_set():
                                        raise
                                    item_failures.append(
                                        f"{item.path}: {parser.name}: {exc}"[:1000]
                                    )
                                    errors += 1
                            if context.extra["read_errors"]:
                                item_failures.extend(
                                    f"Acquired evidence could not be read: {path}"
                                    for path in sorted(context.extra["read_errors"])
                                )
                                errors += len(context.extra["read_errors"])
                                context.extra["read_errors"].clear()
                            failures.extend(item_failures)
                            if stage == "parse":
                                item.status = (
                                    "error"
                                    if item_failures
                                    else "parsed"
                                    if item_written
                                    else "unsupported"
                                )
                                item.parser = ",".join(p.name for p in parsers)
                                item.error = (
                                    "; ".join(item_failures[-4:])
                                    if item.status == "error"
                                    else None
                                )
                                if item.status == "unsupported":
                                    unsupported += 1
                                _persist_inventory_bulk(wdb, job_id, [item])
                            processed += 1
                        metrics = context.extra["performance_metrics"]
                        metrics["parse_seconds"] += time.monotonic() - parse_started
                        commit_started = time.monotonic()
                        written += _persist_artifacts_bulk(wdb, job_id, buffer)
                        execute(
                            wdb,
                            """UPDATE pipeline_work_items SET status=:status,error=:err,completed_at=NOW()
                            WHERE id=:id""",
                            {
                                "id": work["id"],
                                "status": "failed" if failures else "done",
                                "err": "; ".join(failures)[:4000] if failures else None,
                            },
                        )
                        wdb.commit()
                        metrics["commit_seconds"] += time.monotonic() - commit_started
                        metrics["bundles"] += 1
                        if metrics["bundles"] % 25 == 0:
                            import logging
                            logging.getLogger(__name__).info(
                                "mobile-parse-perf stage=%s slot=%s bundles=%s prefetch_s=%.2f "
                                "parse_s=%.2f commit_s=%.2f direct_reads=%s db_fallback_reads=%s",
                                stage, worker_id, metrics["bundles"], metrics["prefetch_seconds"],
                                metrics["parse_seconds"], metrics["commit_seconds"],
                                metrics["direct_reads"], metrics["db_fallback_reads"],
                            )
                        _publish_work_progress(wdb, job_id, stage, stage_run_id)
                        wdb.commit()  # Do not hold the progress write across next shard scan.
                        context.clear_byte_cache()
                    except Exception as exc:
                        wdb.rollback()
                        # Infrastructure/persistence failures stop the barrier. Evidence
                        # parser failures above remain recorded exceptions per bundle.
                        execute(
                            wdb,
                            "UPDATE pipeline_work_items SET status='pending',error=:err WHERE id=:id",
                            {"id": work["id"], "err": str(exc)[:2000]},
                        )
                        wdb.commit()
                        cancelled.set()
                        raise
            finally:
                # The current bundle has its own durable status. Anything only
                # claimed but not started must be released when paused or when
                # a peer raises an exception. A killed process is handled by
                # _seed_work on restart, as before.
                if claimed:
                    try:
                        wdb.rollback()
                        _release_unstarted_claims(
                            wdb, stage_run_id, worker_id, list(claimed)
                        )
                    except Exception:
                        wdb.rollback()
        return {
            "written": written,
            "errors": errors,
            "processed": processed,
            "unsupported": unsupported,
        }

    totals = {"written": 0, "errors": 0, "processed": 0, "unsupported": 0}
    for result in bounded_map(worker, range(1, slots + 1)):
        for key in totals:
            totals[key] += result[key]
    if cancelled.is_set():
        from app.services.forensic_serial_pipeline import StageWaiting

        raise StageWaiting("Mobile stage paused; unfinished bundles remain pending")
    if stage == "recovery":
        from app.services.mobile_forensic.deleted_pipeline import (
            ensure_mobile_deleted_pipeline,
        )

        # Use the existing deleted-data bridge for the report/catalog consumers;
        # native recovery artifacts above retain full forensic provenance.
        ensure_mobile_deleted_pipeline(db, job_id, carve_sqlite=True)
        db.commit()
    work_counts = fetchone(
        db,
        """SELECT count(*) AS total,count(*) FILTER(WHERE status='done') AS completed,
        count(*) FILTER(WHERE status='failed') AS failed,
        count(*) FILTER(WHERE status='skipped') AS skipped
        FROM pipeline_work_items WHERE stage_run_id=:sid""",
        {"sid": stage_run_id},
    )
    update_analysis_run(
        db,
        run_id,
        inventory_total=len(items),
        **(
            {"recovered_written": totals["written"]}
            if stage == "recovery"
            else {
                "files_processed": totals["processed"],
                "artifacts_written": totals["written"],
                "parse_errors": totals["errors"],
                "unsupported_files": totals["unsupported"],
            }
        ),
    )
    db.commit()
    return {
        "status": "ok",
        **dict(work_counts),
        **totals,
        "inventory_total": len(items),
        "parallelism": slots,
        "processing_mode": "fast_forensic" if stage == "parse" and fast_forensic_enabled() else "deep_forensic",
        "indexed_only": indexed_only_sources if stage == "parse" else 0,
        "run_id": run_id,
    }
