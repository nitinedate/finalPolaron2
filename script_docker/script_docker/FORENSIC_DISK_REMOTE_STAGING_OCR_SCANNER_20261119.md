# Polaron disk extraction, native parsing, edge scanning and OCR — implementation handoff

**Date:** 19 November 2026  
**Source reviewed:** uploaded `polaron (2)(1).zip`; changes are code-level, not a successful deployment to the user's production servers.  
**Important:** Originals are evidence. Neither the disk pipeline nor the new OCR feature deletes original disk images, client files, or forensic case files.

## 1. Findings and implemented repairs

| Subsystem | Existing behavior / issue | Change in this patch |
|---|---|---|
| WAN client E01 extraction | Client-agent evidence was mounted read-only through FUSE and then read with TSK/pyewf; filesystem metadata traversal causes unpredictable network seeks. An idle warning at 1260/1350/1440 seconds identifies no progress, **not** the exact failed call. | Stage registered E01/EWF/RAW segments by sequential, bounded network copies to a shared server directory before acquiring the TSK CPU slot; verify SHA-256 and then atomically repoint DB paths. Subsequent parsing uses the server-local image. |
| Client transfer interruption | Reopening a remote image can repeatedly replay slow random reads. | `.partial` resumable transfer per segment; on resume compare the transferred prefix with the frozen source; stage manifest, checksum and per-file progress telemetry; single transfer lock per job, two bounded transfer threads by default. |
| Forensic image SHA metadata | Registration's `fingerprint_host_segment()` stores `sha256(path + size + mtime)` for files larger than 32 MiB, which is not a cryptographic image-byte hash. | Preserve the original registration fingerprint separately. After staging, update `evidence_files.sha256` and `disk_source.segment_hashes` with the **actual SHA-256 of the staged bytes**; the transfer manifest independently records each segment. |
| Native TSK processing | The source already contains isolated watchdog recovery and independent pyewf/pytsk3 thread handles. A GPU does not natively accelerate TSK directory walking. | Auto-select up to four independent readers for SSD/NVMe with CPU headroom, two for HDD/network/unknown, or override explicitly. Existing bounded native watchdog and extraction shard mechanisms remain intact. |
| Selective extraction | The Standard disk profile already excludes much OS/vendor/cache content **after** maintaining an inventory; Deep mode is available when full data coverage is required. | Preserve this documented filtering and metadata inventory. Do not drop registry, logs, deleted-file indicators or source integrity checks solely to improve speed. This change does **not** attempt partial E01 transfer, which would damage filesystem seekability. |
| Parallel vulnerability scanners | Server claims support `FOR UPDATE SKIP LOCKED` and laptop agent jobs; the old fallback could adopt a disconnected laptop's queued job on another laptop/network. | Default to owner-locked claims. Cross-laptop adoption requires explicit `EDGE_CROSS_SCANNER_JOB_REASSIGNMENT=true`. Laptop Compose/example allow two concurrent jobs when the **same network identity** and resources permit. Different networks should have distinct laptop scanner IDs. |
| New OCR menu | Existing Surya OCR + Tesseract is integrated for forensic images, but there was no folder-wide matching document search. | New **Forensic → OCR** page, protected API and isolated `ocr-documents` Celery queue; upload multiple documents/folders or specify an approved server inbox; native PDF text first, Surya OCR for scanned pages with Tesseract fallback, Office XML and plaintext readers; exact phrase/all words/any word search, file results, SHA-256 manifest ZIP. |
| OCR deletion | Directly deleting the user's originals, especially evidence, would be irreversible and break chain-of-custody. | Confirmation-protected removal of **job-owned unmatched copies**. An optional, admin-enabled mode **moves** verified nonmatches out of a dedicated tenant OCR inbox to recoverable quarantine and retains SHA-256. Never delete original evidence or unreadable files. Browser folder uploads never grant filesystem deletion permissions. |

## 2. Disk data flow

