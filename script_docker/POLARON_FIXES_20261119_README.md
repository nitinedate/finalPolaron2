# Polaron: disk/remote extraction, vulnerability reports and OCR document search

**Change set:** 19 November 2026. **Input:** `polaron3(3).zip`.
**Delivery:** overlay/patch archive, not a replacement for a running PostgreSQL database or evidence volume.

## What this patch modifies

| Area | File(s) | Change |
| --- | --- | --- |
| Remote disk staging | `backend/app/services/client_disk_staging.py`, `backend/app/services/progress_agent.py` | Track a waiting-for-client-mount subphase; fingerprint actual remote transfer byte counts so working WAN E01 transfers do not trigger inaccurate no-progress notices. Original archive already includes resumable SHA-256-verified staging and separate bounded native TSK inventory workers. |
| Site infrastructure intake | `backend/app/routers/cases.py`, `frontend/src/pages/vuln/ReportsExportPage.tsx` | Editable Switch, Router, Email; repeatable server rows for Name, IPv4/IPv6, Role, OS and Location; max 200 rows per report with validation. Additional fields persist in the existing per-case `gap_site_json` JSONB; no migration required for those fields. |
| Reports, PDF, DOCX | `backend/app/services/gap_assessment_report.py`, `backend/app/services/gap_report_template.py` | Section 3 shows infrastructure data, extra pages for long server inventories, and site-provenance notes if several different sites are merged. Updated TOC pagination. Excludes Low findings and Low severity summary column **only from generated Gap Assessment PDFs**. The original scan findings, database, Excel, CSV and DOCX remain unchanged. |
| Job-first reporting | `frontend/src/pages/vuln/ReportsExportPage.tsx` | Select **Report selection > Select by scan job (any case)**: the corresponding case is resolved automatically. Finished jobs with saved intake are offered; the pre-existing multiple-job merge and the All jobs in case bundle remain available. |
| OCR Documents | `backend/app/services/document_search.py`, `.env.example`, `docker-compose.yml` | Existing OCR search/index/ZIP/quarantine flow gains an **optional** lazy-loaded pretrained PP-OCRv6 tiny/small/medium inference adapter that can use a GPU when provisioned; the default Surya endpoint is unchanged. It batches PDF raster pages, caches the model within a worker process, protects concurrent inference with a per-process lock, and uses Surya/Tesseract on a model failure. Documents with OCR failures remain errors, not false negative matches. |
| Tests | `backend/tests/test_gap_site_servers_and_low_pdf_20261119.py`, `backend/tests/test_remote_stage_progress_fingerprint_20261119.py`, `backend/tests/test_document_search_20261119.py` | Schema, IP validation, report pagination/merge, PDF-only severity filtering, DOCX, WAN progress and optional PaddleOCR mocked adapter. |

## Important: original archive already has these workflows

- Remote agent mounted read-only, verified server-local compressed E01 staging, isolated TSK retries, and bounded parallel TSK inventory (`EXTRACT_TSK_ENUM_WORKERS=auto`). These do not make remote source data magically available if its client agent/FUSE mount is dead, or bypass server storage capacity.
- Scanner agent partitioned per registered laptop/scanner ID, atomic SQL `SKIP LOCKED` claiming, bounded scanner thread pools, and defaults permitting two active jobs when hardware capacity allows. Separate laptops/networks need **different scanner IDs**; do not enable cross-scanner reassignment into a different customer's network.
- `Menu > OCR` screen at `/forensic/ocr`: folder/file uploads, tenant-scoped server-directory intake, Unicode-normalized phrase/word search, match summaries, SHA-256 manifest ZIP, delete unmatched **workspace copies**, and optional recoverable source-inbox quarantine.

## Safe overlay on Windows

Back up the original source, PostgreSQL and registered evidence/manifest first. Prefer a maintenance window with no active extraction or scanning jobs; do not delete persisted images or queues merely to deploy this patch.

```powershell
# Substitute your actual unpacked Polaron project location.
$project = 'D:\path\to\polaron3'
$patch = 'D:\Downloads\polaron_fix_20261119_patch.zip'
Copy-Item -LiteralPath $project -Destination ($project + '-backup-20261119') -Recurse
Expand-Archive -LiteralPath $patch -DestinationPath $project -Force
Set-Location $project
```

The patch **does not contain `.env`**. It includes only `.env.example`, which is a reference for optional settings. Keep your existing deployment secrets and volumes unchanged.

## Server and remote-E01 configuration

In the existing server `.env` (not the example), check:

```dotenv
REMOTE_DISK_STAGE_ENABLED=true
REMOTE_DISK_STAGE_THREADS=2
REMOTE_DISK_STAGE_RESERVE_MB=1024
REMOTE_DISK_STAGE_ROOT=/remote-disk-staging
EXTRACT_TSK_ENUM_WORKERS=auto
EDGE_CROSS_SCANNER_JOB_REASSIGNMENT=false
```

