# Polaron V1.31.8: the client computer updates itself and shows only the phone that is plugged in

## What was reported
On NITIN (the client computer), Android acquisition page:
1. "Detection limitations" said the Polaron helper on this computer is **version 5.60** and needs 5.62.
2. The browser console showed `GET http://127.0.0.1:9877/health net::ERR_CONNECTION_REFUSED`.
3. **Apple iPhone · this PC** was listed with no phone attached.

What was asked: check whether a phone is really connected. Show nothing when none is, the Android phone when
an Android phone is, the iPhone when an iPhone is, and then allow extraction, mainly on the client.

## Cause
- **The client never received the fixes.** The helper on NITIN runs from a Polaron folder on NITIN. It changed
  only when a release was copied there by hand, so NITIN still ran 5.60 after V1.31.6 and V1.31.7 were
  deployed on the server.
- **5.60 freezes its phone list.** It stops refreshing while a leftover collection process exists (fixed in 5.61,
  V1.31.6), so an iPhone from an earlier collection stays listed after it is unplugged. It also misses many
  Android phones (fixed in 5.62, V1.31.7).
- **Port 9877** is the Client Evidence Agent, which is not installed on NITIN. The page asked it for its status
  directly, and every refused call appears as a red console error.

## What changes
- **The server hands out the client files.** New API `/api/client-kit/...` (Disk and Android/iOS APIs), served
  from the server's own `scripts` and `backend/app` folders:
  - `info`: the helper version the server has;
  - `setup.cmd`: a setup file for this computer;
  - `manifest`: every client file with its SHA-256;
  - `file`: one listed file.

  Manifest and file need a key derived from the server secret. That key allows reading these product files
  only, not cases, evidence or users. Only top-level client scripts, the Client Evidence Agent and
  `backend/app` Python are served. Server diagnostics (`_*.py`), SQL and `.env` are never served.
- **One-time "Update this computer".** The Mobile acquisition page compares this computer's helper with the
  server's:
  - **Older than 5.62:** its phone list is **not shown**, because it cannot be trusted to say what is plugged
    in. An amber panel offers **Update this computer**.
  - **Not running** on a client computer: the panel offers **Set up this computer**.
  - **5.62 or later but older than the server:** a quiet "update available" note.

  The button downloads `Polaron-Setup-This-Computer.cmd`, which names this server. When run, it:
  - finds the Polaron folder the helper runs from (or creates `%LOCALAPPDATA%\Polaron\client`);
  - downloads only files that differ, checks each against the server's SHA-256, and keeps replaced files in
    `.polaron-update\backup-*`;
  - restarts the helper on the new version and registers the logon / 5-minute watchdog;
  - offers to install the Client Evidence Agent (one administrator prompt).

  It never touches `.env`, evidence or tools, and never restarts the helper during a collection.
- **From then on the computer updates itself.** `ensure-host-drive-helper.ps1`, which the watchdog runs every 5
  minutes, runs `scripts\polaron-client-update.ps1 -Watchdog`. At most every 10 minutes it compares with the
  server and applies changed files, but not while a phone collection is running (it retries later). After an
  update it continues with the new copy.
- **No restart during a collection.** A helper restart that nobody asked for (the watchdog finding an older
  helper) now waits until no collection is running. A restart the examiner forces (`-ForceRestart`) still
  happens.
- **No more 9877 console errors.** Helper **5.63** reports in `/health` whether the Client Evidence Agent
  answers on this computer. When it says no, the page does not call 127.0.0.1:9877 at all. Older helpers behave
  as before.
- The diagnostic script points to Update this computer.

## What you do
1. Deploy `POLARON_V1_31_8_UPDATED_FILES.zip` on the **server** as usual. It contains V1.31.0 to V1.31.8.
   Restart the API containers so the new router loads, and rebuild the website.
2. On **NITIN**, open the Android acquisition page. It shows **Update this computer before collecting**. Click
   it and open the downloaded `Polaron-Setup-This-Computer.cmd`. Windows may ask you to confirm running it.
   Accept the administrator prompt if you want the Client Evidence Agent.
3. When it says Done, click **Rescan**:
   - with no phone attached, nothing is listed;
   - an Android phone shows on the Android page, or as an amber "Action needed" card telling you what to choose
     on the phone;
   - an iPhone shows on the iOS page.

   Then Identify, Authorise and Collect as usual.

This one download is needed once, because the 5.60 helper cannot update itself. After it, updates deployed on the
server reach NITIN within about 10 minutes, never during a collection.

If NITIN shows the server's certificate as untrusted, open Polaron by its name (for example
`https://forensic.future-softtech.co.in`) before downloading. The setup file uses the address in the browser and
does not bypass certificate checks.

## Tests
- `tests/client_kit_update_checks.ps1`, 23 checks (new). The real updater runs against a stand-in server and
  helper. Covered:
  - only differing files are written, with a backup;
  - `.env` and paths outside `scripts/` and `backend/app/` are never written;
  - a second run changes nothing;
  - the watchdog waits 10 minutes between checks, then applies a change (exit 10);
  - a running collection postpones it (exit 11), in setup too;
  - a tampered download changes nothing (exit 3);
  - a wrong key is refused;
  - a computer that was never set up is left alone;
  - the script is ASCII.
- `backend/tests/test_client_kit_v1_31_8.py`, 13 tests (new). Covered:
  - the key is required;
  - only client files are listed and served, with no path tricks;
  - the key follows the server secret;
  - unsafe origins are refused for the setup file;
  - the setup file is a CRLF ASCII `.cmd`;
  - **end to end:** the PowerShell part of a generated setup file, run against the real endpoints served by
    uvicorn, installs the files;
  - the router is mounted in both APIs;
  - `ensure` checks for updates before its early exit and does not restart during a collection.

  The setup file's launcher line was also run with a stub to confirm it starts the PowerShell part and returns
  its exit code.
- `frontend/src/lib/clientKit.v1318.test.tsx`, 13 tests (new). Covered:
  - the version info and the setup download;
  - the routing;
  - the outdated / missing / update-available states;
  - the panel's download and error;
  - an outdated helper's iPhone is not listed, and a current helper's phones are;
  - no call to 9877 when the helper says the agent is not running.

  The V1.31.7 tests are updated to the hidden-list behaviour.
- The helper's C# (agent check in `/health`) was compiled with Mono and the port check exercised against an open
  and a closed port.
- Frontend: `tsc -b` clean, vitest 91/91, `vite build` OK.
- Backend full suite: no new failures. It has the same 228 earlier failures and collection errors as the original
  files.
- PowerShell suites: device list 29, diagnostic 8, Caddy 90, drive hot-plug 11, iOS stall 15, iOS recovery 10.
- Not run on Windows or with a real phone here.

Originals: `recovery-snapshots/pre-v1.31.8-client-update-20261117/`.
