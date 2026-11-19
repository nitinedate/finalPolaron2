# Polaron V1.31.14: a collection on a client computer goes to the server while it runs

## What was asked
A phone collected on a client computer was written to that computer (`C:\evidence\<CASE>`) and copied to the
forensic server only afterwards, through the Client Evidence Agent. The image should end up on the server instead:
the data is sent to the server in chunks while it is extracted.

## What changes
- **Sent while it runs.** When a collection starts on a client computer, the page opens an upload session on the
  server and hands it to that computer's helper with the collection. A new uploader on the client
  (`scripts\polaron-stream-upload.ps1`, started hidden by the helper) does the following:
  - Every finished file is sent in 8 MiB chunks. Small files go in groups, so an iPhone backup's many small
    files do not cost two requests each.
  - **iPhone:** while the backup runs, the files are sent from its staging folder (`<drive>:\ib\...`) under their
    final case paths. The move into the case afterwards does not send them again.
  - **Files the collector still rewrites wait until the collection ends.** These are `Manifest.db`,
    `Status.plist`, `Info.plist`, journals and temporary files, the logs, and the packages and hashes.
  - **A file that changes after it was sent is sent again.** The server replaces only its own copy from this
    session, and only before the run is registered.
- **Checked on the server.** Each file is written by the server's HostDrive helper, the same writer the client
  copy uses:
  - a file gets its final name only after its SHA-256 matched on the server disk;
  - an existing file is never overwritten.

  When the collection ends:
  - the server checks that it holds every file;
  - it marks the original extraction read-only;
  - it writes a hash list (`08_Hashes\<run>\client_stream_<id>_sha256.txt`) and a provenance record
    (`07_Logs\<run>\client_stream_<id>.json`);
  - it registers the run with server paths.

  The page then goes straight to *Complete* and processing, as for a collection made on the server.
- **Moved, not copied (default).** The copy on the client computer is removed only after the server confirmed the
  whole run, and only for files that have not changed since they were hashed.
  - A receipt stays in the case folder: `MOVED_TO_SERVER_<run>.json`, with the server folder and the hash
    list's SHA-256.
  - Junctions are removed without following them. The relocated iPhone payload
    (`<drive>:\Aetheris_Mobile_Payloads\...`) is removed only when it belongs to this run.
  - **Keep a copy on this computer** is a new option on the Authorise step. *Packages only* always keeps the
    local copy, because only part of the run is sent.
- **Nothing is deleted unless the server confirmed it.** The image stays on the client computer, and the page
  offers the existing **Copy to the forensic server** button, when:
  - the collection fails or is stopped;
  - a file could not be sent;
  - the server refuses the session.
- **Restarts are handled.** If the client computer restarts or the uploader stops, the helper starts it again.
  It continues from what the server already holds and does not send those files again.
- **Progress on the page.** During the collection, the page shows *"Sent to the forensic server and verified there:
  N file(s), X GB"*. Afterwards it shows the last files being sent, with **Stop sending**.

## Security
- **The destination is not taken from the web page.** The helper answers any website on 127.0.0.1:9876, so it
  sends only to the server this computer was set up for with the setup file (`%LOCALAPPDATA%\Aetheris\client-kit.json`).
  The page supplies only the session id and the upload token the server issued.
- **The token is limited:**
  - valid for one session, one case and one run;
  - stored on the server only as a SHA-256;
  - kept out of the collector's settings file and the command line;
  - wiped from the stream file when the upload is finished.
- **Upload endpoints accept only the run's own folders.** Those are `02`, `03`, `05`, `06`, `07` and `08` with
  the run name; there are no `..` parts, and the same name rules as the server writer apply.

## Fallbacks (the collection still runs, as in V1.31.13)
- **The client computer was not set up with the setup file:** the helper says so and the collection runs without
  streaming. Once the collection finishes, the page offers the copy.
- **The client helper is older than 5.66:** it ignores the session and the image is copied afterwards. Helpers set
  up with the setup file update themselves within about 10 minutes when no collection is running.
- **The server's HostDrive helper is offline:** no session is opened and the copy runs afterwards, as before.

## What you do
1. Deploy `POLARON_V1_31_14_UPDATED_FILES.zip` on the server as usual (website, API, scripts). It contains V1.31.0
   to V1.31.14.
2. **Restart the server's own helper** so it is 5.66 (the launcher does it). The `discard` step needs it.
3. Client computers:
   - **set up with the setup file:** they update themselves to 5.66, or click **Update this computer** on the
     acquisition page;
   - **not set up yet:** download the setup file from the page (**Set up this computer**) and run it once. That
     is also what tells the computer which server to send to.
4. Check with a phone on a client computer:
   - the Collect step says *sent to the forensic server as it runs*, and the counter grows while it runs;
   - after *Complete*, the server case folder has the run, `08_Hashes\<run>\client_stream_*_sha256.txt` and
     `07_Logs\<run>\client_stream_*.json`;
   - `C:\evidence\<CASE>` on the client holds only `MOVED_TO_SERVER_<run>.json` (or the full copy if you kept one).
   - The uploader's log is `%TEMP%\aetheris_acq_jobs\<job>.stream.log` on the client.

## Tests
- `backend/tests/test_client_stream_v1_31_14.py` (14, new):
  - **session:** stored next to the client copies; only a token hash is kept; a wrong token is refused; no
    session without the server helper;
  - **chunks and commits:** path and run checks; resume offsets; SHA-256 refusal; no overwrite;
  - **groups of small files:** a corrupted entry is refused alone;
  - **a changed file:** replaced before registration, never after the seal;
  - **finish:** missing files are listed; the registered run has server paths, the hash list, provenance and the
    sealed original extraction; finishing twice is harmless;
  - **stopping:** abort, and stop by the examiner;
  - **HTTP routes:** token only, plus the TSV status;
  - **end to end with the real uploader (pwsh) and a live server:**
    - an Android-style collection is streamed while it runs and moved: the local copy is removed, the receipt
      written and the token wiped;
    - an iPhone backup is sent from its staging folder, a changed file is sent again, the backup moved into the
      case is not sent again, and the empty staging folder is removed;
    - a killed uploader continues without sending the big file again;
    - a kept copy is identical to the server copy;
    - a stopped collection stays local;
  - **static checks:** the helper's pinned destination and the token removal; the uploader is ASCII and deletes
    junctions without following them.
- `frontend/src/lib/clientStream.v13114.test.tsx` (7, new): no server address goes to the helper; the session
  request; the fallback when the helper refuses (not reported as "busy"); following to completion; stopping when
  the uploader gave up; the job-id check; the progress line.
- Version checks moved to 5.66.
- Backend full suite: no new failures. It has the same 228 earlier failures and collection errors as the original
  files.
- Frontend: `tsc` clean, vitest 108/108, `vite build` OK.
- PowerShell suites: device list 33, client kit 23, diagnostic 8, drive hot-plug 11, iOS stall 15, recover 10,
  Surya 31. The helper's C# compiles with Mono.
- **Not run on Windows or with a real phone here.** The uploader ran under PowerShell 7 on Linux against the real
  API code with a stand-in for the server helper. It is written for Windows PowerShell 5.1 (ASCII, .NET
  Framework calls).

Originals: `recovery-snapshots/pre-v1.31.14-client-stream-20261118/`.
