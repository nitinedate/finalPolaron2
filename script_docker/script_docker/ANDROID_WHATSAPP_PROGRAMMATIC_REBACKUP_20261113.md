# Android WhatsApp key-missing fallback (guarded, best effort)

## What changed

* The existing Android acquisition adapter still performs the offline, post-extraction `crypt15`/`crypt14` key discovery and decryption first.
* If **no matching key** is available, it can attempt an **on-device** WhatsApp re-backup in an isolated derived-evidence branch, with documented authorization. No new UI action or decryption button was added.
* An ADB/UIAutomator state machine tries to navigate WhatsApp (English UI), create a normal backup, enable 64-digit-key end-to-end backups, read the displayed 64-character key from accessible UI hierarchy, trigger a new backup, wait for `msgstore.db.crypt15` to become stable, and `adb pull` the public WhatsApp media tree again. It then uses the existing wa-crypt-tools-backed decryptor, requires successful SQLite validation, and writes `msgstore.db`, `encrypted_backup.key` and restricted `key.txt` into the derived working-copy directory.
* The post-modification branch is `whatsapp_authorized_rebackup/`; audit JSON is written inside it. Earlier acquisition files are preserved. Key material does not appear in routine logs or audit JSON.

## Required policy configuration

This procedure **changes the phone** and must never run automatically against forensic evidence without recorded examiner authorization. Both environment flags must be explicitly configured by the acquiring organization:

```
POLARON_WHATSAPP_ALLOW_DEVICE_MUTATION=1
POLARON_WHATSAPP_EXAMINER_AUTHORIZED=1
```

Alternatively, the acquisition request can supply **both** `whatsapp_generate_key_and_backup` and `allow_device_modification` authorization tokens with the first flag enabled.

No UI button is required for the fallback; it is gated by the acquisition policy.

## Important limitations

* **Not guaranteed unattended**: WhatsApp can hide the key from Android UIAutomator and change its settings interface between releases. If the 64-digit key cannot be read safely, the agent fails closed with `rebackup_requires_examiner_review`. It never fabricates a key or claims decryption succeeded.
* The phone must be powered on, unlocked, with USB debugging authorized and an accessible English WhatsApp UI. ADB cannot bypass lock screens or security prompts.
* A newly generated E2EE key can decrypt only a **matching newly encrypted** backup; it cannot decrypt the older `msgstore.db.crypt14` or a historic crypt15 made with a different key. The old evidence is not overwritten.
* Enabling or changing WhatsApp backup settings, creating backups and ADB UI-dump temporary files are **device modifications**. This fallback does not satisfy a *no-device-modification* forensic acquisition policy.
* A routine backup can have cloud/network effects depending on current WhatsApp settings. Isolate the device in accordance with case policy and record the state before enabling any network connection.
* UIAutomator has no contractual guarantee of exposing the 64-digit key screen. Without it, no 100% unattended implementation is possible through ordinary ADB.
* The storage path `key.txt` contains sensitive key material. Restrict it to examiner accounts; never include it in reports, telemetry or unrestricted exports.

## Verification

Four new synthetic unit tests for authorization and key extraction behavior passed; Python modules compiled. This version has **not** been tested on a real phone or against a fresh WhatsApp-generated crypt15 backup. Previous legacy UI tests reflect a UI that was removed in an earlier revision and therefore do not pass against the current layout.