```text
CLIENT COMPUTER (forensic evidence remains read-only)
  E01/E02/... -> frozen client share -> read-only client-evidence FUSE
    -> identify registered disk segments and completeness
    -> remote_stage (I/O-only, bounded up to 2 parallel sequential copies)
        -> server shared /remote-disk-staging/<job-uuid>/
           .partial/resume -> verify source prefix on resume
           -> per-segment SHA-256 -> atomic rename and manifest
    -> atomic evidence_files path+SHA and disk_source handoff
  SERVER (independent stages with bounded worker concurrency)
    -> CPU-heavy admission -> isolated TSK root mount + directory inventory
       -> automatic read handles (NVMe/SSD <=4; HDD/network <=2)
    -> Standard-profile forensic selection / Deep if requested
    -> sharded content extraction + MinIO checkpoints
    -> native artifact parser claims with SKIP LOCKED + part buckets
    -> OCR/quality triage + entity/artifact enrichment
    -> RAG/vector indexing + forensic reports
```

An E01 image **must be fully readable** for TSK to follow its filesystem metadata. Therefore the server stages the *compressed E01 segments*, not every extracted OS file. Content selection runs after metadata inventory. Network speed and available server storage remain the hard constraints for staging.

### Configure on the forensic server

Set these in the forensic project's existing `.env` (do not overwrite existing secrets):

```dotenv
REMOTE_DISK_STAGE_ENABLED=true
REMOTE_DISK_STAGE_THREADS=2
REMOTE_DISK_STAGE_RESERVE_MB=1024
REMOTE_DISK_STAGE_ROOT=/remote-disk-staging
EXTRACT_TSK_ENUM_WORKERS=auto
# Only set this when confirmed; otherwise use the automatic media detector:
# DISK_SOURCE_MEDIA=nvme
```

`docker-compose.yml` mounts `${AETHERIS_BACKUP_DIR:-./backup}/forensic-data/remote-disk-staging` at `/remote-disk-staging` in API/disk/parse and associated workers. **Ensure this backing filesystem has enough free capacity for the whole compressed image plus reserve and per-case retention**, ideally on SSD/NVMe. Do not point it at a container-only filesystem or a slow FUSE re-export of the same client share.

For the stalled 19 November 2026 job, check **job-specific logs** for `remote_stage`, image registration readiness, frozen share mount, free bytes and transfer progress before restarting any stage. A stale job will not self-convert to healthy without a real successful read and commit; this patch changes the ingest path for subsequent processing. Existing in-flight tasks may require a controlled, checkpoint-aware retry. Do not create a duplicate extraction task or force status to 100%.

### Performance guidance

Use `EXTRACT_TSK_ENUM_WORKERS=auto` first. SSD/NVMe may improve directory metadata throughput using four separate handles; no speedup is guaranteed without a real E01 test. On a rotational HDD or remote mount, extra seek threads may make throughput *worse*. After staging, prioritize independent parsing parts, short SQL claim transactions, batch commits, a warm filesystem cache and low-compression extraction shards. Surya inference can use the separately hosted GPU; TSK/libewf themselves remain CPU/I/O-bound. A per-stage benchmark should record source GiB, segment count, filesystem entries, files selected, stage I/O time, native TSK duration, parser rate and CPU utilization.

## 3. Two vulnerability scans at the same time

On **Laptop A** and **Laptop B**, deploy separate `laptop-scanner` installations with their own persistent instance ID, correct `HOST_COMPUTERNAME` / `AGENT_COMPUTER_NAME`, and distinct `SCANNER_NAME`. Register and select each laptop scanner separately in Polaron; authorize each network and assign Scan Job A to Scanner A, Scan Job B to Scanner B. In the laptop `.env` set:

```dotenv
MAX_CONCURRENT_SCAN_JOBS=2
QUEUE_POLL_INTERVAL_SEC=3
```

This is the per-laptop *ceiling*, not a requirement for both jobs on one laptop. Same-laptop jobs must have a compatible network identity; **different client networks run on different laptop scanners**. Both laptops can operate simultaneously because claims are partitioned by `scanner_id`, row locks use `SKIP LOCKED`, and cross-scanner queued-job migration is disabled by default. Never share one scanner record/instance ID between two laptops. Existing performance scripts can intentionally set this ceiling to 1 for thermal reasons; check effective container env after startup.

On the central server keep the default:

```dotenv
EDGE_CROSS_SCANNER_JOB_REASSIGNMENT=false
```

If the second job still queues, inspect the assigned scanner ID, actual laptop registration/heartbeat, OpenVAS/feed readiness, scan authorization window, source network identity, and `MAX_CONCURRENT_SCAN_JOBS` in the **running** agent. Never solve it by automatically moving a job to a laptop on the wrong network.

## 4. Forensic → OCR user guide

