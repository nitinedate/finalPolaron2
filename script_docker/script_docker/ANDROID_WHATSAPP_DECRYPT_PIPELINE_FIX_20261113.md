# Android WhatsApp post-extraction decryption processing update

- The Android ADB acquisition adapter invokes the offline WhatsApp post-extraction scanner when acquisition targets finish and the acquisition has not been interrupted.
- Recursively discovers `msgstore.db.crypt15`, as requested, plus `msgstore.db.crypt14` for real-world cases that contain the older format.
- Selects the matching format-specific key from the acquired working copy: `encrypted_backup.key` for crypt15 and `key` for crypt14. Existing examiner-supplied 64-character crypt15 key is accepted. No new key can unlock an existing encrypted backup.
- Uses the existing `wa-crypt-tools` `wadecrypt` executable and validates SQLite magic and `PRAGMA quick_check` before publishing `msgstore.db` in the extracted working copy.
- Keeps the source encrypted file unchanged, hashes the encrypted and derived files, and records `whatsapp-decryption-audit.json` beside a decrypted database.
- Updates `whatsapp-post-extraction.json` on subsequent scans to avoid stale missing-key results.
- No decryption button and no added ADB requests in this processing stage; key viewing remains controlled by the previously implemented UI where supported.
- Evidence-derived outputs must be created only in the examiner's writable evidence working copy and not on a read-only or direct evidence mount.

## Limitations

Decryption cannot succeed without the original matching key, regardless of backup format. On an ordinary unrooted Android device, WhatsApp's private key may be inaccessible through normal ADB acquisition. The Android extraction itself may cause state changes, but this offline post-extraction step does not communicate with the phone.

The current hook is in the Android ADB adapter; separately imported Android forensic images or ZIP ingestion flows need to invoke the shared `decrypt_extracted_android_tree()` function after materialization. No claim of end-to-end verification with real user backup data is made.

## Tests

`PYTHONPATH=backend python -m pytest -q backend/tests/test_android_whatsapp_oneclick_20261113.py backend/tests/test_android_adapter_decrypt_agent.py`

Result: 6 synthetic tests passed. Full frontend build and real-device decrypt tests were not run.
