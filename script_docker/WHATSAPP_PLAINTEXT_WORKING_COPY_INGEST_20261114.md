# WhatsApp readable database intake for Android mobile reprocessing

## Why the UI showed 0 messages

The AXIOM-style WhatsApp count comes from **registered and parsed mobile artifacts**, not a directory listing. The prior job loaded encrypted `msgstore` files from its original sealed ZIP and reported `key_material_missing_or_invalid`. An externally decrypted `msgstore.db` placed under `03_Working_Copy/.../readable_artifacts/whatsapp/Databases/` was not automatically registered into `job_artifacts`. Therefore a database could exist on Windows while the UI still counted zero messages and zero conversations.

## Change

`backend/app/services/mobile_forensic/whatsapp_working_copy_intake.py` is invoked by `prepare_whatsapp_evidence()` **before** the analysis inventory is built. It derives the case ID from the job's own disk-source/evidence path and inspects only that case's `03_Working_Copy/<run>/readable_artifacts/whatsapp/Databases/msgstore.db` (or the documented alternate case spelling), never other cases and never the originals. It checks the SQLite header, quick-check, and recognizable WhatsApp chat/message tables. It then copies the bytes into case-owned `jobs/<job_id>/derived/mobile/android/whatsapp_decrypted/working_copy/.../msgstore.db` storage with SHA-256, registers it in `job_artifacts`, and lets the existing WhatsApp parser index it during reprocessing.

The physical decrypted file is **not proof that decryption was authenticated**; metadata makes no such claim. The app does not read `encrypted_backup.key` for this plaintext intake, change the phone, or change any source ZIP. It does not guarantee deleted message recovery. No PostgreSQL migration is required; existing `job_artifacts` columns are used.

## Deployment requirement

The backend must have read access to the host working-copy folder. If `/evidence/cases/<case_id>` is not mounted there, set the environment variable `POLARON_EVIDENCE_CASES_ROOT` to the **container-visible parent directory that contains the CASE-* folders**, and mount the relevant Windows drive read-only in Docker. Do not set it to a case subdirectory, an arbitrary ZIP path, or a private key file. If no mounted case is available, the gate returns `not_found_or_case_volume_not_mounted` and cannot ingest from the Windows-only path.

Example folder on the host:

`D:\project1\polaran2\evidence\cases\CASE-20261114-ANDROIDPHO\03_Working_Copy\CASE-20261114-ANDROIDPHO_E-400003_Android_phone_FFS_20261114_094907\readable_artifacts\whatsapp\Databases\msgstore.db`

In Docker, this might be visible as `/evidence/cases/CASE-20261114-ANDROIDPHO/03_Working_Copy/.../msgstore.db` if the volume is already configured. Verify that visibility *from inside the mobile backend container*.

## How to verify on Windows first

Open PowerShell in the screenshot's Databases folder (the folder displaying `msgstore`, with extensions potentially hidden):

```powershell
Get-ChildItem -File | Where-Object Name -Match '^msgstore(\.db)?$|^msgstore\.db\.crypt15$' | Select-Object Name,Length
python -c "import sqlite3; c=sqlite3.connect('file:msgstore.db?mode=ro',uri=True); print(c.execute('PRAGMA quick_check').fetchone()); print(c.execute('SELECT name FROM sqlite_master WHERE type=\"table\"').fetchmany(15)); c.close()"
Get-FileHash .\msgstore.db -Algorithm SHA256
```

*Verify the true filename first*. Do not rename evidence originals. The SQL parser may require a schema-specific migration only if it reports actual database errors; no new SQL table is needed merely to register a decrypted file.

## Refresh the UI

Deploy the patched backend, ensure the case volume is mounted, then run **Case Intake -> Reprocess mobile evidence** when the job is idle. Wait for all relevant mobile parse groups to finalize. The gate's job metadata contains `whatsapp_evidence_gate.working_copy_intake` with `registered`, `cached`, `rejected`, and `state`. Then inspect WhatsApp Messages and Chats. If the counts remain zero, inspect this state and the four processing errors. Valid SQLite by itself does not prove chats exist or that the parser supports the specific message schema.

## Testing

`PYTHONPATH=backend python -m pytest -q backend/tests/test_whatsapp_working_copy_intake_20261114.py backend/tests/test_whatsapp_payloads.py`

44 targeted tests passed locally. Not validated against the live PostgreSQL, Docker volume, or real 38 MB WhatsApp database.
