# Polaron V1.27.0: mobile image created on the client PC (C:\evidence) and on the forensic server

## What was found (before this release)

Mobile images were created by whichever machine ran the HostDrive helper (`scripts/host-drive-helper.ps1`, port 9876).

- **Phone on the forensic server:** the API asks the server's helper through `host.docker.internal:9876`, and the
  image is written on the **server** in `<project>\evidence\cases\<CASE>\02_Original_Extraction … 08_Hashes`.
- **Phone on a client PC:** the browser talks straight to `http://127.0.0.1:9876` on that PC. That only worked if the
  examiner kit was installed there. The image then stayed **only on the client**, in the kit folder (not `C:\evidence`).
  The server never received it. "Recover from case folder" and "Start analysis" then searched the server's disk and
  found nothing, or got a client path the server cannot read.
- From a remote browser, a phone plugged into the **server** was also sent to the browser PC's own helper, because
  routing went by adapter type (`android_mtp` / `ios_lockdown`) rather than by the machine the phone is plugged into.

## What changes

### 1. Phone on the client PC: the image is created there and on the server

1. The HostDrive helper on the client PC creates the image in **`C:\evidence\<CASE>\…`**, with the same 01–10 case
   layout used on the server. The folder can be changed in the wizard; the default comes from `CLIENT_MOBILE_CASE_ROOT`.
2. When the run is sealed (`07_Logs\<run>\collection_summary.json`, written last), the browser asks the Client
   Evidence Agent on the PC to share that case folder **read-only**.
3. The forensic server mounts the share. It works over the office LAN, or over the agent's outbound connection from
   home or another site. The server then streams the run to **its own HostDrive helper**, which writes it into
   `<project>\evidence\cases\<CASE>`, the folder server-side collections use ("as usual").
4. Every file is hashed (SHA-256) while it is read from the client and again from the server disk. Only then does it
   get its final name. An existing file is never overwritten.
5. The server copy of `02_Original_Extraction\<run>` is marked read-only. The copy record is written next to the run:
   - `07_Logs\<run>\client_import_<id>.json`: client PC, client path, share manifest digest, transport, examiner,
     times and server paths.
   - `08_Hashes\<run>\client_import_<id>_sha256.txt`: SHA-256 of every copied file.
6. The wizard finishes with the **server** paths, and "Start analysis" registers the server copy. Registration,
   parsing, RAG and the report all run on the server. Both copies are kept; the client copy is never changed.

What is copied can be chosen:

- **Whole run** (default): original extraction, working copy, packages, logs and hashes, the same as a server-side
  collection.
- **Evidence packages, logs and hashes only:** much less to send over a slow link. Analysis reads `05_Exports`.

### 2. Pull an existing image from either place

A new **Pull an existing image** card on the acquisition page offers two sources.

- **From this computer:** a client-side browser. Choose the case folder (for example `C:\evidence\CASE-2026-001`).
  The server copies the newest sealed run as in section 1, then shows Complete with server paths.
- **From the forensic server:** a collection already in the server case folder, used in place. Nothing is copied.

The existing **Where is the evidence?** choice in the mobile job page is unchanged. With **This computer**, the server
reads an extraction folder in place over the network.

### 3. Routing by where the phone is plugged in

Since helper 5.58, `/health` reports `computer_name`. The browser compares its local helper with the server's helper
(`GET /api/acquisition/client-import/capabilities`):

| Phone plugged into | Browser on | Collected by | Image written to |
|---|---|---|---|
| Forensic server | the server | server helper (as before) | server case root |
| Forensic server | a client PC | server helper **through the API** (fixed) | server case root |
| Client PC | that client PC | client helper | `C:\evidence\<CASE>` **and then** the server case root |

## Interruptions and safety

- **Restart-safe.** The copy runs on the server; closing the browser does not stop it. On reload, the wizard
  reattaches to it (the copy id is kept in the live-run snapshot).
- **Resumable.** Copying the same run again keeps the files that were already verified and sends the rest.
- **Cancel** stops the copy. Unverified `*.polaron-part` files are removed; verified files are kept.
- **Tamper check.** A file that changes on the client during the copy fails it, using the V1.21 read-only mount check.
- **Platform guard.** The Android backend refuses an iOS run, and the iOS backend refuses an Android run.
- **No client paths reach analysis.** While only the client copy exists, Complete and "Start analysis" stay blocked.
- **Clear errors.** The server copy is refused with a clear message in these cases:
  - the share is truncated (more than 250,000 files listed);
  - the server helper is older than 5.58;
  - the agent is missing or listens on the PC only.
- **Full ZIP outside the case folder.** If the client wrote the full ZIP outside the case folder because the drive was
  short of space (`FULL_ZIP_LOCATION.txt`), the result carries a warning saying where it is.

## Security

