# Polaron V1.31.1: the prod launcher starts Caddy when it is stopped

## The error
```
==> Add Polaron to the running Caddy (Caddy serves HTTPS and renews the certificates)
    No Caddy process is listening on TCP 80/443 on this PC; Caddy must be running for the reload.
STARTUP ERROR: Caddy was left unchanged; Polaron was not started. Caddy did not accept the reload. Its admin API must be enabled (no 'admin off'):
... Post "http://localhost:2019/load": dial tcp [::1]:2019: connectex: No connection could be made because the target machine actively refused it.
```

## Cause
`.env` has `PUBLIC_HTTPS_PROXY=caddy`, but Caddy was **not running**:
- nothing listened on TCP 80/443;
- nothing answered on Caddy's admin API (`localhost:2019`).

The usual reasons are a stopped Caddy service, a reboot with a Caddy that does not start automatically, or a
closed console window that was running `caddy run`.

V1.30 only reloads a running Caddy (`caddy reload` talks to that admin API), so startup stopped. It also blamed
`admin off`, which was wrong here. The Caddyfile itself was fine: it adapted to JSON without errors. The "not
formatted" line is only a style warning.

## What changes
When `PUBLIC_HTTPS_PROXY=caddy` and Caddy is stopped, the launcher starts it. It does this after the Caddyfile
with Polaron's sites passes `caddy validate`, and before the reload:

| How Caddy is installed on the PC | What the launcher does |
|---|---|
| Windows service (sc.exe, WinSW or NSSM) | starts the service; warns if it is set to *Manual* (stays stopped after a reboot) |
| Scheduled task that runs Caddy | runs the task |
| Neither | runs `caddy.exe run --config <CADDY_CONFIG> --adapter caddyfile` in its own hidden console. Log: `deployment\caddy\caddy-background.log`. It survives closing the launcher window, but not a reboot or sign-out (the launcher starts it again next time) |
| `CADDY_AUTOSTART=service` and no service | registers the Windows service `caddy` (automatic start, restart on crash), then starts it |

The launcher waits up to `CADDY_START_SECONDS` (default 45) until Caddy listens on TCP 80/443 or its admin API
answers. If Caddy exits at once, the error shows the end of Caddy's own log, for example a port already in use.

**Stops with a clear message, before anything is changed:**
- TCP 80/443 is held by another program, such as IIS/http.sys (PID 4), another web server or a container. The
  message names it. Polaron's own builtin-mode gateway is the exception: it is stopped so Caddy can listen there,
  and started again if Caddy fails.
- The Caddyfile has `admin off`, so `caddy reload` cannot work.
- The Caddy service is *Disabled*.
- `CADDY_AUTOSTART=off`.

**If Polaron's sites still cannot be added** (validate, start or reload fails):
- both files are restored byte for byte;
- the Caddy the launcher started is stopped again;
- a service it registered is removed;
- Polaron's gateway is started again.

The PC is left as it was.

A refused reload now names the real cause: "Caddy's admin API at localhost:2019 did not answer, so Caddy is not
running".

## What you do
1. Copy the files over the project.
2. Run `script_docker\start_docker_prod.cmd` **as Administrator** (needed to start a service). Look for
   `Caddy is running (...)`, then `Caddy now serves https://122.179.140.167/ and https://forensic.future-softtech.co.in/`.
3. Optional `.env` settings (the defaults need no change):
   - `CADDY_AUTOSTART=service`: Windows starts Caddy at boot. The service is registered at the next start where
     Caddy is stopped.
   - `CADDY_AUTOSTART=off`: the launcher never starts Caddy.
   - `CADDY_START_SECONDS=45`: how long the launcher waits for Caddy.

To run now without this update, start Caddy yourself and run the launcher again:
- with a service: `sc.exe start caddy`;
- otherwise, in a separate console window that stays open: `C:\caddy\caddy.exe run --config C:\caddy\Caddyfile --adapter caddyfile`.

## Tests
- `tests/caddy_integration_checks.ps1`: 90 checks pass (50 from V1.30, 40 new), with Caddy, Windows services and
  Docker mocked. The new checks cover:
  - admin address parsing (`admin off`, custom address, admin inside a site block);
  - `CADDY_AUTOSTART` values;
  - stopped-Caddy detection;
  - the start order validate → start → reload, for a service, a Manual service, a Disabled service, a scheduled
    task, a background start and service registration;
  - `off`;
  - a port held by http.sys;
  - Polaron's gateway moved aside, then rolled back when the reload fails;
  - a start timeout with full rollback;
  - a Caddy that exits at once, reported with its log;
  - the wording of the refused-reload error from this incident.
- Other launcher suites, unchanged:
  - `prod_https_edge_checks.ps1`: 19 pass;
  - `windows_public_https_handoff_checks.ps1`: 33 pass;
  - `dependency_reuse_checks.ps1`: 37 pass;
  - `backend/tests/test_caddy_https_v1_30.py`: 4 pass.
- Not run on Windows here. The service, scheduled-task, `Get-NetTCPConnection` and hidden `Start-Process` calls
  are mocked offline.

Originals: `recovery-snapshots/pre-v1.31.1-caddy-autostart-20261116/`.