**Option A — client folder/file selection:** Choose folder (Chromium folder picker) or Choose files; enter a sentence or keyword list; choose exact phrase, all words or any word; start OCR search. Each file is copied to a private job workspace and processed by two independently scheduled document worker processes with two threads each by default.

**Option B — server path:** Place files in a dedicated, tenant-specific OCR inbox such as `/app/data/ocr_search/inbox/<firm_schema>/batch_001`; select the folder path. `OCR_DOCUMENT_SOURCE_ROOTS` defaults to `/app/data/ocr_search/inbox`. The API rejects paths outside configured allowlists and other tenants. This is NOT a general purpose server file-browser or access to forensic case folders.

**Results:** The page shows processed count, errors, matching files, text snippets, engine and SHA-256. Download matched document(s) as a single ZIP with `OCR_SEARCH_MANIFEST.json`, including results and hashes. Unreadable/password-protected docs are errors, **never inferred as nonmatches**. PDF rasterization is limited to pages that need OCR; native PDF text, plaintext and XML-based Office documents do not waste GPU time.

**Unmatched documents:** By default the user can explicitly remove nonmatching files **only from the OCR job's private working copies** after the ZIP is complete; originals do not change. For disposable *server OCR inbox batches only*, administrators may set:

```dotenv
OCR_ALLOW_INBOX_PRUNE=true
```

After typing `QUARANTINE UNMATCHED SOURCE FILES`, verified nonmatching files are atomically **moved** out of that selected inbox to `/app/data/ocr_search/quarantine/<firm_schema>/<job_id>/`; the checksums and restore manifest remain available. Matching files and failed/unreadable files remain in the selected inbox. **Permanent deletion of forensic originals is intentionally not implemented.** A local browser folder cannot be deleted on the user's computer from this API.

### Supported types and limits

- `.pdf`, `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`, `.webp` — text layer first when present; Surya OCR and Tesseract fallback for scanned pages/images.
- `.docx`, `.xlsx`, `.pptx` — bounded native XML extraction, no macros executed; legacy `.doc` / `.xls` are not included in this first version.
- `.txt`, `.md`, `.csv`, `.tsv`, `.json`, `.xml`, `.html`, `.htm`, `.log`, `.eml` — decoded as text.
- Up to 3,000 supported files, 200 MiB each, 4 GiB aggregate, up to 3,000 PDF pages/file, bounded Office XML and raster pixel budgets. Scanner-independent OCR queue concurrency is separately adjustable through `OCR_DOCUMENT_THREADS` and Docker worker `--concurrency`.

### Deployment / smoke test

1. Back up the current project, PostgreSQL data and evidence manifest; stop **or complete** active ingestion jobs before replacing source and recreating workers. Do **not** delete evidence or rename shared data volumes.
2. Overlay the patch ZIP at the project root (or start from the sanitized full-source ZIP while retaining your deployment's protected `.env`); ensure the new `backend/app/services/`, `backend/app/routers/`, `frontend/src/pages/forensic/` files are copied.
3. In the deployed project, validate Compose with `docker compose -f docker-compose.yml config --quiet` and check storage mounts.
4. When safe to redeploy, use `docker compose -f docker-compose.yml up -d --no-deps api worker-disk worker-parse worker-ocr-documents`. Rebuild the existing frontend through your normal deployment pipeline; its generated image has not been built in this offline review.
5. Verify the `/forensic/ocr` menu and one small text PDF + scanned image + nonmatching file. Inspect logs with `docker compose -f docker-compose.yml logs --tail=150 worker-ocr-documents worker-disk api`; then perform a **controlled small test E01** over the client share. For two scanners, run two independent laptop agents and verify that both scan jobs show `running` in separate cases/networks.
6. Audit `sha256-manifest.json`, all `evidence_files.sha256`, job path handoff, extraction counts, native parse progress, ZIP contents, source retention and any files quarantined. Restore failed documents for reprocessing rather than discarding them.

**Validation performed here:** Python compile passes; targeted OCR, staging, scanner and parallel-TSK tests pass; updated TSX files pass syntax transpilation; Docker Compose YAML parses. A complete frontend build, full Python test suite, real Greenbone two-laptop scan, E01 client transfer, production PostgreSQL handoff, physical GPU throughput and live server deployment **have not been executed**. The local extracted repository did not include installed frontend npm dependencies or the Python `zstandard` module needed for some existing integration tests. No performance target or complete evidence coverage is claimed without measuring a real case.
