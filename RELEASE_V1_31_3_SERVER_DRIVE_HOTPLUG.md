# Polaron V1.31.3: drives connected to the forensic server appear (and disappear) by themselves

## The problem
Forensic server → **Server evidence browser** listed only C:, D:, E:, F:. A drive connected to the server
afterwards never appeared, and a removed drive stayed listed.

## Why
A container cannot see Windows' drive table. Polaron gets the live list of drives from the **workstation helper**
(`scripts\host-drive-helper.ps1`, port 9876 on the server). The forensic API asks it at
`http://host.docker.internal:9876`.

When the API could not reach the helper, `/api/hostdrive/drives` fell back to the drives Docker mounted when the
containers started (`docker-compose.drives.generated.yml`: C:, D:, E:, F:). It still answered "ok", so:
- the browser showed a frozen list;
- it never said new drives could not be detected.

One cause is fixed in this release: when `ensure-host-drive-helper.ps1` cannot reserve `http://+:9876/`, it starts
the helper on `http://127.0.0.1:9876/`. Windows' HTTP server then refuses every request whose Host header is not
`127.0.0.1:9876` ("Bad Request - Invalid Hostname"). Requests from the containers say `host.docker.internal`, so the
helper looked offline from Docker even though it answered on the server itself.

## What changes
- **The containers reach a helper bound to 127.0.0.1.** Requests to `host.docker.internal`,
  `gateway.docker.internal` or `192.168.65.254` (the Windows host) carry `Host: 127.0.0.1:9876`:
  - drive listing (`hostdrive_agent`);
  - phone collection (`host_bridge`);
  - client mobile import.

  A helper bound to `+` accepts any Host, so nothing changes there.
- **Live drive list.** The Server evidence browser checks the server's drives every 5 s (every 30 s while the list
  cannot be live; paused while the tab is hidden):
  - **A drive connected:** it appears in This PC within seconds, with "New drive G: on the forensic server…". Open
    it and select the evidence folder. As before, Docker mounts that folder read-only just before extraction.
  - **A drive removed:** it disappears. If you were browsing it, you are taken back to This PC with "Drive E: was
    removed".
  - Nothing restarts or remounts Docker while watching. Containers are recreated only for the folder you start
    extraction from, as before.
  - The header shows "new drives appear automatically" while the list is live.
- **When the list cannot be live, the browser says so.** An amber note under the header gives:
  - "Drives connected to the forensic server after Polaron started are not detected";
  - the reason (helper not running, only accepts 127.0.0.1, token mismatch, firewall/timeout);
  - the fix.

  `/api/hostdrive/drives` now returns `live`, `helper_error` and `helper_errors`.
- **Startup check.** After it starts the helper, `start_docker_prod.cmd` asks the helper from inside the forensic
  API container:
  - when reachable: "The forensic containers reach the workstation helper …; drives connected later appear …
    Drives now: C: D: E: F:";
  - when not: the reason and the fix (run as Administrator, or
    `scripts\configure-host-drive-helper-network.ps1` and `scripts\ensure-host-drive-helper.ps1 -ForceRestart`).

  The check never stops startup.

## What you do
1. Unzip `POLARON_V1_31_3_UPDATED_FILES.zip` over the project root. It contains V1.31.0 to V1.31.3. No `.env`
   change is needed.
2. Run `script_docker\start_docker_prod.cmd` as Administrator, then read the line after "Check that the forensic
   containers reach the workstation helper".
3. Press Ctrl+F5 in the browser. Connect a USB disk to the server: it should appear in This PC within about 5 s.

## Limits
- The helper runs in the Windows session that started it. After a reboot, run the launcher again, or install the
  logon task once: `scripts\install-host-drive-helper-task.ps1`.
- A drive that Windows has not assigned a letter to (for example an uninitialised disk, or one with no letter in
  Disk Management) cannot be listed.

## Tests
- Backend: `test_server_drive_hotplug_v1_31_3.py` (6 tests). A local server that behaves like Windows' HTTP server
  bound to 127.0.0.1 refuses a plain request (400), but is reached through `host.docker.internal` with the new
  header. A drive is added and removed between listings, an unreachable helper is reported with its reason, and
  the mobile bridge sends the same header. Client-evidence, browser-share and mobile-import suites: 72 passed,
  1 skipped. The helper/drive suites show the same 8 older source-text failures before and after this change.
- Frontend: vitest 66/66, `tsc` clean, `vite build` OK. New UI tests:
  - a drive connected after startup appears and is dropped when removed;
  - the drive being browsed is removed → back to This PC;
  - the "not detected" note with the reason.
- Launcher: `tests/server_drive_hotplug_checks.ps1` (11 checks); Caddy 90, HTTPS edge 19, handoff 33, dependency
  reuse 37, all unchanged.
- Not run on a Windows server here. The startup check prints the real situation on yours.

Originals: `recovery-snapshots/pre-v1.31.3-server-drive-hotplug-20261116/` (and the V1.31.0–V1.31.2 folders).
