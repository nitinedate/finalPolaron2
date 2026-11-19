# Polaron: live Android/iOS device isolation and private server evidence transfer

Date: 17 November 2026
Scope: mobile Android and iOS acquisition; forensic Disk, RAG and vulnerability services remain separate.

## Problem and implemented behavior

1. A disconnected phone must not remain in the device list. The Windows helper now refreshes connected PnP devices and actual ADB/libimobiledevice transports every six seconds. It never uses historical Windows USB registry entries as proof of a connected device. The device cache expires after 15 seconds, and the mobile acquisition page polls while waiting for connection, identification, or authorization. Disconnection clears a stale selection. If no phone is connected, the device list is empty. Unknown device identity remains **unknown**, never a fabricated Android phone.
2. Android screens accept Android devices only; iOS screens accept iOS devices only. The Windows agent probes ADB for Android manufacturer/model and libimobiledevice for iOS ProductType. Without pairing or authorization, the UI may correctly show the OS but cannot claim a precise model.
3. Before starting host acquisition, the authenticated examiner requests a platform- and case-bound, time-limited upload ticket from the corresponding Android/iOS backend. The examiner-host agent completes the acquisition, builds the complete evidence ZIP and uploads it directly to that backend in SHA-256-verified 8 MiB chunks. It must receive the verified server receipt to mark acquisition complete. A partial/failed transfer does not mark collection complete; its generated local ZIP is preserved for retry.
4. The receiving backend stores the original ZIP in a tenant- and platform-specific private directory under the persistent `DATA_ROOT/private_mobile_transfers_v1/` tree, with service-account-only POSIX permissions where supported. The server verifies every chunk and a complete SHA-256 digest and publishes the evidence atomically with a receipt. The browser sees only transfer status and checksum; it never receives or downloads evidence bytes. Raw image/export paths are no longer rendered in the acquisition screen.
5. Case job creation receives an opaque reference `mobile-transfer:<transfer_id>`, resolves it against tenant, mobile service and case, and persists the verified server-side evidence directory for extraction/RAG. There is no cross-product fallback. Re-verification computes SHA-256 on the server; it does not need an examiner workstation path.
6. On a verified upload, the examiner-host uploader attempts to delete the *generated ZIP copy* on the client. Forensic raw source/staging and sidecar metadata are retained under the existing evidence-retention and chain-of-custody policies. An examiner with filesystem access to that machine might still see those files; hiding paths in React is not a filesystem security boundary. Apply appropriate OS ACLs and retention/secure-erasure policy separately if complete client-side removal is required.

## Files changed

- `backend/app/services/mobile_acquire/secure_transfer.py` — secure transfer store, verification, scoped tickets.
- `backend/app/routers/acquisition.py` — ticket/chunk/complete/verify endpoints.
- `backend/app/routers/jobs.py` — resolve verified mobile evidence references.
- `scripts/mobile_device_probe.py` — live ADB/iOS model detection.
- `scripts/mobile_server_transfer.py` — direct host uploader with verification/retries and safe local ZIP cleanup.
- `scripts/host-drive-helper.ps1` — live PnP/agent refresh, cache expiry, platform-safe preview and acquisition routing.
- `scripts/host-mobile-acquire.ps1`, `scripts/native_mtp_job.ps1`, `scripts/native_ios_job.ps1` — mandatory server transfer before successful host acquisition result.
- `frontend/src/lib/hostDriveHelper.ts`, `frontend/src/lib/forensicApi.ts`, `frontend/src/lib/types/forensic.ts`, `frontend/src/pages/forensic/MobileAcquisitionPage.tsx` — platform checks, polling, secure transfer ticket, opaque evidence reference, no image paths in UI.
- `backend/tests/test_mobile_secure_server_transfer_v20261117.py`, `backend/tests/test_mobile_device_probe_and_uploader_v20261117.py` — regression and local HTTP end-to-end tests.

## Deployment and prerequisites

- **Back up your running instance first.** Upgrade the Android API, iOS API, frontend and the *Windows examiner-host helper together*. Do not install only the frontend changes.
- Run the mobile APIs under their respective `mobile-android` and `mobile-ios` service identities. Use a stable, distinct production signing secret and persistent `DATA_ROOT`; make its private evidence tree writable only by its service user, and mount it into the corresponding workers that read mobile evidence. Ensure the API and Celery workers use the same evidence volume and do not merge Android/iOS buckets or databases.
- The browser/gateway must expose the mobile transfer endpoints to the examiner host over HTTPS, with support for `PUT`, up to 8 MiB per request, long-running transfers, and mobile-service routing. HTTP is rejected except loopback development connections. Authentication is required to issue tickets; the scoped ticket is used for the upload itself.
- Windows: restart the host helper with the existing `scripts/ensure-host-drive-helper.ps1` launcher and rebuild/restart relevant backend/frontend containers using the deployment Compose file. On the workstation, ensure ADB is available for Android and libimobiledevice `idevice_id`/`ideviceinfo` is installed for iOS model identification. Accept the appropriate USB Debugging or Trust prompts for authorization.
- For each mobile service, verify `GET /api/acquisition/devices` on the *correct service route* returns empty when no phone is attached, then plug each type in turn and check the model/OS. Run a small authorized test acquisition and confirm the server receipt. Check `DATA_ROOT/private_mobile_transfers_v1` (server only) contains a read-only `evidence.zip` and matching `receipt.json`.
- Full encrypted iPhone backup, WhatsApp private data and Android full-file-system access still depend on device permissions, available acquisition methods and keys. The transfer improvement does not bypass mobile encryption/OS security or guarantee deleted-data recovery.

## Validation

Targeted Python tests: 27 passed (includes existing V26 isolation tests, secure-transfer tests and client-agent tests). The local end-to-end HTTP test verifies a multi-chunk upload, private server store, SHA-256, receipt, and generated-client-ZIP deletion. Python source syntax checks passed. The four edited TypeScript/TSX modules passed syntax transpilation; this is *not* a complete frontend dependency/type-check build. Live USB testing requires a Windows workstation, an Android phone, an iPhone and the real deployed backend and is not claimed as performed.

The pre-existing `test_mobile_platform_isolation_v24.py` broad infrastructure suite has five unrelated legacy Compose/host-capacity assertions failing against this source package's existing configuration; this patch does not modify the affected Compose, volumes or Redis deployment configuration. Run full CI before production release.

Test command from the repository root:

```
cd backend
python -m pytest -q tests/test_mobile_unified_console_v26.py tests/test_mobile_secure_server_transfer_v20261117.py tests/test_mobile_device_probe_and_uploader_v20261117.py
```

## Security / forensics cautions

- Transfer tickets grant time-limited write permissions. Never log or display the ticket in React, process logs or exported reports.
- Preserve chain-of-custody facts and SHA-256 receipt. A SHA match verifies transfer integrity but does not assert complete device coverage.
- For network outages, retry with the same acquisition/transfer ticket until expiry; never silently fall back to a local-only success result.
- Server storage is private by API and intended to be service-account-restricted by filesystem ACLs. Configure encrypted-at-rest storage and backup retention to meet your agency's policy; the patch does not turn every filesystem or object store into encrypted-at-rest storage automatically.
