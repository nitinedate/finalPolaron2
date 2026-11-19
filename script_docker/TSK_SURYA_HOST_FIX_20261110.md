# Polaron disk TSK coverage + Surya 2.5 host model mismatch repair

## Root causes

1. The Windows host llama.cpp Surya endpoint advertised the absolute GGUF path from `/v1/models`, whereas the Surya Python library validates the exact identifier `datalab-to/surya-ocr-2.5`. Prior startup only checked that the ID contained `surya`; it therefore incorrectly reported readiness.
2. The parallel TSK inventory counted entries with unavailable `info.meta` as unrecoverable without trying the `TSK_FS_NAME.meta_addr` inode lookup. Alias suppression was conflated in the warning text with missing metadata.

## Source changes

- `scripts/ensure_host_surya.ps1`: start host llama.cpp with `--alias datalab-to/surya-ocr-2.5`; verify the exact model ID; if an existing endpoint is incompatible, restart only the saved PID after Windows process/command-line verification confirms Polaron's own Surya GGUF/port. An unknown/unmanaged process is never stopped.
- `backend/app/services/surya_adapter.py`: inspect `/v1/models` and reject a bad model identity *before* starting OCR. Preserve Surya predictor and bounded batches; no fabricated result or OCR success.
- `backend/app/services/tsk_parallel_inventory.py`: on missing `info.meta`, retry the metadata inode exposed by `info.name.meta_addr`, using the existing read-only TSK filesystem. Track `metadata_missing`, `metadata_recovered`, `metadata_unresolved`, and `metadata_name_address_missing` separately. Retain alias suppression.
- `backend/app/services/extracted_disk.py`: propagate recovery counters into progress and extract manifests; emit warning only when unreadable directories or unresolved metadata remain. Alias counts are still recorded and can require case-specific review. Existing evidence is not modified.

## Installation and recovery on Windows

1. Stop/finish active evidence jobs before updating source and restarting the OCR worker. Back up the project source and deployment settings. Do not delete DB, MinIO, indexes, or volumes.
2. Extract patch contents into `D:\project1\polaran2`, preserving folder structure.
3. Run `script_docker\start_docker_local.cmd` (or production launcher for production). The launcher checks host Surya and, for *verified Polaron-owned* old processes, restarts it with the stable alias. No model re-download is required.
4. Validate from Windows: `Invoke-RestMethod http://127.0.0.1:8000/v1/models | ConvertTo-Json -Depth 5` (model ID should be `datalab-to/surya-ocr-2.5`). From container: `docker exec aetheris-forensic-api-1 python -c "import json,urllib.request; print(json.load(urllib.request.urlopen('http://host.docker.internal:8000/v1/models'))['data'])"`.
5. Restart/resume the failed OCR task through Polaron's job recovery function after checking the new worker image is running. Do not manually mark the old batch complete; it remains retryable.
6. To reassess a previously enumerated case, re-run the inventory/extraction stage with the patched worker; old coverage counters won't retroactively change. Check the extract manifest's `tsk_metadata_recovered` and `tsk_metadata_unresolved` values. Independently verify sampled metadata failures against TSK (`fls`/`istat`) and document remaining source limitations in the forensic report.

## Boundaries

- A recoverable metadata address can restore some entries, but genuinely unresolvable or overwritten entries remain limitations. No guarantee that all 15,882 entries can be recovered; 100% processed does not mean 100% source coverage.
- An existing Surya service started outside Polaron's launcher cannot be terminated automatically; first verify ownership and restart it manually with `--alias datalab-to/surya-ocr-2.5`.
- Because evidence integrity matters, the patch never deletes evidence/volumes, bypasses exceptions, or falsely completes OCR batches.
- Code was tested under Linux using mocks and inventory unit tests; live Windows Docker/Surya model inference and E01 evidence enumeration were not available for end-to-end tests.
