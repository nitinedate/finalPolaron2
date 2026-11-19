# Polaron Android WhatsApp — automatic offline post-extraction

## Changes
- Removed the manually triggered `Find key & decrypt WhatsApp` UI, its folder path, loading state, API client method, and old manual endpoint.
- Removed the Android ADB adapter's automatic WhatsApp decrypt agent (which could initiate extra device interaction or backup-creation attempts).
- After the Android adapter completes its selected acquisition targets, the adapter invokes an offline recursive scan of the acquired Android tree; no ADB commands are made by this stage.
- Locates every acquired `msgstore.db.crypt15` and identifies existing acquired `encrypted_backup.key` material (including another folder under the same extracted tree), plus the previously supplied valid 64-character key.
- Uses the installed `wa-crypt-tools` `wadecrypt` CLI, validates output as SQLite, and writes the derived `msgstore.db` beside the source backup in the acquired copy. Writes `encrypted_backup.key` **only after successful decryption**, without overwriting any existing file.
- Writes per-directory `whatsapp-decryption-audit.json` on success and root `whatsapp-post-extraction.json` with machine-readable statuses. All files are examiner-host artifacts; the evidence phone is not modified by this processing step.

## No-root limitation
No offline implementation can generate a valid matching key for an old `.crypt15` backup solely from the encrypted database. Without the key (either an acquired `encrypted_backup.key` or the existing 64-character key), status is `key_unavailable` and no plaintext database is generated. A newly generated independent key will not decrypt an existing backup.

## Install and checks
The backend requirements already pin `wa-crypt-tools==0.1.0`. Rebuild or reinstall backend worker dependencies (verify `wadecrypt --help` inside the Android API/worker environment). No external `git` command or internet connection is needed at analysis time after dependencies have been installed.

Unit smoke tests:

    PYTHONPATH=backend python -m pytest -q backend/tests/test_android_whatsapp_oneclick_20261113.py backend/tests/test_android_adapter_decrypt_agent.py

## Forensic scope
A decrypted database is a derived working-copy artifact, not an original file found on the phone. Preserve the original crypt15 file and its acquisition hash. This workflow does not claim deleted or inaccessible chats were recovered. Android acquisition itself can cause device state changes associated with access/USB debugging; only the *offline post-extraction decryption* makes no device changes. For separate imported disk images or non-ADB paths, ensure the corresponding extraction orchestration also runs the offline scan after materialization.

## Verification status
Synthetic tests passed. No real `.crypt15` backup/key pair was supplied for a live end-to-end decryption test. Frontend dependencies were not installed in the workspace, so a full TypeScript production build was not run.