Make sure the host bind for `/remote-disk-staging` exists, is writable **from the disk worker** and has free space for all compressed E01 segments plus reserve. For the previously stalled job, inspect logs for `remote_mount`, `remote_stage`, `remote_staging`, `enumerating` and read offsets. If it is already running, a source update does not retroactively repair its in-memory task: use a checkpoint-aware **controlled retry** after checking client connectivity and source hashes. Never launch a duplicate extraction or mark it 100% manually.

## Two independent vulnerability jobs

On **each laptop** check `laptop-scanner/.env` and effective environment:

```dotenv
MAX_CONCURRENT_SCAN_JOBS=2
QUEUE_POLL_INTERVAL_SEC=3
```

The agent's hardware capacity policy can lower the effective concurrency. One job on Scanner A and another on Scanner B should proceed independently **only when** both are online/healthy, registered under unique persistent scanner/agent identifiers, the job is assigned to its matching scanner/network and feeds are ready. Diagnose a queued job's scanner assignment, heartbeat and capacity before altering global worker counts. A single laptop is not a safe substitute for two different network points of view.

## OCR Documents — recommended model selection

The fastest default is **native extraction** for digitally searchable PDF, Office XML and plaintext; use raster OCR only when needed. The existing Surya endpoint is the no-install default. As an independently configurable *optional* pretrained model, use **PP-OCRv6 small** for an accuracy/throughput balance; use **medium** for documents requiring higher accuracy or **tiny** for faster screening. The official model documentation gives hardware-specific benchmarks, not a guarantee for your server. This patch does **not** include weights, a GPU runtime or `paddleocr` in the existing image.

```dotenv
# Default, requiring no additional Python packages:
OCR_DOCUMENT_ENGINE=surya
OCR_DOCUMENT_DEVICE=cpu

# Optional after installing a compatible PaddleOCR 3.x/PaddlePaddle runtime and
# warming the exact PP-OCRv6_small_det and PP-OCRv6_small_rec models:
# OCR_DOCUMENT_ENGINE=paddle_v6_small
# OCR_DOCUMENT_DEVICE=cpu
# On a GPU-enabled OCR worker (separate Compose GPU configuration is required):
# OCR_DOCUMENT_DEVICE=gpu:0
```

`docker-compose.yml` now passes these two independent parameters into `worker-ocr-documents`. To use PaddleOCR, build a dedicated worker image with `paddleocr` and compatible CPU or CUDA-enabled `paddlepaddle` wheels and grant that container GPU access when applicable; otherwise it will try Surya/Tesseract. Do **not** install PaddleOCR into production workers during active evidence acquisition or rely on unexpected runtime downloads. Warm/verify weights in a staging environment first.

OCR file removal is intentionally limited to user-confirmed job-owned working copies. Optional server-inbox cleanup moves hash-verified nonmatches to **recoverable quarantine** only, after administrator enablement (`OCR_ALLOW_INBOX_PRUNE=true`) and typing the confirmation. Forensic originals and unreadable/failed documents are never deleted. Browser folder upload cannot give the server permission to delete the user's local files.

## Validation executed in this offline workspace

- `python -m compileall` on changed Python modules — pass.
- React Reports TSX TypeScript parser/transpilation — zero syntax diagnostics; **full npm build not run** (node_modules absent).
- Docker Compose YAML parser — pass (14 services); **Docker startup not run**.
- Targeted Python tests — **73 passed, 1 skipped, 1 pre-existing severity expectation test deselected**. Schema import integration test skipped because `psycopg2` is absent in this offline test environment; production imports need the project's configured Postgres driver.
- Rendered a **sample report PDF in memory** using PyMuPDF fallback: server inventory (including last of 20 rows), email, High finding present; Low finding absent. xhtml2pdf was not available locally, so the higher-fidelity renderer was not exercised here.
- Optional PaddleOCR 3.x adapter tested with a mocked documented API; **real GPU inference and model accuracy not measured**.
- No production PostgreSQL migration, WAN E01 transfer, laptop Greenbone/OpenVAS scan, real GPU benchmark, client cleanup or deployed frontend build was run. These require your live infrastructure and should be checked after installation, not assumed fixed.

## Smoke tests after deployment

1. Confirm Compose config and that API/disk/OCR workers receive the expected settings. Check storage mount and worker queues.
2. Run a small **known-good** E01 over the client connection, check `remote_stage` progress increasing every ~20 seconds, the SHA-256 manifest, and TSK inventory to 100% with no duplicated tasks.
3. Start two authorized scan jobs assigned to **different scanner laptops**; confirm both become running and generate independently attributable scan logs/results.
4. Create a Gap Site Infrastructure record with at least 20 server rows, export PDF and DOCX, inspect every row and TOC, and verify Low findings still exist in the raw result/Excel but **not** in PDF.
5. Select Job first, test whole-case and multi-job merged export, confirm correct case/site ownership and scan-job IDs.
6. OCR a native PDF, scanned image and nonmatch; download ZIP, verify hashes; if cleanup is needed, test only on a **disposable tenant OCR inbox**. Restore quarantine to prove reversibility.

**No extraction stage should be claimed complete from a heartbeat alone.** Keep source limitations and unreadable evidence visible in the final forensic report; 100% processing does not imply full evidence coverage.
