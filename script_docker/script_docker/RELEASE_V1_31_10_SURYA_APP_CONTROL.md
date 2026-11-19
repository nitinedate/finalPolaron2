# Polaron V1.31.10: Surya OCR runs in Docker when Windows App Control blocks llama-server.exe

## What was reported
Starting Polaron locally stopped at **Ensure host SuryaOCR inference**:

    [Surya] Starting Windows host llama.cpp server (GPU layers=12; context=24576)...
    STARTUP ERROR: This command cannot be run due to the error: An Application Control policy has blocked this file.
    Startup failed with exit code 1.

## Cause
- Windows App Control refused to start the downloaded llama.cpp server
  (`%LOCALAPPDATA%\Polaron\SuryaOCR\llama-runtimes\...\llama-server.exe`). This is either Smart App Control on
  Windows 11 or an organisation's App Control (WDAC) policy.
- llama.cpp's Windows builds are not code-signed, so downloading another build would be blocked in the same way.
- The launcher treated any OCR problem as fatal, so the whole of Polaron did not start.

## What changes
- **Docker fallback (scripts\ensure_host_surya.ps1).** When Windows App Control blocks llama-server.exe, the
  manager detects it, either way it shows up:
  - from the "Application Control policy has blocked this file" error; or
  - when the process is killed at load with `0xC0000428`.

  It then runs the **same llama.cpp server in Docker**. Linux containers are not subject to Windows App Control.
  The container:
  - is called `polaron-surya-ocr` and uses the official `ghcr.io/ggml-org/llama.cpp` image: `server-cuda` with
    `--gpus all` when an NVIDIA GPU is available, otherwise `server` (CPU);
  - uses the same cached GGUF weights (mounted read-only), port 8000 and model ID `datalab-to/surya-ocr-2.5`.
    Nothing changes for the forensic containers, which still use `host.docker.internal:8000`;
  - restarts with Docker (`--restart unless-stopped`);
  - falls back to the CPU image if the CUDA one does not start.

  The choice is saved in `%LOCALAPPDATA%\Polaron\SuryaOCR\surya-runtime.json`. Later launches go straight to Docker
  and download only the model weights.
- **New `.env` settings:**
  - `SURYA_HOST_RUNTIME=auto|native|docker` (default `auto`). `native` reports the block instead of switching;
    `docker` always uses Docker.
  - `SURYA_DOCKER_IMAGE` overrides the image; `SURYA_DOCKER_GPU=0` forces CPU.
  - The existing `SURYA_HOST_*` values in `.env` now actually reach the manager. Before, only
    `SURYA_HOST_MODE` was read.
- **OCR no longer stops Polaron (script_docker\start_docker.ps1).** If Surya still cannot start (for example,
  Docker cannot pull the image), the launcher:
  - prints the reason;
  - starts Polaron **without OCR**: text in scanned pages and images is not read;
  - skips the OCR reachability check;
  - repeats the warning next to READY.

  `SURYA_REQUIRED=true` keeps the old behaviour (stop the launch).
- Any other llama-server start failure (missing file, crash) is still reported as before. Only an App Control
  block switches to Docker.

## What you do
1. Copy `POLARON_V1_31_10_UPDATED_FILES.zip` over the Polaron folder on the computer where you run the launcher
   (it contains V1.31.0 to V1.31.10).
2. Run the launcher again. At the Surya step:
   - it reports the App Control block once;
   - it pulls the llama.cpp image the first time (about 1-2 GB for the CUDA image);
   - it says **Host inference ready (Docker container polaron-surya-ocr)**.

   Docker Desktop must be running and able to reach `ghcr.io`.
3. Optional: add `SURYA_HOST_RUNTIME=auto` and `SURYA_REQUIRED=false` to `.env` (the defaults; see
   `.env.example`).

To use the Windows build instead, allow it in App Control. Smart App Control can only be turned off as a whole,
in Windows Security > App & browser control. A WDAC policy needs an allow rule from your administrator. Then
delete `surya-runtime.json`.

## Tests
- `tests/surya_app_control_checks.ps1`, 20 checks (new). The real manager runs with Docker, Start-Process, the
  HTTP probe and the Python bootstrap simulated. Covered:
  - the App Control error leads to one Docker container with the same model, projector, model ID, port and
    read-only weights, and the choice is remembered;
  - the next launch goes straight to Docker, downloading weights only;
  - a running ready container is kept;
  - with NVIDIA, the CUDA image with `--gpus all` is used, and the CPU image if CUDA fails;
  - `0xC0000428` counts as a block;
  - `SURYA_HOST_RUNTIME=native` reports the block;
  - other errors are not taken for App Control;
  - an allowed native server is still used;
  - the scripts are ASCII;
  - the launcher continues without OCR and skips the reachability check.
- `backend/tests/test_surya_host_bootstrap.py`: 17 pass (one updated to the new argument list, one new).
- `tests/test_cached_build_restart_20261110.py`: the same 3 failures as the original files.
- Caddy 90 and drive hot-plug 11 launcher suites still pass.
- Not run on Windows here. Docker and App Control are simulated.

Originals: `recovery-snapshots/pre-v1.31.10-surya-app-control-20261117/`.
