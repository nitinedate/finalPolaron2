# Polaron V1.31.0: choose evidence on "This computer" without installing anything

## The problem
New disk case → **Select evidence directory**:
- **Forensic server** lists the server's drives (C:, D:, E:, F:) and works.
- **This computer** showed only "Install the Client Evidence Agent on this computer (one time)" and no drives, so
  extraction and RAG could not start from the examiner's own PC.

## Why it happened
A web page cannot list the drives of the computer it runs on. Until now, "This computer" listed drives only through
the **Client Evidence Agent** (TCP 9877 on the examiner's PC, V1.21/V1.22). With no agent, the dialog had nothing
to show except the install panel.

Two more cases ended at the same panel even with the agent installed:
- Chrome now asks permission before a site may contact apps on the same PC (*Local Network Access*: "Apps on this
  device" / `loopback-network`). After **Block**, the agent looks "not installed".
- Over plain `http://`, Chrome refuses that permission without asking.

## What changes
### 1. "Choose folder on this computer…" (no install, works on any PC)
The **This computer** tab opens with the computer's own folder window:
1. Click **Choose folder on this computer…** and pick the folder that holds the evidence: an E01 set, DD/RAW,
   VMDK/VHDX, AFF, a UFED/mobile extraction, or any other folder. Every file in it is served. Every drive of that PC
   is available in that window: C:, D:, USB disks and mapped network drives.
2. The folder appears in the selected list as `<name> · chosen in this browser · N files`. Several folders can be
   added, mixed with folders from the agent.
3. **Start extraction** → the case runs the normal pipeline (extraction, parsing, OCR, RAG), exactly as for a server
   path.

How the server reads the files:
- Nothing is copied first. This browser tab answers the server's read requests over the same outbound HTTPS relay
  the agent uses since V1.22 (`/api/client-agent/relay/...`, the same request and reply format). The server reads
  only the byte ranges the extraction needs: an E01 is not uploaded whole.
- Empty space travels as a short marker and text compresses on the wire (the agent's `zero` / `deflate` encodings).
- The list of files (paths, sizes, modified times) is fixed when the folder is chosen and protected by the same
  SHA-256 digest the agent uses. If a file changes on the PC afterwards, reads stop with "evidence modified" (409).
  The examiner sees an error; changed data is never mixed in.
- Read-only: the tab never writes to the folder.
- Works over `http://` as well. Without HTTPS the browser uses its classic folder input, and the code uses its own
  SHA-256 when WebCrypto is unavailable.

While the tab serves evidence:
- An amber banner on every page shows "Serving evidence from this computer … keep this tab open until processing
  finishes" and how much the server has read.
- Closing or reloading the tab asks for confirmation.
- The job's source card shows "Served by this browser tab: X read so far".
- After a reload or a closed tab, the job card offers **Reconnect folder from this computer…**. Choose the same
  folder: it is accepted only if every file still matches the digest, then processing continues.

### 2. The Client Evidence Agent stays available, as an option
Under the folder chooser: "Optional: show this computer's drive list with the Client Evidence Agent (one-time
install; keeps working when this tab is closed)". Use the agent for very long jobs or when the tab cannot stay
open. If the agent is installed but blocked, the dialog now says so and shows how to fix it:
- lock icon in the address bar → **Site settings**;
- **Local network access** / **Apps on this device** → **Allow**;
- **Check again**.

### 3. Server
- `POST /api/jobs/{id}/client-source` accepts `browser: true`. Such a source is reached only through the outbound
  relay (no LAN address is tried) and is recorded as `browser_share` in the job.
- If the relay was turned off with `CLIENT_EVIDENCE_RELAY_ENABLED=false`, the request is refused with a clear
  message.
- The status endpoint reports `browser_share` so the job card shows the right text.
- No new containers, ports or `.env` keys. The relay is on by default (`CLIENT_EVIDENCE_RELAY_ENABLED=true` is
  already in `.env.example`).

## What you do
1. Restart the API containers (backend code is bind-mounted, so no image rebuild is needed):
   `docker compose restart api` (plus the Android/iOS API containers if you add client folders there).
2. Rebuild the frontend: `scripts\rebuild-ui.ps1` (or the build you use today).
3. Open the console through HTTPS (`https://<PUBLIC_HTTPS_IP>/`, V1.30). The folder chooser also works over HTTP, but
   Chrome refuses the agent's permission on `http://` pages, so the agent option needs HTTPS.

## Limits
- The tab must stay open, and the PC must stay awake and online, until extraction and indexing finish. Speed
  depends on the PC's upload speed. For multi-hour jobs, the agent or copying the image to the server is better.
- Browsers do not reveal full local paths, so the case shows the folder name (for example `Case-0423`), not
  `D:\Cases\Case-0423`.
- A browser folder can hold up to 250,000 files.
- Tested in Node 22 with the same File, Blob, WebCrypto and CompressionStream APIs Chrome uses, and in jsdom for the
  dialogs. Not yet run in a real Chrome/Edge/Firefox on Windows: do one test case before relying on it.

## Tests
- `frontend/src/lib/browserEvidenceShare.test.tsx` (7):
  - the same manifest digest as the server's Python code;
  - SHA-256 fallback equal to WebCrypto;
  - skipped system folders;
  - zero/deflate encoding;
  - scripted relay (manifest, ranges, clipping, 404/416, revoke stops the tab);
  - a changed file refused with 409;
  - reconnect accepts only the unchanged folder.
- `frontend/src/components/forensic/ClientEvidenceBrowser.test.tsx` (+5):
  - folder chooser shown without the agent;
  - selected list and confirm;
  - "browser blocks this site" message;
  - no-folder-window message;
  - the panel registers with `browser: true, relay: true` and starts the case;
  - Reconnect after a reload.
- `backend/tests/test_browser_evidence_share_v1_31.py` (4): relay-only attach, refusal when the relay is off, schema,
  and an end-to-end run. The end-to-end run starts a real uvicorn + Redis relay, and the real TS module (in Node)
  serves a folder to the server's FUSE reader. It checks:
  - byte-identical reads of E01/E02, a zero-filled file, a log, an empty file and a `Café résumé` name;
  - zero-marker and deflate savings;
  - a modified file stopping reads;
  - revoke stopping the tab.

  Driver and instructions: `backend/tests/browser_share_e2e/`.
- Results:
  - frontend: `tsc` clean, vitest 58/58, `vite build` OK;
  - backend client-evidence suites: 48 passed (4 new), 1 skipped, including the agent's C# 5 compile check.

Originals: `recovery-snapshots/pre-v1.31.0-browser-folder-20261116/`.
