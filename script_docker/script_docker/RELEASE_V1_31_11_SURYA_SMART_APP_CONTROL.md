# Polaron V1.31.11: Surya OCR when Smart App Control blocks part of llama.cpp

## What was reported
The launcher started `llama-server.exe`, Windows showed **"Smart App Control has blocked part of this app"**, and
startup stopped:

    [Surya] Starting Windows host llama.cpp server (GPU layers=12; context=24576)...
    STARTUP ERROR: Surya inference failed to reach ready state. Log: ...\SuryaOCR\surya-server.err.log
    Startup failed with exit code 1.

## Cause
- **A different kind of block.** This time Windows let `llama-server.exe` start, then refused one of the DLLs it
  loads ("part of this app"). The server ended without a clear error.
- **V1.31.10 would not have recognised it.** V1.31.10 switched to Docker only when Windows refused the program
  itself.
- **The old launcher was used.** The "STARTUP ERROR" line shows this run was made with the launcher from before
  V1.31.10. From V1.31.10 on, an OCR failure does not stop Polaron starting.

## What changes (scripts\ensure_host_surya.ps1)
- **Checked before trying.** If **Smart App Control is on**, or the organisation's App Control enforces code
  integrity for applications, llama.cpp's unsigned Windows build cannot run on this computer. Surya then goes
  straight to Docker, without a Windows warning and without downloading the Windows build.
  - Smart App Control is read from `HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy`, and App Control for
    Business from Win32_DeviceGuard.
  - In evaluation mode Smart App Control does not block, so the Windows build is still tried.
- **Any Windows-build failure falls back to Docker.** In auto mode, if the Windows server does not become ready
  for any reason, Docker is used for this launch and later ones. This covers a blocked DLL, a crash, or a missing
  file.
  - The Windows Code Integrity log is read so the blocked file is named in the message.
  - A server that is still loading after the wait is stopped first, and only that process (the one this script
    started).
  - The choice is remembered in `surya-runtime.json` only after Docker has worked. Delete that file to try the
    Windows build again.
- **If Docker fails too,** one error names both causes. The launcher (V1.31.10) then starts Polaron without OCR
  and says so; set `SURYA_REQUIRED=true` to stop instead.
- `SURYA_HOST_RUNTIME=native` keeps the strict behaviour: it reports the block or failure and does not use Docker.

## What you do
1. On the computer where you run the launcher, copy **all** of `POLARON_V1_31_11_UPDATED_FILES.zip` over the
   Polaron folder, including `script_docker\` and `scripts\`. It contains V1.31.0 to V1.31.11.
2. Make sure Docker Desktop is running, then run the launcher. At the Surya step it says:
   *Smart App Control is on: llama.cpp's unsigned Windows build cannot run on this computer. Using the same server
   in Docker.*
   - The first run pulls `ghcr.io/ggml-org/llama.cpp:server-cuda` (NVIDIA) or `:server` (CPU).
   - It then reports **Host inference ready (Docker container polaron-surya-ocr)**.
   - Later launches reuse the container.

## Tests
- `tests/surya_app_control_checks.ps1`: 30 checks (10 new). The real manager runs with Windows and Docker
  simulated. New cases:
  - Smart App Control on, or enforced App Control for Business, goes to Docker with no attempt and no
    llama.cpp download, and the reason is recorded;
  - Smart App Control in evaluation still uses the Windows build;
  - **the reported case:** the server starts and then ends, the Code Integrity log names `ggml-vulkan.dll`, and
    Docker is used with that reason recorded;
  - the server ends with nothing logged, and Docker is used and remembered;
  - Docker failing too reports both causes and remembers nothing;
  - `native` reports other errors, while `auto` falls back.
- `backend/tests/test_surya_host_bootstrap.py`: 17 pass.
- Not run on Windows here; Smart App Control, the Code Integrity log and Docker are simulated.

Originals: `recovery-snapshots/pre-v1.31.11-surya-smart-app-control-20261117/`.