- The server helper's `/acquisition/import/*` endpoints refuse any request from a web page (`Origin` header).
- They write only under the server evidence root (`/evidence/cases` → `<project>\evidence\cases`, or
  `HOST_IMPORT_CASE_ROOT`), and only inside `02/03/05/06/07/08_<folder>\<run>\`.
- They honour `HOST_DRIVE_HELPER_TOKEN`; the API now sends `X-Host-Helper-Token` when that variable is set.
- Recommendation: set `HOST_DRIVE_HELPER_TOKEN` on the server (in the Windows environment of the helper **and** in
  `.env`), and limit inbound TCP 9876 on the server to the Docker host. Other helper endpoints, such as
  `/acquisition/remove`, already need that protection.
- The share token never leaves the server's Redis or job record, and is not in the copy state returned to the browser.

## Install

### Server
1. Restart the HostDrive helper: `scripts\ensure-host-drive-helper.ps1`. It now expects 5.58 and restarts an older
   helper by itself.
2. Restart the Android and iOS API containers. The backend code is bind-mounted, so no image rebuild is needed for
   Python. FUSE settings are already present from V1.21.
3. Rebuild the frontend (`scripts\rebuild-ui.ps1`, or the build used today).
4. Optional `.env` settings: the `CLIENT_MOBILE_*` block in `.env.example`.

### Each examiner PC that collects phones
- **HostDrive helper and examiner kit** (USB collection), installed as on the server:
  1. Copy `scripts\`, `backend\` and `tools\` to the PC.
  2. Run `scripts\ensure-examiner-kit.ps1`.
  3. Run `scripts\install-host-drive-helper-task.ps1`.
- **Client Evidence Agent**, agent 1.2.0, the same installer as V1.21–1.23. The wizard shows the steps if it is
  missing.

## Verification
- `backend/tests/test_client_mobile_import_v1_27.py`: **15 tests**. They run against an in-process stand-in that
  follows the helper's import protocol. They cover:
  - run selection from case-folder and cases-root shares;
  - byte-identical copy with preserved modified times, read-only seal, provenance and hash list;
  - a result whose paths are server-only;
  - recovery after an API restart;
  - bytes corrupted on the server disk caught and re-sent;
  - already-verified files kept, and different files refused (nothing overwritten);
  - cancel leaving no parts;
  - platform guard, old helper and truncated share;
  - HTTP endpoints and error mapping.
- Existing suites were re-run: `test_client_evidence_v1_21.py`, `test_acquire_recover.py` and
  `test_acquisition_registry.py` (43 passed; 9 skipped because they need a real FUSE mount or Mono).
- The helper's embedded C# (dispatcher plus the new `AetherisImport` writer) compiles under **C# 5 rules**
  (`mcs -langversion:5`), the compiler level Windows PowerShell 5.1 `Add-Type` uses. It also compiles with
  PowerShell 7.4 `Add-Type`.
- `host-drive-helper.ps1` and `ensure-host-drive-helper.ps1` pass the PowerShell 7.4 parser. The parser's AST shows
  no variable expansion inside the new C# class; it sits in an expandable here-string, which would rewrite a `$`.
- Frontend:
  - `tsc --noEmit` is clean;
  - all of vitest passes, 46 tests, including 7 new ones in `src/lib/clientMobileImport.test.tsx` (routing matrix,
    computer-name identity, sealed-run detection, share → copy → result, share withdrawn on refusal, agent missing,
    unpaired or local-only);
  - `vite build --mode mobile-android` succeeds.

## Not verified here
- The C# writer running on Windows: long-path (`\\?\`) creation, `MoveFileExW`, `SetFileTime`, `SetFileAttributesW`.
  The Python tests exercise the same protocol through a stand-in.
- A real phone collected on a client PC, and the copy through a real Docker FUSE mount over a real LAN or the outbound
  connection.

## First-run checklist
1. Server: run `scripts\ensure-host-drive-helper.ps1`. Then
   `Invoke-RestMethod http://127.0.0.1:9876/health` shows `helper_version 5.58`, `client_import True` and
   `computer_name`.
2. Client PC: the examiner kit helper and the Client Evidence Agent are both running.
3. Open the console from the client PC → Android acquisition. Plug in the phone; it shows "this PC". Identify it.
   The authority step says "Phone on this computer — image here first, then on the forensic server".
4. Collect. `C:\evidence\<CASE>\02_Original_Extraction\<run>` fills on the client. When it is sealed, the Collect card
   shows "Forensic server copy" progress.
5. On the server, `<project>\evidence\cases\<CASE>\07_Logs\<run>\client_import_<id>.json` exists, and the hash list
   matches. Analysis starts from the server path.
6. **Pull an existing image → From this computer**: pick the same case folder again. All files are reported "already
   on the server" and nothing is overwritten.
