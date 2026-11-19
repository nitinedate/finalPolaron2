# Polaron TSK coverage log TypeError hotfix (2026-11-10)

## Root cause
The TSK coverage log concatenated adjacent f-strings with a parenthesized ternary expression without a `+`. Python interpreted this as attempting to call a string, raising `TypeError: str object is not callable`.

## Change
Add an explicit `+` to concatenate the final conditional suffix. This does not alter extraction, metadata recovery, evidence bytes, or coverage counters. Both unresolved and fully-recovered coverage messages are now valid.

## Apply
Stop or pause the running extraction before deploying updated worker code. Back up the existing `backend/app/services/extracted_disk.py` and extract the patch into the Polaron project root, preserving paths. Use the existing Polaron launcher to rebuild/restart forensic API and extraction workers. Do not delete Docker volumes or evidence, and do not run `down -v`. Retry the failed extraction through the app's resume workflow; check its checkpoint and counts first to avoid unnecessary duplicate extraction.

## Validation
`python -m py_compile backend/app/services/extracted_disk.py` succeeded; three isolated regression cases passed. Windows Docker and live E01 processing were not run in this environment.
