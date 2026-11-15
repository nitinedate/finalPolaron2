# Polaron Fast Forensic processing (Mobile + Disk) — 2026-11-11

## Scope

`FORENSIC_PROCESSING_MODE=fast_forensic` is the default. `deep_forensic` disables
new selective filters for future work but **does not automatically requeue**
records that a prior Fast Forensic run marked `skipped` / `indexed_only`.

- Mobile: after complete extraction and inventory registration, classify
  evidence bundles. Retain native parsing for likely application databases,
  SQLite companions, contacts, messages, browser history, calls, location,
  manifests, unknown formats, and recovered/deleted sources.
- Common media, documents, and known binary payloads without forensic-rich
  path markers remain searchable by their inventory metadata and eligible for
  dedicated OCR/media/document processing. They do **not** count as native-parsed.
- Mobile bundles previously seeded in the same active stage are reconciled with
  bounded PostgreSQL updates: only `pending` items become `skipped`; `running`,
  `done`, and `failed` work is not modified. Already-committed normalized
  artifacts remain unchanged.
- Mobile inventory records are annotated with
  `parser=fast_forensic_metadata_only` and
  `meta.native_parse_scope=indexed_only`, unless a stronger terminal status is
  already present. Coverage describes these as unsupported native parsing, not
  full evidence coverage.
- Disk: preserves the existing selective queue, quality-OCR dispatch, and
  single-pass part streaming; explicit `deep_forensic` bypasses the selective
  preparation in the serial stage for future work.

## Install

1. Capture the current job ID and Stage-5 committed count; preserve original
   evidence, MinIO objects, PostgreSQL database, and checkpoint records.
2. Pause the job through the supported application workflow, and ensure no
   parse worker is still processing the case before changing files.
3. Back up the replaced files. Apply the patch at `D:\project1\polaran2`.
4. Set `FORENSIC_PROCESSING_MODE=fast_forensic` in the root `.env`. The fallback
   in code is also `fast_forensic`. Rebuild and recreate only the worker image
   used for mobile/disk parsing with your normal launcher/Compose override set.
5. Resume the **same** job; do not start a new acquisition or delete storage.
6. Verify per-item coverage, `pipeline_work_items` pending/done/skipped counts,
   and logs containing `fast-forensic`. Confirm that iOS native candidates are
   still parsed and indexed-only evidence is visible in inventory.

## Inspection SQL (use the current tenant schema and correct stage-run ID)

```sql
SELECT status, count(*)
FROM pipeline_work_items
WHERE stage_run_id = :stage_run_id
GROUP BY status ORDER BY status;

SELECT parser, status, count(*)
FROM mobile_inventory_items
WHERE job_id = :job_id
  AND parser = 'fast_forensic_metadata_only'
GROUP BY parser, status;
```

The stage's `indexed_only` result field describes metadata-only source count.
Its native-parsing progress denominator is now the candidate work queue for
fresh runs; resumed stages may also contain previously seeded `skipped` work.
These are different from extracted-file totals and are not evidence-recovery
percentages.

## Warnings and limitations

- This is a conservative file-extension/path classifier; it is not a full
  forensic artifact-detection engine. Some documents and media may contain
  important embedded metadata or application data; their specialist stages
  must remain enabled, and examiners must be able to request targeted deep
  parsing when needed.
- Do not treat `skipped`, `unsupported`, or `indexed_only` as a successful native
  parse or a claim of complete acquisition/coverage.
- Existing uncommitted work may be retried after an orderly pause.
- This patch has not been tested against your live 179,028-item case. Run
  staging/smoke checks first; there is no guaranteed elapsed-time improvement.
- If source storage reports `Errno 5`, resolve its underlying disk/mount issue;
  this mode does not repair filesystem faults.
