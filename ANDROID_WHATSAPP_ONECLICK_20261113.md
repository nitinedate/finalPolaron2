# Android WhatsApp crypt15 one-click working-copy recovery

## Scope
Android only. The acquisition screen has **Find key & decrypt WhatsApp**. Supply the path to the *case working copy* directory that contains `msgstore.db.crypt15` (not a phone mount or an original immutable evidence directory). The Android API only accepts paths below `WHATSAPP_WORKING_COPY_ROOT` (default `/evidence/cases`). The examiner requires `forensic.job.create` permission.

The backend uses `wa-crypt-tools`'s `wadecrypt` (already pinned as `wa-crypt-tools==0.1.0` in `backend/requirements.txt`) and attempts these key sources:
1. Existing, examiner-entered 64-character hexadecimal key (if supplied).
2. `encrypted_backup.key` beside the backup.
3. Same-named key in immediate sibling acquired directories, including `files/encrypted_backup.key`.

The crypt15 key is **not contained in the crypt15 ciphertext**; the software cannot invent a key that will decrypt an existing encrypted backup. An ADB acquisition cannot read private app `/data/data` without existing privileged access. The pre-existing Android acquire agent can capture accessible privileged keys when available. A newly-generated unrelated key is not valid for the old backup.

## Outputs
A successful run creates `msgstore.db`, `encrypted_backup.key` (if not already present), and `whatsapp-decryption-audit.json` in the selected working directory. SQLite signature and `PRAGMA quick_check` are verified before writing. The report includes input/output SHA256 hashes, key SHA256 (not the secret), and device-modification status. The original crypt15 backup is never altered and no file is silently overwritten. `encrypted_backup.key` contains *sensitive key material*, not encrypted ciphertext; secure and restrict access to the working folder. It is not suitable for unprotected downloads or user-visible logs.

## Running
Install project dependencies in the Android service: `python -m pip install -r backend/requirements.txt` (or rebuild its Docker image). Confirm `wadecrypt -h` works in that worker's PATH. If the service uses a different mounted case root, set `WHATSAPP_WORKING_COPY_ROOT` to the mounted evidence root and restart the Android API. Ensure the worker has write permission only on case working copies.

To test the isolated helper: `PYTHONPATH=backend python -m pytest backend/tests/test_android_whatsapp_oneclick_20261113.py -q`.

## Known limits
This is offline recovery, not guaranteed chat recovery, deleted-data recovery, or WhatsApp key extraction from unrooted Android devices. A matching previously available key is mandatory. The new button does not automatically schedule RAG ingestion of its output; the produced SQLite should be submitted to the normal Android WhatsApp parsing/indexing workflow. The UI provides a secure password field for an existing key but does not display/prefill secret key bytes from the server. Some filenames and key representations vary across WhatsApp releases. The new integration has synthetic unit-test coverage only; end-to-end verification in a deployed Android stack and with authorized real backups is still required.
